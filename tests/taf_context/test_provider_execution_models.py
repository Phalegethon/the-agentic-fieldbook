"""Strict wire-model tests for active provider execution."""

from __future__ import annotations

import json
import unittest

from taf_context.provider_execution_models import (
    AdapterManifest,
    AttemptRecord,
    ExecutionPolicy,
    InspectionRecord,
    ProviderExecutionModelError,
    parse_adapter_manifest,
    parse_inspection_record,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def valid_manifest() -> dict[str, object]:
    return {
        "schema_version": "1",
        "adapter_identity": "fixture.stdio",
        "adapter_version": "1.0.0",
        "provider_identity": "fixture.graph",
        "provider_version": "2.0.0",
        "executable": "bin/fixture-provider",
        "arguments": ["--stdio"],
        "capabilities": ["repository-map", "search-symbols"],
        "supported_phases": ["describe", "inspect", "query"],
        "environment_allowlist": ["LANG"],
        "locality": "local",
        "network_required": False,
    }


def valid_inspection() -> dict[str, object]:
    return {
        "schema_version": "1",
        "adapter_identity": "fixture.stdio",
        "provider_identity": "fixture.graph",
        "provider_version": "2.0.0",
        "repository_identity": SHA_A,
        "worktree_identity": SHA_B,
        "committed_head": "1" * 40,
        "dirty_overlay_fingerprint": SHA_C,
        "index_identity": "d" * 64,
        "readiness": "ready",
        "capabilities": ["repository-map", "search-symbols"],
        "path_coverage": 1.0,
        "language_coverage": 0.75,
        "storage_bytes": 4096,
        "reason_codes": [],
        "warnings": [],
    }


class AdapterManifestTests(unittest.TestCase):
    def test_manifest_round_trip_is_exact(self) -> None:
        manifest = AdapterManifest.from_dict(valid_manifest())
        self.assertEqual(AdapterManifest.from_dict(manifest.to_dict()), manifest)
        self.assertEqual(parse_adapter_manifest(json.dumps(valid_manifest())), manifest)

    def test_manifest_rejects_unsafe_paths_ordering_and_unknown_fields(self) -> None:
        variants = []
        for executable in ("/tmp/provider", "../provider", "bin/../provider"):
            value = valid_manifest()
            value["executable"] = executable
            variants.append(value)
        value = valid_manifest()
        value["capabilities"] = ["search-symbols", "repository-map"]
        variants.append(value)
        value = valid_manifest()
        value["surprise"] = True
        variants.append(value)
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(ProviderExecutionModelError):
                AdapterManifest.from_dict(variant)

    def test_manifest_parser_rejects_duplicate_nonfinite_and_oversized_json(self) -> None:
        hostile = (
            b'{"schema_version":"1","schema_version":"1"}',
            b'{"schema_version":NaN}',
            b"{" + b'"padding":"' + (b"x" * (256 * 1024)) + b'"}',
        )
        for wire in hostile:
            with self.subTest(size=len(wire)), self.assertRaises(ProviderExecutionModelError):
                parse_adapter_manifest(wire)


class InspectionRecordTests(unittest.TestCase):
    def test_inspection_round_trip_preserves_binding_and_coverage(self) -> None:
        record = InspectionRecord.from_dict(valid_inspection())
        self.assertEqual(InspectionRecord.from_dict(record.to_dict()), record)
        self.assertEqual(parse_inspection_record(json.dumps(valid_inspection())), record)

    def test_inspection_rejects_bad_digests_claims_and_counters(self) -> None:
        variants = []
        for field, value in (
            ("repository_identity", "not-a-digest"),
            ("committed_head", "123"),
            ("path_coverage", 1.1),
            ("storage_bytes", -1),
        ):
            candidate = valid_inspection()
            candidate[field] = value
            variants.append(candidate)
        candidate = valid_inspection()
        candidate["readiness"] = "corrupt"
        candidate["reason_codes"] = []
        variants.append(candidate)
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(ProviderExecutionModelError):
                InspectionRecord.from_dict(variant)


class ExecutionPolicyAndAttemptTests(unittest.TestCase):
    def test_policy_enforces_fixed_output_and_timeout_ceilings(self) -> None:
        policy = ExecutionPolicy.from_dict(
            {
                "schema_version": "1",
                "timeout_seconds": 10.0,
                "maximum_stdout_bytes": 262144,
                "maximum_stderr_bytes": 65536,
                "network_allowed": False,
                "fallback_allowed": True,
                "maximum_inspections": 2,
            }
        )
        self.assertEqual(ExecutionPolicy.from_dict(policy.to_dict()), policy)
        for field, value in (
            ("timeout_seconds", 121),
            ("maximum_stdout_bytes", 262145),
            ("maximum_stderr_bytes", 65537),
            ("maximum_inspections", 4),
        ):
            wire = policy.to_dict()
            wire[field] = value
            with self.subTest(field=field), self.assertRaises(ProviderExecutionModelError):
                ExecutionPolicy.from_dict(wire)

    def test_attempt_record_is_bounded_and_contains_no_diagnostics(self) -> None:
        value = {
            "schema_version": "1",
            "provider_identity": "fixture.graph",
            "phase": "inspect",
            "status": "failed",
            "reason_codes": ["provider-timeout"],
            "elapsed_ns": 123,
            "stdout_bytes": 0,
            "stderr_bytes": 0,
        }
        record = AttemptRecord.from_dict(value)
        self.assertEqual(record.to_dict(), value)
        value["stderr"] = "/Users/example secret=abc"
        with self.assertRaises(ProviderExecutionModelError):
            AttemptRecord.from_dict(value)


if __name__ == "__main__":
    unittest.main()
