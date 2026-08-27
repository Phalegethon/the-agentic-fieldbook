"""Black-box tests for provider discovery, routing, and consent commands."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from taf_context.cli import main
from taf_context.git_snapshot import collect_snapshot

from .repo_factory import init_committed_repo
from .test_level1_models import request_wire


FIXED_NOW = datetime(2026, 8, 26, 12, 34, 56, tzinfo=timezone.utc)
FIXED_TIMESTAMP = "2026-08-26T12:34:56Z"
MAX_CONTROL_BYTES = 256 * 1024


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ) + "\n"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_canonical(value), encoding="utf-8")


def _invoke(
    *argv: str,
    state_home: Path | None = None,
    stdin: str | None = None,
    now: datetime = FIXED_NOW,
) -> tuple[int, str, str]:
    stdout = StringIO()
    stderr = StringIO()
    environment = {
        "HOME": "/global-home-must-not-be-used",
        "TAF_STATE_HOME": os.fspath(state_home or Path("/missing-test-state")),
    }
    input_stream = StringIO(stdin) if stdin is not None else None
    with mock.patch.dict(os.environ, environment, clear=True), mock.patch(
        "sys.stdin", input_stream or StringIO("")
    ):
        code = main(
            list(argv),
            stdout=stdout,
            stderr=stderr,
            utc_clock=lambda: now,
        )
    return code, stdout.getvalue(), stderr.getvalue()


def _invoke_explicit_environment(
    argv: list[str], environment: dict[str, str]
) -> tuple[int, str, str]:
    stdout = StringIO()
    stderr = StringIO()
    code = main(
        argv,
        stdout=stdout,
        stderr=stderr,
        utc_clock=lambda: FIXED_NOW,
        environment=environment,
    )
    return code, stdout.getvalue(), stderr.getvalue()


def _descriptor(
    *,
    identity: str = "local.graph",
    source: str = "host-inventory",
    status_evidence: str = "provider-inspected",
) -> dict[str, object]:
    return {
        "schema_version": "1",
        "provider_identity": identity,
        "provider_version": "1.0.0",
        "provider_schema_version": "1",
        "capabilities": ["symbol-graph"],
        "locality": "local",
        "discovery_sources": [source],
        "availability": "available",
        "registration": (
            "unregistered" if source == "host-inventory" else "user-registered"
        ),
        "status_evidence": status_evidence,
        "freshness": "exact",
        "path_coverage": 1.0,
        "language_coverage": 1.0,
        "latency_ms": 2.0,
        "confidence": "verified",
        "supported_actions": ["inspect", "query"],
        "required_actions": ["query"],
        "marker_hints": [],
        "reason_codes": [],
        "warnings": [],
    }


def _inventory(*providers: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "1",
        "providers": list(providers),
        "rejected_provider_count": 0,
        "rejection_summaries": [],
        "omitted_provider_count": 0,
        "partial": False,
    }


def _discovery(*providers: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "1",
        "repository_identity": "sha256:repo",
        "worktree_identity": "sha256:worktree",
        "inventory_fingerprint": "sha256:" + "a" * 64,
        "providers": list(providers),
        "rejected_provider_count": 0,
        "rejection_summaries": [],
        "omitted_provider_count": 0,
        "partial": False,
        "warnings": [],
        "input_bytes": 1,
        "output_bytes": 1,
    }


def _broker_request() -> dict[str, object]:
    return {
        "schema_version": "1",
        "consumer_identity": "fieldbook",
        "repository_identity": "sha256:repo",
        "worktree_identity": "sha256:worktree",
        "required_capability": "symbol-graph",
        "minimum_freshness": "exact",
        "minimum_path_coverage": 1.0,
        "minimum_language_coverage": 1.0,
        "network_acceptable": False,
        "maximum_latency_ms": 10.0,
        "maximum_machine_output_bytes": 16384,
        "maximum_model_output_characters": 2000,
        "preferred_provider": None,
    }


def _consent_request(*, actions: list[str] | None = None) -> dict[str, object]:
    request: dict[str, object] = {
        "schema_version": "1",
        "repository_identity": "sha256:repo",
        "provider_identity": "local.graph",
        "provider_schema_version": "1",
        "actions": actions or ["inspect", "query"],
        "locality": "local",
        "data_surface": "repository-metadata",
        "fallback": "native-level-0",
        "requested_at": FIXED_TIMESTAMP,
    }
    request["digest"] = hashlib.sha256(_canonical(request).encode("utf-8")).hexdigest()
    return request


def _routing_decision(request: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "1",
        "status": "consent-required",
        "selected_provider": None,
        "selection_reason_codes": ["consent-required"],
        "rejected_alternatives": [],
        "eligible_count": 0,
        "rejected_count": 1,
        "omitted_count": 0,
        "consent_requests": [request],
        "escalation_required": True,
        "next_safe_action": "request-provider-consent",
        "model_summary": "decision: consent-required",
        "output_bytes": 1,
        "output_characters": 26,
    }


def _record(
    action: str,
    *,
    repository: str = "sha256:repo",
    provider: str = "local.graph",
    disposition: str = "allow",
    digest: str | None = None,
) -> dict[str, str]:
    return {
        "action": action,
        "repository_identity": repository,
        "provider_identity": provider,
        "provider_schema_version": "1",
        "disposition": disposition,
        "decided_at": FIXED_TIMESTAMP,
        "request_digest": digest or "sha256:" + "a" * 64,
    }


def _state_files(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class ProviderDiscoveryCommandTests(unittest.TestCase):
    def assertInvalid(self, result: tuple[int, str, str]) -> None:
        code, stdout, stderr = result
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertTrue(stderr.startswith("error: "))
        self.assertEqual(len(stderr.rstrip("\n").splitlines()), 1)

    def test_exact_new_command_shapes_are_required_by_argparse(self) -> None:
        invalid_commands = (
            ("providers",),
            ("providers", "discover"),
            ("providers", "route", "--discovery", "d.json"),
            ("consent",),
            ("consent", "request"),
            ("consent", "record", "--request", "r.json", "--decision", "grant"),
            ("consent", "revoke", "--repository-identity", "sha256:repo"),
        )
        for argv in invalid_commands:
            with self.subTest(argv=argv):
                self.assertInvalid(_invoke(*argv))

    def test_discover_merges_stdin_registry_and_registration_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            snapshot = collect_snapshot(repo)
            snapshot_file = root / "snapshot.json"
            _write_json(snapshot_file, snapshot.to_dict())
            state = root / "state"
            registry = _descriptor(source="user-registry")
            _write_json(state / "providers.json", [registry])
            registration = {
                "schema_version": "1",
                "repository_identity": snapshot.repository_identity,
                "providers": [
                    {
                        "provider_identity": "local.graph",
                        "provider_schema_version": "1",
                        "required_capabilities": ["symbol-graph"],
                    }
                ],
            }
            _write_json(repo / ".taf/context/registration.json", registration)
            before = _state_files(state)

            with mock.patch.object(
                socket, "socket", side_effect=AssertionError("network forbidden")
            ), mock.patch.object(
                subprocess, "Popen", side_effect=AssertionError("execution forbidden")
            ):
                code, stdout, stderr = _invoke(
                    "providers",
                    "discover",
                    "--repo",
                    str(repo),
                    "--snapshot",
                    str(snapshot_file),
                    "--inventory",
                    "-",
                    state_home=state,
                    stdin=_canonical(_inventory(_descriptor())),
                )

            self.assertEqual((code, stderr), (0, ""))
            value = json.loads(stdout)
            self.assertEqual(stdout, _canonical(value))
            providers = {item["provider_identity"]: item for item in value["providers"]}
            self.assertEqual(set(providers), {"local.graph", "taf-context"})
            self.assertEqual(providers["local.graph"]["registration"], "project-declared")
            self.assertEqual(
                providers["local.graph"]["discovery_sources"],
                ["host-inventory", "project-registration", "user-registry"],
            )
            self.assertEqual(_state_files(state), before)
            self.assertFalse((state / "consent.json").exists())
            self.assertFalse((state / "audit.jsonl").exists())

    def test_discover_without_inventory_is_native_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            snapshot = collect_snapshot(repo)
            snapshot_file = root / "snapshot.json"
            _write_json(snapshot_file, snapshot.to_dict())
            state = root / "state"

            code, stdout, stderr = _invoke(
                "providers",
                "discover",
                "--repo",
                str(repo),
                "--snapshot",
                str(snapshot_file),
                state_home=state,
            )

            self.assertEqual((code, stderr), (0, ""))
            value = json.loads(stdout)
            self.assertEqual(stdout, _canonical(value))
            self.assertEqual(
                [item["provider_identity"] for item in value["providers"]],
                ["taf-context"],
            )
            self.assertFalse(state.exists())

    def test_discover_ignores_registration_for_another_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            snapshot = collect_snapshot(repo)
            snapshot_file = root / "snapshot.json"
            _write_json(snapshot_file, snapshot.to_dict())
            state = root / "state"
            _write_json(state / "providers.json", [_descriptor(source="user-registry")])
            _write_json(
                repo / ".taf/context/registration.json",
                {
                    "schema_version": "1",
                    "repository_identity": "sha256:another-repository",
                    "providers": [
                        {
                            "provider_identity": "local.graph",
                            "provider_schema_version": "1",
                            "required_capabilities": ["symbol-graph"],
                        }
                    ],
                },
            )

            code, stdout, stderr = _invoke(
                "providers",
                "discover",
                "--repo",
                str(repo),
                "--snapshot",
                str(snapshot_file),
                state_home=state,
            )

            self.assertEqual((code, stderr), (0, ""))
            provider = next(
                item for item in json.loads(stdout)["providers"]
                if item["provider_identity"] == "local.graph"
            )
            self.assertEqual(provider["registration"], "user-registered")
            self.assertNotIn("project-registration", provider["discovery_sources"])

    def test_discover_rejects_oversize_duplicate_inventory_and_bad_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            snapshot = collect_snapshot(repo)
            snapshot_file = root / "snapshot.json"
            _write_json(snapshot_file, snapshot.to_dict())
            oversized = root / "oversized.json"
            oversized.write_bytes(b"{" + b" " * MAX_CONTROL_BYTES)
            duplicate = root / "duplicate.json"
            duplicate.write_text(
                '{"schema_version":"1","schema_version":"1","providers":[],"rejected_provider_count":0,"rejection_summaries":[],"omitted_provider_count":0,"partial":false}',
                encoding="utf-8",
            )
            malformed_snapshot = root / "malformed-snapshot.json"
            malformed_snapshot.write_text('{"schema_version":"1"}', encoding="utf-8")

            for inventory in (oversized, duplicate):
                with self.subTest(inventory=inventory.name):
                    self.assertInvalid(
                        _invoke(
                            "providers",
                            "discover",
                            "--repo",
                            str(repo),
                            "--snapshot",
                            str(snapshot_file),
                            "--inventory",
                            str(inventory),
                            state_home=root / "state",
                        )
                    )
            self.assertInvalid(
                _invoke(
                    "providers",
                    "discover",
                    "--repo",
                    str(repo),
                    "--snapshot",
                    str(malformed_snapshot),
                    state_home=root / "state",
                )
            )

    def test_discover_rejects_snapshot_for_a_different_resolved_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = init_committed_repo(root / "first")
            second = init_committed_repo(root / "second")
            snapshot_file = root / "snapshot.json"
            _write_json(snapshot_file, collect_snapshot(first).to_dict())

            self.assertInvalid(
                _invoke(
                    "providers",
                    "discover",
                    "--repo",
                    str(second),
                    "--snapshot",
                    str(snapshot_file),
                    state_home=root / "state",
                )
            )


class ProviderRoutingCommandTests(unittest.TestCase):
    def assertInvalid(self, result: tuple[int, str, str]) -> None:
        code, stdout, stderr = result
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertTrue(stderr.startswith("error: "))
        self.assertEqual(len(stderr.rstrip("\n").splitlines()), 1)

    def test_route_uses_injected_clock_and_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            discovery = root / "discovery.json"
            request = root / "request.json"
            _write_json(discovery, _discovery(_descriptor()))
            _write_json(request, _broker_request())
            state = root / "state"

            code, stdout, stderr = _invoke(
                "providers",
                "route",
                "--discovery",
                str(discovery),
                "--request",
                str(request),
                state_home=state,
            )

            self.assertEqual((code, stderr), (0, ""))
            value = json.loads(stdout)
            self.assertEqual(stdout, _canonical(value))
            self.assertEqual(value["status"], "consent-required")
            self.assertEqual(len(value["consent_requests"]), 1)
            self.assertEqual(
                value["consent_requests"][0]["requested_at"], FIXED_TIMESTAMP
            )
            self.assertFalse(state.exists())

    def test_route_fails_closed_on_corrupt_consent_without_overwrite_or_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            discovery = root / "discovery.json"
            request = root / "request.json"
            _write_json(discovery, _discovery(_descriptor()))
            _write_json(request, _broker_request())
            state = root / "state"
            state.mkdir()
            corrupt = b'{"schema_version":"2","records":['
            (state / "consent.json").write_bytes(corrupt)

            code, stdout, stderr = _invoke(
                "providers",
                "route",
                "--discovery",
                str(discovery),
                "--request",
                str(request),
                state_home=state,
            )

            self.assertEqual((code, stderr), (0, ""))
            value = json.loads(stdout)
            self.assertEqual(stdout, _canonical(value))
            self.assertEqual(value["status"], "insufficient-context")
            self.assertEqual(value["consent_requests"], [])
            self.assertEqual(value["next_safe_action"], "repair-consent-store")
            reasons = value["rejected_alternatives"][0]["reason_codes"]
            self.assertIn("consent-store-corrupt", reasons)
            self.assertEqual((state / "consent.json").read_bytes(), corrupt)
            self.assertFalse((state / "audit.jsonl").exists())

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_route_treats_symlinked_consent_as_unusable_without_following(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            discovery = root / "discovery.json"
            request = root / "request.json"
            _write_json(discovery, _discovery(_descriptor()))
            _write_json(request, _broker_request())
            state = root / "state"
            state.mkdir()
            outside = root / "outside-consent.json"
            outside_bytes = _canonical(
                {"schema_version": "2", "records": []}
            ).encode("utf-8")
            outside.write_bytes(outside_bytes)
            (state / "consent.json").symlink_to(outside)

            code, stdout, stderr = _invoke(
                "providers", "route", "--discovery", str(discovery),
                "--request", str(request), state_home=state,
            )

            self.assertEqual((code, stderr), (0, ""))
            decision = json.loads(stdout)
            self.assertEqual(decision["status"], "insufficient-context")
            self.assertEqual(decision["consent_requests"], [])
            self.assertEqual(decision["next_safe_action"], "repair-consent-store")
            self.assertIn(
                "consent-store-corrupt",
                decision["rejected_alternatives"][0]["reason_codes"],
            )
            self.assertTrue((state / "consent.json").is_symlink())
            self.assertEqual((state / "consent.json").readlink(), outside)
            self.assertEqual(outside.read_bytes(), outside_bytes)

    def test_route_treats_oversized_consent_as_unusable_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            discovery = root / "discovery.json"
            request = root / "request.json"
            _write_json(discovery, _discovery(_descriptor()))
            _write_json(request, _broker_request())
            state = root / "state"
            state.mkdir()
            oversized = b"{" + b" " * MAX_CONTROL_BYTES
            (state / "consent.json").write_bytes(oversized)

            code, stdout, stderr = _invoke(
                "providers", "route", "--discovery", str(discovery),
                "--request", str(request), state_home=state,
            )

            self.assertEqual((code, stderr), (0, ""))
            decision = json.loads(stdout)
            self.assertEqual(decision["status"], "insufficient-context")
            self.assertEqual(decision["consent_requests"], [])
            self.assertEqual(decision["next_safe_action"], "repair-consent-store")
            self.assertIn(
                "consent-store-corrupt",
                decision["rejected_alternatives"][0]["reason_codes"],
            )
            self.assertEqual((state / "consent.json").read_bytes(), oversized)

    def test_route_treats_unsupported_secure_state_as_unusable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            discovery = root / "discovery.json"
            request = root / "request.json"
            _write_json(discovery, _discovery(_descriptor()))
            _write_json(request, _broker_request())
            state = root / "state"

            with mock.patch(
                "taf_context.provider_state._POSIX_SECURITY_PRIMITIVES", False
            ):
                code, stdout, stderr = _invoke(
                    "providers", "route", "--discovery", str(discovery),
                    "--request", str(request), state_home=state,
                )

            self.assertEqual((code, stderr), (0, ""))
            decision = json.loads(stdout)
            self.assertEqual(decision["status"], "insufficient-context")
            self.assertEqual(decision["consent_requests"], [])
            self.assertEqual(decision["next_safe_action"], "repair-consent-store")
            self.assertIn(
                "consent-store-corrupt",
                decision["rejected_alternatives"][0]["reason_codes"],
            )
            self.assertFalse(state.exists())

    def test_route_rejects_duplicate_and_oversize_control_documents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            discovery = root / "discovery.json"
            request = root / "request.json"
            discovery.write_text(
                '{"schema_version":"1","schema_version":"1"}', encoding="utf-8"
            )
            _write_json(request, _broker_request())
            self.assertInvalid(
                _invoke(
                    "providers", "route", "--discovery", str(discovery),
                    "--request", str(request), state_home=root / "state"
                )
            )

            _write_json(discovery, _discovery(_descriptor()))
            request.write_bytes(b"{" + b" " * MAX_CONTROL_BYTES)
            self.assertInvalid(
                _invoke(
                    "providers", "route", "--discovery", str(discovery),
                    "--request", str(request), state_home=root / "state"
                )
            )


class ConsentCommandTests(unittest.TestCase):
    def assertInvalid(self, result: tuple[int, str, str]) -> None:
        code, stdout, stderr = result
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertTrue(stderr.startswith("error: "))
        self.assertEqual(len(stderr.rstrip("\n").splitlines()), 1)

    def test_request_extracts_exactly_one_request_without_state_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = _consent_request()
            decision = root / "decision.json"
            _write_json(decision, _routing_decision(request))
            state = root / "state"

            code, stdout, stderr = _invoke(
                "consent", "request", "--decision", str(decision), state_home=state
            )

            self.assertEqual((code, stderr), (0, ""))
            self.assertEqual(stdout, _canonical(request))
            self.assertFalse(state.exists())

            no_request = _routing_decision(request)
            no_request["consent_requests"] = []
            no_request["status"] = "insufficient-context"
            no_request["escalation_required"] = False
            no_request["next_safe_action"] = "provide-more-context"
            _write_json(decision, no_request)
            self.assertInvalid(
                _invoke(
                    "consent", "request", "--decision", str(decision), state_home=state
                )
            )

    def test_record_and_revoke_make_only_exact_ledger_and_audit_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            request = _consent_request()
            request_file = root / "request.json"
            _write_json(request_file, request)

            code, stdout, stderr = _invoke(
                "consent",
                "record",
                "--request",
                str(request_file),
                "--decision",
                "allow",
                state_home=state,
            )

            digest = "sha256:" + str(request["digest"])
            expected_ledger = {
                "schema_version": "2",
                "records": [
                    _record("inspect", digest=digest),
                    _record("query", digest=digest),
                ],
            }
            self.assertEqual((code, stderr), (0, ""))
            self.assertEqual(stdout, _canonical(expected_ledger))
            self.assertEqual(
                (state / "consent.json").read_text(encoding="utf-8"),
                _canonical(expected_ledger),
            )
            record_audit = {
                "timestamp": FIXED_TIMESTAMP,
                "request_digest": digest,
                "repository_fingerprint": "sha256:repo",
                "provider_identity": "local.graph",
                "provider_schema_version": "1",
                "actions": ["inspect", "query"],
                "disposition": "allow",
                "operation": "record",
            }
            self.assertEqual(
                (state / "audit.jsonl").read_text(encoding="utf-8"),
                _canonical(record_audit),
            )

            code, stdout, stderr = _invoke(
                "consent",
                "revoke",
                "--repository-identity",
                "sha256:repo",
                "--provider",
                "local.graph",
                "--provider-schema",
                "1",
                "--action",
                "query",
                state_home=state,
            )

            remaining = {
                "schema_version": "2",
                "records": [_record("inspect", digest=digest)],
            }
            revoke_audit = {
                "timestamp": FIXED_TIMESTAMP,
                "request_digest": digest,
                "repository_fingerprint": "sha256:repo",
                "provider_identity": "local.graph",
                "provider_schema_version": "1",
                "actions": ["query"],
                "disposition": "allow",
                "operation": "revoke",
            }
            self.assertEqual((code, stderr), (0, ""))
            self.assertEqual(stdout, _canonical(remaining))
            self.assertEqual(
                (state / "consent.json").read_text(encoding="utf-8"),
                _canonical(remaining),
            )
            self.assertEqual(
                (state / "audit.jsonl").read_text(encoding="utf-8"),
                _canonical(record_audit) + _canonical(revoke_audit),
            )

    def test_record_accepts_exact_deny_and_rejects_tampered_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = _consent_request(actions=["query"])
            request_file = root / "request.json"
            _write_json(request_file, request)
            state = root / "state"

            code, stdout, stderr = _invoke(
                "consent", "record", "--request", str(request_file),
                "--decision", "deny", state_home=state
            )

            digest = "sha256:" + str(request["digest"])
            expected = {
                "schema_version": "2",
                "records": [_record("query", disposition="deny", digest=digest)],
            }
            self.assertEqual((code, stderr), (0, ""))
            self.assertEqual(stdout, _canonical(expected))

            request["fallback"] = "tampered"
            _write_json(request_file, request)
            before = _state_files(state)
            self.assertInvalid(
                _invoke(
                    "consent", "record", "--request", str(request_file),
                    "--decision", "allow", state_home=state
                )
            )
            self.assertEqual(_state_files(state), before)

    def test_opposing_same_second_decisions_retain_fractional_ordering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            request_file = root / "consent-request.json"
            _write_json(request_file, _consent_request(actions=["query"]))

            first = _invoke(
                "consent", "record", "--request", str(request_file),
                "--decision", "allow", state_home=state,
                now=datetime(
                    2026, 8, 26, 12, 34, 56, 100000, tzinfo=timezone.utc
                ),
            )
            second = _invoke(
                "consent", "record", "--request", str(request_file),
                "--decision", "deny", state_home=state,
                now=datetime(
                    2026, 8, 26, 12, 34, 56, 900000, tzinfo=timezone.utc
                ),
            )

            self.assertEqual((first[0], first[2]), (0, ""))
            self.assertEqual((second[0], second[2]), (0, ""))
            records = json.loads(second[1])["records"]
            self.assertEqual(
                [
                    (record["decided_at"], record["disposition"])
                    for record in records
                ],
                [
                    ("2026-08-26T12:34:56.100000Z", "allow"),
                    ("2026-08-26T12:34:56.900000Z", "deny"),
                ],
            )

            discovery = root / "discovery.json"
            broker_request = root / "broker-request.json"
            _write_json(discovery, _discovery(_descriptor()))
            _write_json(broker_request, _broker_request())
            code, stdout, stderr = _invoke(
                "providers", "route", "--discovery", str(discovery),
                "--request", str(broker_request), state_home=state,
            )

            self.assertEqual((code, stderr), (0, ""))
            decision = json.loads(stdout)
            self.assertEqual(decision["status"], "insufficient-context")
            self.assertEqual(decision["consent_requests"], [])
            self.assertIn(
                "consent-query-denied",
                decision["rejected_alternatives"][0]["reason_codes"],
            )

    def test_list_filters_canonical_records_and_does_not_mutate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            ledger = {
                "schema_version": "2",
                "records": [
                    _record("query", repository="sha256:other"),
                    _record("inspect"),
                    _record("query"),
                ],
            }
            _write_json(state / "consent.json", ledger)
            audit = b"existing audit bytes\n"
            (state / "audit.jsonl").write_bytes(audit)
            before = _state_files(state)

            code, stdout, stderr = _invoke(
                "consent",
                "list",
                "--repository-identity",
                "sha256:repo",
                state_home=state,
            )

            expected = {
                "schema_version": "2",
                "records": [_record("inspect"), _record("query")],
            }
            self.assertEqual((code, stderr), (0, ""))
            self.assertEqual(stdout, _canonical(expected))
            self.assertEqual(_state_files(state), before)

    def test_corrupt_consent_blocks_mutation_and_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            corrupt = b'{"schema_version":"2","records":['
            (state / "consent.json").write_bytes(corrupt)
            request_file = root / "request.json"
            _write_json(request_file, _consent_request(actions=["query"]))

            self.assertInvalid(
                _invoke(
                    "consent", "record", "--request", str(request_file),
                    "--decision", "allow", state_home=state
                )
            )
            self.assertEqual((state / "consent.json").read_bytes(), corrupt)
            self.assertFalse((state / "audit.jsonl").exists())

    def test_duplicate_or_oversize_decision_is_rejected_concisely(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decision = root / "decision.json"
            decision.write_text(
                '{"schema_version":"1","schema_version":"1"}', encoding="utf-8"
            )
            self.assertInvalid(
                _invoke(
                    "consent", "request", "--decision", str(decision),
                    state_home=root / "state"
                )
            )
            decision.write_bytes(b"{" + b" " * MAX_CONTROL_BYTES)
            self.assertInvalid(
                _invoke(
                    "consent", "request", "--decision", str(decision),
                    state_home=root / "state"
                )
            )

    def test_explicit_environment_and_clock_override_global_process_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            explicit_state = root / "explicit-state"
            global_state = root / "global-state"
            global_state.mkdir()
            corrupt = b"global state must not be read"
            (global_state / "consent.json").write_bytes(corrupt)
            request_file = root / "request.json"
            request = _consent_request(actions=["query"])
            _write_json(request_file, request)

            with mock.patch.dict(
                os.environ, {"TAF_STATE_HOME": str(global_state)}, clear=True
            ):
                code, stdout, stderr = _invoke_explicit_environment(
                    [
                        "consent", "record", "--request", str(request_file),
                        "--decision", "allow",
                    ],
                    {
                        "HOME": "/must-not-be-used",
                        "TAF_STATE_HOME": str(explicit_state),
                    },
                )

            self.assertEqual((code, stderr), (0, ""))
            self.assertEqual((global_state / "consent.json").read_bytes(), corrupt)
            recorded = json.loads((explicit_state / "consent.json").read_text())
            self.assertEqual(recorded["records"][0]["decided_at"], FIXED_TIMESTAMP)
            self.assertEqual(stdout, _canonical(recorded))


class ProviderExecutionCommandTests(unittest.TestCase):
    @unittest.skipUnless(
        sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file(),
        "macOS sandbox-exec is required for active provider execution",
    )
    def test_execute_inspects_and_queries_one_explicit_authorized_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            snapshot = collect_snapshot(repo)
            adapter = root / "adapter"
            (adapter / "bin").mkdir(parents=True)
            executable = adapter / "bin/provider"
            shutil.copyfile(
                Path(__file__).with_name("fixture_provider.py"), executable
            )
            executable.chmod(0o700)
            snapshot_file = root / "snapshot.json"
            discovery_file = root / "discovery.json"
            request_file = root / "request.json"
            manifest_file = root / "adapter.json"
            _write_json(snapshot_file, snapshot.to_dict())
            provider = _descriptor(
                identity="fixture.graph",
                source="user-registry",
                status_evidence="uninspected",
            )
            provider.update(
                {
                    "provider_version": "2.0.0",
                    "capabilities": ["repository-map", "search-symbols"],
                    "freshness": "unknown",
                    "path_coverage": None,
                    "language_coverage": None,
                    "latency_ms": None,
                    "confidence": "uncertain",
                    "supported_actions": ["inspect", "query"],
                    "required_actions": ["inspect", "query"],
                }
            )
            discovery = _discovery(provider)
            discovery.update(
                {
                    "repository_identity": snapshot.repository_identity,
                    "worktree_identity": snapshot.worktree_identity,
                }
            )
            _write_json(discovery_file, discovery)
            request = request_wire()
            request.update(
                {
                    "provider_identity": "fixture.graph",
                    "repository_identity": snapshot.repository_identity,
                    "worktree_identity": snapshot.worktree_identity,
                    "committed_head": snapshot.head_sha,
                    "dirty_overlay_fingerprint": snapshot.dirty_fingerprint,
                }
            )
            _write_json(request_file, request)
            _write_json(
                manifest_file,
                {
                    "schema_version": "1",
                    "adapter_identity": "fixture.stdio",
                    "adapter_version": "1.0.0",
                    "provider_identity": "fixture.graph",
                    "provider_version": "2.0.0",
                    "executable": "bin/provider",
                    "arguments": ["valid"],
                    "capabilities": ["repository-map", "search-symbols"],
                    "supported_phases": ["describe", "inspect", "query"],
                    "environment_allowlist": [],
                    "locality": "local",
                    "network_required": False,
                },
            )
            state = root / "state"
            _write_json(
                state / "consent.json",
                {
                    "schema_version": "2",
                    "records": [
                        _record(
                            "inspect",
                            repository=snapshot.repository_identity,
                            provider="fixture.graph",
                        ),
                        _record(
                            "query",
                            repository=snapshot.repository_identity,
                            provider="fixture.graph",
                        ),
                    ],
                },
            )

            code, stdout, stderr = _invoke(
                "providers", "execute",
                "--repo", str(repo),
                "--snapshot", str(snapshot_file),
                "--discovery", str(discovery_file),
                "--request", str(request_file),
                "--adapter-manifest", str(manifest_file),
                "--adapter-root", str(adapter),
                state_home=state,
            )

            self.assertEqual((code, stderr), (0, ""))
            execution = json.loads(stdout)
            self.assertEqual(execution["route"], "fixture.graph")
            self.assertEqual(
                [attempt["phase"] for attempt in execution["attempts"]],
                ["inspect", "query"],
            )
            self.assertEqual(
                execution["result"]["provider_identity"], "fixture.graph"
            )
            self.assertNotIn(str(repo), stdout)

    def test_execute_returns_canonical_bounded_result_without_local_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            (repo / "src.py").write_text(
                "class RecoveryDossier:\n    pass\n", encoding="utf-8"
            )
            subprocess.run(
                ["git", "add", "src.py"], cwd=repo, check=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            subprocess.run(
                ["git", "commit", "-m", "add source"], cwd=repo, check=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            snapshot = collect_snapshot(repo)
            snapshot_file = root / "snapshot.json"
            discovery_file = root / "discovery.json"
            request_file = root / "request.json"
            _write_json(snapshot_file, snapshot.to_dict())
            _write_json(
                discovery_file,
                {
                    "schema_version": "1",
                    "repository_identity": snapshot.repository_identity,
                    "worktree_identity": snapshot.worktree_identity,
                    "inventory_fingerprint": "sha256:" + "a" * 64,
                    "providers": [],
                    "rejected_provider_count": 0,
                    "rejection_summaries": [],
                    "omitted_provider_count": 0,
                    "partial": False,
                    "warnings": [],
                    "input_bytes": 1,
                    "output_bytes": 1,
                },
            )
            request = request_wire()
            request.update(
                {
                    "repository_identity": snapshot.repository_identity,
                    "worktree_identity": snapshot.worktree_identity,
                    "committed_head": snapshot.head_sha,
                    "dirty_overlay_fingerprint": snapshot.dirty_fingerprint,
                    "provider_identity": "missing.graph",
                    "filters": {
                        "path_prefixes": [],
                        "languages": [],
                        "symbol_kinds": [],
                        "source_types": [],
                    },
                }
            )
            _write_json(request_file, request)

            code, stdout, stderr = _invoke(
                "providers", "execute",
                "--repo", str(repo),
                "--snapshot", str(snapshot_file),
                "--discovery", str(discovery_file),
                "--request", str(request_file),
                "--allow-fallback",
                state_home=root / "state",
            )

            self.assertEqual((code, stderr), (0, ""))
            execution = json.loads(stdout)
            self.assertEqual(stdout, _canonical(execution))
            self.assertEqual(execution["route"], "bounded-fallback")
            self.assertEqual(
                execution["result"]["provider_identity"],
                "taf.bounded-fallback",
            )
            self.assertEqual(execution["result"]["returned_count"], 1)
            self.assertNotIn(str(repo), stdout)

    def test_execute_requires_explicit_paired_adapter_roots(self) -> None:
        code, stdout, stderr = _invoke(
            "providers", "execute",
            "--repo", "/missing",
            "--snapshot", "/missing-snapshot",
            "--discovery", "/missing-discovery",
            "--request", "/missing-request",
            "--adapter-manifest", "/missing-manifest",
        )

        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("adapter manifest and root counts must match", stderr)


class CompatibilityAndPublicAPITests(unittest.TestCase):
    def test_snapshot_and_status_outputs_remain_byte_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            output = root / "artifacts"

            code, snapshot_stdout, stderr = _invoke(
                "snapshot", "--repo", str(repo), "--output-dir", str(output),
                state_home=root / "state"
            )
            self.assertEqual((code, stderr), (0, ""))
            snapshot_summary = json.loads(snapshot_stdout)
            self.assertEqual(snapshot_stdout, _canonical(snapshot_summary))
            self.assertEqual(
                set(snapshot_summary),
                {
                    "artifacts", "freshness", "path_coverage", "storage_bytes",
                    "dossier_characters",
                },
            )

            code, status_stdout, stderr = _invoke(
                "status", "--repo", str(repo),
                "--manifest", str(output / "manifest.json"),
                state_home=root / "state"
            )
            expected_status = {
                "freshness": "exact",
                "reasons": ["exact-match"],
                "can_incrementally_update": False,
                "requires_rebuild": False,
            }
            self.assertEqual((code, stderr), (0, ""))
            self.assertEqual(status_stdout, _canonical(expected_status))

    def test_provider_cli_public_api_is_exported(self) -> None:
        source_root = Path(__file__).parents[2] / "tools" / "taf-context"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from taf_context import register_provider_commands, "
                    "run_provider_command; "
                    "assert callable(register_provider_commands); "
                    "assert callable(run_provider_command)"
                ),
            ],
            env={"PYTHONPATH": str(source_root), "PYTHONNOUSERSITE": "1"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
