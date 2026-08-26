"""Language-neutral conformance runner for context discovery and routing."""

from __future__ import annotations

import builtins
import copy
import io
import json
import os
import socket
import subprocess
import sys
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from taf_context.consent import AuthorizationLedger
from taf_context.discovery import discover_providers
from taf_context.models import RepositorySnapshot, canonical_json
from taf_context.provider_models import (
    BrokerRequest,
    DiscoverySnapshot,
    ProjectRegistration,
    ProviderDescriptor,
    RoutingDecision,
    parse_host_inventory,
)
from taf_context.routing import route_provider


_PREBOUND_OPEN = builtins.open
_PREBOUND_POPEN = subprocess.Popen
_PREBOUND_SOCKET = socket.socket
_PREBOUND_SOCKET_BIND = socket.socket.bind
_PREBOUND_STAT = os.stat
_PREBOUND_PATH_EXISTS = Path.exists


FIXTURES = Path(__file__).with_name("conformance") / "context-discovery-routing"
SCHEMA = "taf-context-conformance/1"
FIXTURE_FIELDS = {
    "schema",
    "name",
    "snapshot",
    "host_inventory",
    "user_registry",
    "project_registration",
    "consent",
    "request",
    "expected_discovery",
    "expected_decision",
}
CONSENT_FIELDS = {"ledger", "state_usable", "utc_now"}
_FORBIDDEN_AUDIT_EVENTS = {
    "open",
    "os.listdir",
    "os.scandir",
    "os.system",
    "subprocess.Popen",
    "urllib.Request",
}
_FORBIDDEN_AUDIT_PREFIXES = (
    "os.exec",
    "os.fork",
    "os.posix_spawn",
    "os.spawn",
    "socket.",
)
_FORBIDDEN_FILESYSTEM_METADATA_CALLS = tuple(
    function
    for name in (
        "access",
        "fpathconf",
        "fstat",
        "fstatvfs",
        "getxattr",
        "listxattr",
        "lstat",
        "pathconf",
        "readlink",
        "stat",
        "statvfs",
    )
    if (function := getattr(os, name, None)) is not None
)
_forbidden_audit_depth = 0
_forbidden_audit_hook_installed = False


class DuplicateKeyError(ValueError):
    """Raised when a fixture contains an ambiguous JSON object."""


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _canonical_bytes(value: object) -> bytes:
    return canonical_json(value).encode("utf-8")


def _load_fixture(path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    value = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_strict_object,
        parse_constant=_reject_constant,
    )
    if type(value) is not dict or set(value) != FIXTURE_FIELDS:
        raise ValueError(f"{path.name}: fixture fields")
    if value["schema"] != SCHEMA:
        raise ValueError(f"{path.name}: fixture schema")
    if not isinstance(value["name"], str) or not value["name"]:
        raise ValueError(f"{path.name}: fixture name")
    if raw != _canonical_bytes(value):
        raise ValueError(f"{path.name}: fixture is not canonical JSON")
    return value


def _execute(
    fixture: dict[str, object], *, reverse_inputs: bool = False
) -> tuple[bytes, bytes]:
    value = copy.deepcopy(fixture)
    host_inventory_wire = value["host_inventory"]
    user_registry_wire = value["user_registry"]
    consent_wire = value["consent"]
    if type(host_inventory_wire) is not dict:
        raise ValueError("host_inventory")
    if type(user_registry_wire) is not list:
        raise ValueError("user_registry")
    if type(consent_wire) is not dict or set(consent_wire) != CONSENT_FIELDS:
        raise ValueError("consent")

    if reverse_inputs:
        providers = host_inventory_wire.get("providers")
        if type(providers) is list:
            providers.reverse()
        user_registry_wire.reverse()
        ledger = consent_wire.get("ledger")
        if type(ledger) is dict and type(ledger.get("records")) is list:
            ledger["records"].reverse()  # type: ignore[union-attr]

    snapshot = RepositorySnapshot.from_dict(value["snapshot"])  # type: ignore[arg-type]
    inventory = parse_host_inventory(host_inventory_wire).inventory
    user_registry = tuple(
        ProviderDescriptor.from_dict(item) for item in user_registry_wire
    )
    registration_wire = value["project_registration"]
    registration = (
        None
        if registration_wire is None
        else ProjectRegistration.from_dict(registration_wire)  # type: ignore[arg-type]
    )
    consent = AuthorizationLedger.from_dict(consent_wire["ledger"])  # type: ignore[arg-type]
    request = BrokerRequest.from_dict(value["request"])  # type: ignore[arg-type]

    discovery = discover_providers(
        snapshot,
        inventory,
        user_registry,
        registration,
    )
    decision = route_provider(
        discovery,
        request,
        consent,
        utc_now=consent_wire["utc_now"],  # type: ignore[arg-type]
        consent_state_usable=consent_wire["state_usable"],  # type: ignore[arg-type]
    )
    return _canonical_bytes(discovery.to_dict()), _canonical_bytes(decision.to_dict())


