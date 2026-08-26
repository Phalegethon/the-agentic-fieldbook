"""Language-neutral conformance runner for context discovery and routing."""

from __future__ import annotations

import builtins
import copy
import io
import json
import os
import socket
import subprocess
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
    return _PatchStack(
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
