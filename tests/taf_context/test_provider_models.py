"""Wire-contract tests for provider discovery and routing records."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import unittest

from taf_context.models import ContextAction, RepositorySnapshot, canonical_json
from taf_context.provider_models import (
    Availability,
    BrokerRequest,
    ConsentRequest,
    DiscoverySnapshot,
    DiscoverySource,
    HostInventory,
    ProjectRegistration,
    ProviderDescriptor,
    ProviderLocality,
    Registration,
    RoutingDecision,
    RoutingStatus,
    StatusEvidence,
    parse_host_inventory,
)


DESCRIPTOR = {
    "schema_version": "1", "provider_identity": "example.provider",
    "provider_version": "1.2.3", "provider_schema_version": "1",
    "capabilities": ["repository-map", "status"], "locality": "local",
    "discovery_sources": ["host-inventory", "native"],
    "availability": "available", "registration": "native",
    "status_evidence": "provider-inspected", "freshness": "exact",
    "path_coverage": 1.0, "language_coverage": 1.0, "latency_ms": 1.5,
    "confidence": "verified", "supported_actions": ["inspect", "query"],
    "required_actions": ["inspect"], "marker_hints": ["pyproject.toml"],
    "reason_codes": ["available"], "warnings": [],
}


class ProviderDescriptorTests(unittest.TestCase):
    def test_exact_round_trip_uses_stable_wire_values(self) -> None:
        descriptor = ProviderDescriptor.from_dict(DESCRIPTOR)
        self.assertEqual(descriptor.to_dict(), DESCRIPTOR)
        self.assertEqual(descriptor.capabilities, ("repository-map", "status"))
        self.assertIs(descriptor.locality, ProviderLocality.LOCAL)
        self.assertIn(ContextAction.QUERY, descriptor.supported_actions)
        self.assertEqual(canonical_json(descriptor.to_dict()), canonical_json(DESCRIPTOR))

    def test_rejects_schema_shape_and_executable_fields(self) -> None:
        cases = []
        missing = copy.deepcopy(DESCRIPTOR); del missing["provider_version"]
        cases.append(missing)
        unknown = copy.deepcopy(DESCRIPTOR); unknown["command"] = ["run"]
        cases.append(unknown)
        version = copy.deepcopy(DESCRIPTOR); version["schema_version"] = "2"
        cases.append(version)
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    ProviderDescriptor.from_dict(value)

    def test_rejects_invalid_bounded_set_and_evidence_values(self) -> None:
        cases = []
        unsorted = copy.deepcopy(DESCRIPTOR); unsorted["capabilities"] = ["status", "repository-map"]
        cases.append(unsorted)
        duplicate = copy.deepcopy(DESCRIPTOR); duplicate["capabilities"] = ["status", "status"]
        cases.append(duplicate)
        too_many = copy.deepcopy(DESCRIPTOR); too_many["capabilities"] = [f"cap-{i:02d}" for i in range(65)]
        cases.append(too_many)
        marker = copy.deepcopy(DESCRIPTOR); marker["marker_hints"] = ["../secret"]
        cases.append(marker)
        absolute = copy.deepcopy(DESCRIPTOR); absolute["marker_hints"] = ["/secret"]
        cases.append(absolute)
        drive_qualified = copy.deepcopy(DESCRIPTOR); drive_qualified["marker_hints"] = ["C:/secret"]
        cases.append(drive_qualified)
        huge = copy.deepcopy(DESCRIPTOR); huge["provider_version"] = "x" * 257
        cases.append(huge)
        truth = copy.deepcopy(DESCRIPTOR); truth["latency_ms"] = True
        cases.append(truth)
        nonfinite = copy.deepcopy(DESCRIPTOR); nonfinite["path_coverage"] = math.nan
        cases.append(nonfinite)
        markers = copy.deepcopy(DESCRIPTOR); markers["marker_hints"] = [f"m{i}" for i in range(17)]
        cases.append(markers)
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    ProviderDescriptor.from_dict(value)


class HostInventoryTests(unittest.TestCase):
    def test_parse_tolerates_one_bad_descriptor_but_not_bad_envelope(self) -> None:
        envelope = {
            "schema_version": "1", "providers": [DESCRIPTOR, {"bad": True}],
            "rejected_provider_count": 0, "rejection_summaries": [],
            "omitted_provider_count": 0, "partial": False,
        }
        result = parse_host_inventory(json.dumps(envelope))
        self.assertEqual(result.inventory.providers, (ProviderDescriptor.from_dict(DESCRIPTOR),))
        self.assertEqual(result.inventory.rejected_provider_count, 1)
        self.assertEqual(result.input_bytes, len(json.dumps(envelope).encode("utf-8")))
        for malformed in ('{"schema_version":"1","schema_version":"1"}', '{bad', json.dumps({**envelope, "command": "run"})):
            with self.subTest(malformed=malformed):
                with self.assertRaises(ValueError):
                    parse_host_inventory(malformed)

    def test_rejects_inventory_larger_than_256_kib_before_json_parsing(self) -> None:
        envelope = {
            "schema_version": "1", "providers": [], "rejected_provider_count": 0,
            "rejection_summaries": [], "omitted_provider_count": 0, "partial": False,
        }
        oversized = json.dumps(envelope) + (" " * (256 * 1024))
        with self.assertRaises(ValueError):
            parse_host_inventory(oversized)

    def test_rejects_more_than_64_descriptors_directly(self) -> None:
        value = {"schema_version": "1", "providers": [DESCRIPTOR] * 65,
                 "rejected_provider_count": 0, "rejection_summaries": [],
                 "omitted_provider_count": 0, "partial": False}
        with self.assertRaises(ValueError):
            HostInventory.from_dict(value)


class OtherRecordTests(unittest.TestCase):
    def test_project_snapshot_and_request_round_trip(self) -> None:
        registration = {"schema_version": "1", "repository_identity": "sha256:repo", "providers": [{"provider_identity": "example.provider", "provider_schema_version": "1", "required_capabilities": ["repository-map"]}]}
        self.assertEqual(ProjectRegistration.from_dict(registration).to_dict(), registration)
        snapshot = {"schema_version": "1", "repository_identity": "sha256:repo", "worktree_identity": "sha256:worktree", "inventory_fingerprint": "sha256:inventory", "providers": [DESCRIPTOR], "rejected_provider_count": 0, "rejection_summaries": [], "omitted_provider_count": 0, "partial": False, "warnings": [], "input_bytes": 1, "output_bytes": 2}
        self.assertEqual(DiscoverySnapshot.from_dict(snapshot).to_dict(), snapshot)
        request = {"schema_version": "1", "consumer_identity": "consumer", "repository_identity": "sha256:repo", "worktree_identity": "sha256:worktree", "required_capability": "repository-map", "minimum_freshness": "exact", "minimum_path_coverage": 1.0, "minimum_language_coverage": 1.0, "network_acceptable": False, "maximum_latency_ms": 2.0, "maximum_machine_output_bytes": 16384, "maximum_model_output_characters": 2000, "preferred_provider": None}
        self.assertEqual(BrokerRequest.from_dict(request).to_dict(), request)

    def test_consent_digest_is_canonical_and_rejects_tampering(self) -> None:
        consent = ConsentRequest.create(schema_version="1", repository_identity="sha256:repo", provider_identity="example.provider", provider_schema_version="1", actions=(ContextAction.INSPECT,), locality=ProviderLocality.LOCAL, data_surface="repository-metadata", fallback="native", requested_at="2026-08-26T00:00:00Z")
        wire = consent.to_dict()
        self.assertEqual(wire["digest"], hashlib.sha256(canonical_json({k: v for k, v in wire.items() if k != "digest"}).encode("utf-8")).hexdigest())
        self.assertEqual(ConsentRequest.from_dict(wire), consent)
        wire["digest"] = "0" * 64
        with self.assertRaises(ValueError):
            ConsentRequest.from_dict(wire)

    def test_routing_decision_round_trip_and_strict_enums(self) -> None:
        wire = {"schema_version": "1", "status": "selected", "selected_provider": "example.provider", "selection_reason_codes": ["eligible"], "rejected_alternatives": [{"provider_identity": "other.provider", "reason_codes": ["stale"]}], "eligible_count": 1, "rejected_count": 1, "omitted_count": 0, "consent_requests": [], "escalation_required": False, "next_safe_action": "query", "model_summary": "selected", "output_bytes": 1, "output_characters": 8}
        decision = RoutingDecision.from_dict(wire)
        self.assertEqual(decision.to_dict(), wire)
        self.assertIs(decision.status, RoutingStatus.SELECTED)
        self.assertEqual(Availability.AVAILABLE.value, "available")
        self.assertEqual(DiscoverySource.NATIVE.value, "native")
        self.assertEqual(Registration.NATIVE.value, "native")
        self.assertEqual(StatusEvidence.UNINSPECTED.value, "uninspected")

    def test_repository_snapshot_loads_only_strict_portable_wire(self) -> None:
        wire = {name: getattr(_snapshot(), name) for name in RepositorySnapshot.__dataclass_fields__}
        wire["tracked_paths"] = ["README.md"]
        wire["staged_paths"] = []; wire["unstaged_paths"] = []; wire["untracked_paths"] = []
        wire["language_counts"] = {"Python": 1}; wire["candidate_artifacts"] = []
        wire["provider_markers"] = []; wire["warnings"] = []
        loaded = RepositorySnapshot.from_dict(wire)
        self.assertEqual(loaded.to_dict(), wire)
        wire["insertions"] = True
        with self.assertRaises(ValueError):
            RepositorySnapshot.from_dict(wire)


def _snapshot() -> RepositorySnapshot:
    return RepositorySnapshot("1", "sha256:repo", "root", "sha256:root", "git", "common", "sha256:common", "sha256:worktree", "a" * 40, None, "sha256:clean", True, ("README.md",), (), (), (), 0, 0, 0, 0, (("Python", 1),), (), (), 0, 0, 0, ())


if __name__ == "__main__":
    unittest.main()