def _forbid_active_discovery() -> object:
    forbidden = AssertionError("conformance discovery attempted forbidden activity")
    return _ForbiddenActivityGuard(
        _PatchStack(
            patch.object(subprocess, "Popen", side_effect=forbidden),
            patch.object(subprocess, "run", side_effect=forbidden),
            patch.object(socket, "socket", side_effect=forbidden),
            patch.object(os, "listdir", side_effect=forbidden),
            patch.object(os, "open", side_effect=forbidden),
            patch.object(os, "scandir", side_effect=forbidden),
            patch.object(os, "system", side_effect=forbidden),
            patch.object(Path, "glob", side_effect=forbidden),
            patch.object(Path, "iterdir", side_effect=forbidden),
            patch.object(Path, "open", side_effect=forbidden),
            patch.object(Path, "rglob", side_effect=forbidden),
            patch.object(Path, "read_text", side_effect=forbidden),
            patch.object(Path, "read_bytes", side_effect=forbidden),
            patch.object(builtins, "open", side_effect=forbidden),
            patch.object(io, "open", side_effect=forbidden),
        )
    )


def _forbidden_audit_hook(event: str, _arguments: tuple[object, ...]) -> None:
    if not _forbidden_audit_depth:
        return
    if event in _FORBIDDEN_AUDIT_EVENTS or event.startswith(
        _FORBIDDEN_AUDIT_PREFIXES
    ):
        raise AssertionError(
            f"conformance discovery attempted forbidden activity: {event}"
        )


def _install_forbidden_audit_hook() -> None:
    global _forbidden_audit_hook_installed
    if not _forbidden_audit_hook_installed:
        sys.addaudithook(_forbidden_audit_hook)
        _forbidden_audit_hook_installed = True


def _forbidden_call_profile(
    _frame: object, event: str, argument: object
) -> None:
    if not _forbidden_audit_depth or event != "c_call":
        return
    if any(
        argument is forbidden
        for forbidden in _FORBIDDEN_FILESYSTEM_METADATA_CALLS
    ):
        name = getattr(argument, "__name__", "metadata")
        raise AssertionError(
            "conformance discovery attempted forbidden activity: "
            f"filesystem.{name}"
        )


def _get_thread_profile_hook() -> object:
    getter = getattr(threading, "getprofile", None)
    return getter() if callable(getter) else getattr(
        threading, "_profile_hook", None
    )


class _ForbiddenActivityGuard:
    def __init__(self, patches: "_PatchStack") -> None:
        self._patches = patches
        self._previous_profile: object = None
        self._previous_thread_profile: object = None

    def _profile(self, frame: object, event: str, argument: object) -> None:
        _forbidden_call_profile(frame, event, argument)
        if callable(self._previous_profile):
            self._previous_profile(frame, event, argument)

    def _thread_profile(
        self, frame: object, event: str, argument: object
    ) -> None:
        _forbidden_call_profile(frame, event, argument)
        if callable(self._previous_thread_profile):
            self._previous_thread_profile(frame, event, argument)

    def __enter__(self) -> "_ForbiddenActivityGuard":
        global _forbidden_audit_depth
        _install_forbidden_audit_hook()
        _forbidden_audit_depth += 1
        self._previous_profile = sys.getprofile()
        self._previous_thread_profile = _get_thread_profile_hook()
        try:
            sys.setprofile(self._profile)
            threading.setprofile(self._thread_profile)
            self._patches.__enter__()
        except BaseException:
            _forbidden_audit_depth -= 1
            try:
                threading.setprofile(  # type: ignore[arg-type]
                    self._previous_thread_profile
                )
            finally:
                sys.setprofile(self._previous_profile)  # type: ignore[arg-type]
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        global _forbidden_audit_depth
        try:
            self._patches.__exit__(*exc)
        finally:
            _forbidden_audit_depth -= 1
            try:
                threading.setprofile(  # type: ignore[arg-type]
                    self._previous_thread_profile
                )
            finally:
                sys.setprofile(self._previous_profile)  # type: ignore[arg-type]


class _PatchStack:
    def __init__(self, *patchers: object) -> None:
        self._patchers = patchers

    def __enter__(self) -> "_PatchStack":
        for patcher in self._patchers:
            patcher.start()  # type: ignore[attr-defined]
        return self

    def __exit__(self, *exc: object) -> None:
        for patcher in reversed(self._patchers):
            patcher.stop()  # type: ignore[attr-defined]


class ForbiddenActivityGuardTests(unittest.TestCase):
    @staticmethod
    def _thread_profile_hook() -> object:
        return _get_thread_profile_hook()

    def _assert_worker_call_blocked(self, probe: object) -> None:
        outcomes: list[object] = []

        def worker() -> None:
            try:
                probe()  # type: ignore[operator]
            except BaseException as error:
                outcomes.append(error)
            else:
                outcomes.append(None)

        original_thread_profile = self._thread_profile_hook()

        def previous_thread_profile(
            _frame: object, _event: str, _argument: object
        ) -> None:
            return None

        threading.setprofile(previous_thread_profile)
        try:
            with _forbid_active_discovery():
                thread = threading.Thread(target=worker)
                thread.start()
                thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), "metadata probe thread did not finish")
            self.assertIs(self._thread_profile_hook(), previous_thread_profile)
            self.assertEqual(len(outcomes), 1)
            self.assertIsInstance(outcomes[0], AssertionError)
            self.assertRegex(
                str(outcomes[0]), r"forbidden activity: filesystem\.stat"
            )
        finally:
            threading.setprofile(original_thread_profile)  # type: ignore[arg-type]

    def test_guard_blocks_prebound_file_alias(self) -> None:
        with self.assertRaisesRegex(AssertionError, "forbidden activity"):
            with _forbid_active_discovery(), _PREBOUND_OPEN(os.devnull, "rb"):
                pass

    def test_guard_blocks_prebound_process_alias(self) -> None:
        with self.assertRaisesRegex(AssertionError, "forbidden activity"):
            with _forbid_active_discovery():
                process = _PREBOUND_POPEN([sys.executable, "-c", "pass"])
                process.wait(timeout=5)

    def test_guard_blocks_prebound_socket_alias(self) -> None:
        with self.assertRaisesRegex(AssertionError, "forbidden activity"):
            with _forbid_active_discovery():
                active_socket = _PREBOUND_SOCKET()
                active_socket.close()

    def test_guard_blocks_prebound_network_alias(self) -> None:
        active_socket = _PREBOUND_SOCKET()
        try:
            with self.assertRaisesRegex(AssertionError, "forbidden activity"):
                with _forbid_active_discovery():
                    _PREBOUND_SOCKET_BIND(active_socket, ("127.0.0.1", 0))
        finally:
            active_socket.close()

    def test_guard_blocks_prebound_filesystem_metadata_alias(self) -> None:
        with self.assertRaisesRegex(AssertionError, "forbidden activity"):
            with _forbid_active_discovery():
                _PREBOUND_STAT(os.devnull)

    def test_guard_blocks_prebound_path_existence_alias(self) -> None:
        with self.assertRaisesRegex(AssertionError, "forbidden activity"):
            with _forbid_active_discovery():
                _PREBOUND_PATH_EXISTS(Path(os.devnull))

    def test_guard_blocks_prebound_metadata_alias_in_worker_thread(self) -> None:
        self._assert_worker_call_blocked(lambda: _PREBOUND_STAT(os.devnull))

    def test_guard_blocks_prebound_existence_alias_in_worker_thread(self) -> None:
        self._assert_worker_call_blocked(
            lambda: _PREBOUND_PATH_EXISTS(Path(os.devnull))
        )


class ContextConformanceTests(unittest.TestCase):
    def test_context_discovery_routing_vectors(self) -> None:
        paths = sorted(FIXTURES.glob("*.json"))
        self.assertEqual(len(paths), 16, "expected exactly 16 conformance fixtures")
        fixtures = [_load_fixture(path) for path in paths]
        names = [fixture["name"] for fixture in fixtures]
        self.assertEqual(len(names), len(set(names)), "duplicate conformance fixture name")

        for path, fixture in zip(paths, fixtures):
            name = fixture["name"]
            guard = (
                _forbid_active_discovery()
                if name == "zero-forbidden-discovery-activity"
                else nullcontext()
            )
            with self.subTest(path=path.name, name=name), guard:
                discovery, decision = _execute(fixture)
                expected_discovery = _canonical_bytes(fixture["expected_discovery"])
                self.assertEqual(
                    discovery,
                    expected_discovery,
                    f"{name}: expected_discovery mismatch",
                )
                DiscoverySnapshot.from_dict(fixture["expected_discovery"])  # type: ignore[arg-type]

                expected_decision = _canonical_bytes(fixture["expected_decision"])
                self.assertEqual(
                    decision,
                    expected_decision,
                    f"{name}: expected_decision mismatch",
                )
                RoutingDecision.from_dict(fixture["expected_decision"])  # type: ignore[arg-type]

                repeated = _execute(fixture)
                permuted = _execute(fixture, reverse_inputs=True)
                self.assertEqual((discovery, decision), repeated)
                self.assertEqual((discovery, decision), permuted)


if __name__ == "__main__":
    unittest.main()
