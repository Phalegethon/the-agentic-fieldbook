"""Behavior tests for pure, passive provider discovery."""

from __future__ import annotations

import builtins
import io
import itertools
import socket
import subprocess
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from taf_context.discovery import discover_providers, native_level0_descriptor
from taf_context.models import Confidence, ContextAction, Freshness, RepositorySnapshot
from taf_context.provider_models import (
    Availability,
    DiscoverySnapshot,
    DiscoverySource,
    HostInventory,
    ProjectRegistration,
    ProjectRegistrationEntry,
    ProviderDescriptor,
    ProviderLocality,
    Registration,
    StatusEvidence,
)


class PassiveDiscoveryTests(unittest.TestCase):
    def test_native_only_descriptor_is_exact_and_partial_only_for_incomplete_dirty_state(self) -> None:
        complete = _snapshot(warnings=("snapshot-warning",))
        partial = replace(complete, dirty_fingerprint_complete=False)

        with _forbid_active_discovery():
            exact = native_level0_descriptor(complete)
            incomplete = native_level0_descriptor(partial)
            discovered = discover_providers(complete, _inventory(), (), None)

        self.assertEqual(
            exact,
            ProviderDescriptor(
                schema_version="1",
                provider_identity="taf-context",
                provider_version="0.1.0",
                provider_schema_version="1",
                capabilities=("repository-map", "status"),
                locality=ProviderLocality.LOCAL,
                discovery_sources=(DiscoverySource.NATIVE,),
                availability=Availability.AVAILABLE,
                registration=Registration.NATIVE,
                status_evidence=StatusEvidence.MANIFEST_VALIDATED,
                freshness=Freshness.EXACT,
                path_coverage=1.0,
                language_coverage=None,
                latency_ms=None,
                confidence=Confidence.VERIFIED,
                supported_actions=(ContextAction.QUERY,),
                required_actions=(),
                marker_hints=(),
                reason_codes=(),
                warnings=("snapshot-warning",),
            ),
        )
        self.assertIs(incomplete.freshness, Freshness.PARTIAL)
        self.assertEqual(discovered.providers, (exact,))
        self.assertEqual(discovered.repository_identity, complete.repository_identity)
        self.assertEqual(discovered.worktree_identity, complete.worktree_identity)
        self.assertEqual(discovered.rejected_provider_count, 0)
        self.assertFalse(discovered.partial)
        self.assertTrue(discovered.inventory_fingerprint.startswith("sha256:"))
        self.assertEqual(len(discovered.inventory_fingerprint), 71)
        self.assertEqual(ProviderDescriptor.from_dict(exact.to_dict()), exact)
        self.assertEqual(
            DiscoverySnapshot.from_dict(discovered.to_dict()), discovered
        )

    def test_source_overlays_do_not_invent_availability_or_consent(self) -> None:
        host = _descriptor("host.provider", source=DiscoverySource.HOST_INVENTORY)
        registered = _descriptor("registered.provider", source=DiscoverySource.USER_REGISTRY)
        declared = _descriptor(
            "declared.provider",
            source=DiscoverySource.USER_REGISTRY,
            availability=Availability.UNKNOWN,
            freshness=Freshness.UNKNOWN,
            required_actions=(ContextAction.INSTALL,),
        )
        registration = _registration(
            _registration_entry("declared.provider", required=("repository-map",))
        )

        with _forbid_active_discovery():
            result = discover_providers(
                _snapshot(), _inventory(host), (registered, declared), registration
            )

        providers = _by_identity(result.providers)
        self.assertIs(providers["host.provider"].availability, Availability.AVAILABLE)
        self.assertIs(providers["host.provider"].registration, Registration.UNREGISTERED)
        self.assertIs(providers["registered.provider"].registration, Registration.USER_REGISTERED)
        self.assertIs(providers["declared.provider"].availability, Availability.UNKNOWN)
        self.assertIs(providers["declared.provider"].registration, Registration.PROJECT_DECLARED)
        self.assertEqual(providers["declared.provider"].required_actions, (ContextAction.INSTALL,))
        self.assertIn(
            DiscoverySource.PROJECT_REGISTRATION,
            providers["declared.provider"].discovery_sources,
        )

    def test_registration_without_a_descriptor_is_rejected_evidence_only(self) -> None:
        registration = _registration(_registration_entry("missing.provider"))

        with _forbid_active_discovery():
            result = discover_providers(_snapshot(), _inventory(), (), registration)

        self.assertEqual(_identities(result.providers), ("taf-context",))
        self.assertEqual(result.rejected_provider_count, 1)
        self.assertEqual(
            result.rejection_summaries,
            ("missing.provider:registration-provider-unavailable",),
        )

    def test_literal_marker_matching_uses_only_materialized_path_metadata(self) -> None:
        snapshot = _snapshot(
            tracked_paths=("README.md", "tracked.marker"),
            untracked_paths=("untracked.marker",),
            provider_markers=("inventory.marker",),
        )
        providers = (
            _descriptor("inventory.match", marker_hints=("inventory.marker",)),
            _descriptor("literal.nonmatch", marker_hints=("tracked.marker.backup",)),
            _descriptor("tracked.match", marker_hints=("tracked.marker",)),
            _descriptor("untracked.match", marker_hints=("untracked.marker",)),
        )

        with _forbid_active_discovery():
            result = discover_providers(snapshot, _inventory(), providers, None)

        found = _by_identity(result.providers)
        for identity in ("inventory.match", "tracked.match", "untracked.match"):
            self.assertIn(DiscoverySource.PROJECT_MARKER, found[identity].discovery_sources)
            self.assertIn("marker-match", found[identity].reason_codes)
        self.assertNotIn(
            DiscoverySource.PROJECT_MARKER,
            found["literal.nonmatch"].discovery_sources,
        )
        self.assertNotIn("marker-match", found["literal.nonmatch"].reason_codes)

    def test_marker_only_candidate_stays_candidate_and_unusable(self) -> None:
        marker_only = _descriptor(
            "marker.provider",
            availability=Availability.CANDIDATE,
            freshness=Freshness.UNUSABLE,
            confidence=Confidence.UNCERTAIN,
            marker_hints=(".marker",),
        )

        with _forbid_active_discovery():
            result = discover_providers(
                _snapshot(provider_markers=(".marker",)),
                _inventory(),
                (marker_only,),
                None,
            )

        descriptor = _by_identity(result.providers)["marker.provider"]
        self.assertIs(descriptor.availability, Availability.CANDIDATE)
        self.assertIs(descriptor.freshness, Freshness.UNUSABLE)
        self.assertIs(descriptor.confidence, Confidence.UNCERTAIN)
        self.assertIn(DiscoverySource.PROJECT_MARKER, descriptor.discovery_sources)

    def test_marker_matching_considers_all_compatible_claim_hints_before_bounding_output(self) -> None:
        host = _descriptor(
            "many-markers.provider",
            source=DiscoverySource.HOST_INVENTORY,
            marker_hints=tuple(f"a-marker-{index:02d}" for index in range(16)),
        )
        registry = _descriptor(
            "many-markers.provider",
            marker_hints=tuple(f"z-marker-{index:02d}" for index in range(16)),
        )

        with _forbid_active_discovery():
            result = discover_providers(
                _snapshot(provider_markers=("z-marker-15",)),
                _inventory(host),
                (registry,),
                None,
            )

        descriptor = _by_identity(result.providers)["many-markers.provider"]
        self.assertEqual(len(descriptor.marker_hints), 16)
        self.assertIn(DiscoverySource.PROJECT_MARKER, descriptor.discovery_sources)
        self.assertIn("marker-match", descriptor.reason_codes)

    def test_compatible_host_and_registry_claims_merge_deterministically(self) -> None:
        host = _descriptor(
            "merged.provider",
            source=DiscoverySource.HOST_INVENTORY,
            status_evidence=StatusEvidence.PROVIDER_INSPECTED,
            latency_ms=2.0,
        )
        registry = _descriptor(
            "merged.provider",
            source=DiscoverySource.USER_REGISTRY,
            availability=Availability.UNKNOWN,
            freshness=Freshness.UNKNOWN,
            status_evidence=StatusEvidence.UNINSPECTED,
            latency_ms=None,
        )

        with _forbid_active_discovery():
            result = discover_providers(_snapshot(), _inventory(host), (registry,), None)

        merged = _by_identity(result.providers)["merged.provider"]
        self.assertEqual(
            merged.discovery_sources,
            (DiscoverySource.HOST_INVENTORY, DiscoverySource.USER_REGISTRY),
        )
        self.assertIs(merged.registration, Registration.USER_REGISTERED)
        self.assertIs(merged.availability, Availability.AVAILABLE)
        self.assertIs(merged.status_evidence, StatusEvidence.PROVIDER_INSPECTED)
        self.assertEqual(merged.latency_ms, 2.0)

    def test_authoritative_claim_conflicts_reject_the_provider_identity(self) -> None:
        host = _descriptor("conflict.provider", source=DiscoverySource.HOST_INVENTORY)
        conflicts = {
            "provider-version-conflict": replace(host, provider_version="2.0.0"),
            "provider-schema-version-conflict": replace(host, provider_schema_version="2"),
            "provider-locality-conflict": replace(
                host, locality=ProviderLocality.NETWORK_BACKED
            ),
            "provider-capabilities-conflict": replace(host, capabilities=("status",)),
        }

        for reason, conflicting in conflicts.items():
            registry = replace(
                conflicting, discovery_sources=(DiscoverySource.USER_REGISTRY,)
            )
            with self.subTest(reason=reason), _forbid_active_discovery():
                result = discover_providers(
                    _snapshot(), _inventory(host), (registry,), None
                )
            self.assertNotIn("conflict.provider", _identities(result.providers))
            self.assertEqual(result.rejected_provider_count, 1)
            self.assertEqual(
                result.rejection_summaries, (f"conflict.provider:{reason}",)
            )
            self.assertTrue(result.partial)
            self.assertIn("inventory-partial", result.warnings)

    def test_registration_mismatch_rejects_only_the_overlay(self) -> None:
        host = _descriptor("compatible.provider", source=DiscoverySource.HOST_INVENTORY)
        cases = (
            (
                _registration_entry("compatible.provider", schema="2"),
                "registration-schema-version-mismatch",
            ),
            (
                _registration_entry("compatible.provider", required=("semantic-search",)),
                "registration-required-capability-mismatch",
            ),
        )

        for entry, reason in cases:
            with self.subTest(reason=reason), _forbid_active_discovery():
                result = discover_providers(
                    _snapshot(), _inventory(host), (), _registration(entry)
                )
            descriptor = _by_identity(result.providers)["compatible.provider"]
            self.assertIs(descriptor.registration, Registration.UNREGISTERED)
            self.assertNotIn(
                DiscoverySource.PROJECT_REGISTRATION, descriptor.discovery_sources
            )
            self.assertEqual(result.rejected_provider_count, 1)
            self.assertEqual(
                result.rejection_summaries,
                (f"compatible.provider:{reason}",),
            )

    def test_reserved_native_identity_and_invalid_host_source_are_rejected(self) -> None:
        native_collision = _descriptor(
            "taf-context", source=DiscoverySource.HOST_INVENTORY
        )
        invalid_source = _descriptor(
            "source.spoof", source=DiscoverySource.NATIVE
        )

        with _forbid_active_discovery():
            result = discover_providers(
                _snapshot(), _inventory(native_collision, invalid_source), (), None
            )

        self.assertEqual(_identities(result.providers), ("taf-context",))
        self.assertEqual(result.rejected_provider_count, 2)
        self.assertEqual(
            result.rejection_summaries,
            (
                "source.spoof:invalid-host-discovery-source",
                "taf-context:reserved-provider-identity",
            ),
        )

    def test_spoofed_host_claim_taints_same_identity_valid_registry_claim(self) -> None:
        spoofed_host = _descriptor(
            "tainted.provider", source=DiscoverySource.USER_REGISTRY
        )
        valid_registry = _descriptor("tainted.provider")

        with _forbid_active_discovery():
            result = discover_providers(
                _snapshot(), _inventory(spoofed_host), (valid_registry,), None
            )

        self.assertNotIn("tainted.provider", _identities(result.providers))
        self.assertEqual(result.rejected_provider_count, 1)
        self.assertEqual(
            result.rejection_summaries,
            ("tainted.provider:invalid-host-discovery-source",),
        )

    def test_sixty_five_providers_truncate_by_identity_with_exact_omission(self) -> None:
        registry = tuple(
            _descriptor(f"z-provider-{index:02d}") for index in reversed(range(64))
        )

        with _forbid_active_discovery():
            result = discover_providers(_snapshot(), _inventory(), registry, None)

        self.assertEqual(len(result.providers), 64)
        self.assertEqual(result.providers[0].provider_identity, "taf-context")
        self.assertEqual(result.providers[-1].provider_identity, "z-provider-62")
        self.assertEqual(result.omitted_provider_count, 1)
        self.assertTrue(result.partial)
        self.assertIn("inventory-partial", result.warnings)

        changed_omitted = tuple(
            replace(item, provider_version="9.9.9")
            if item.provider_identity == "z-provider-63"
            else item
            for item in registry
        )
        with _forbid_active_discovery():
            changed = discover_providers(
                _snapshot(), _inventory(), changed_omitted, None
            )
        self.assertNotEqual(result.inventory_fingerprint, changed.inventory_fingerprint)

    def test_equivalent_input_permutations_are_byte_identical(self) -> None:
        host_items = (
            _descriptor("alpha.provider", source=DiscoverySource.HOST_INVENTORY),
            _descriptor("beta.provider", source=DiscoverySource.HOST_INVENTORY),
        )
        registry_items = (
            _descriptor("alpha.provider"),
            _descriptor("gamma.provider", marker_hints=(".gamma",)),
        )
        registration_items = (
            _registration_entry("alpha.provider"),
            _registration_entry("gamma.provider"),
        )
        wires = []

        for reverse_host, reverse_registry, reverse_registration in itertools.product(
            (False, True), repeat=3
        ):
            inventory = _inventory(
                *(reversed(host_items) if reverse_host else host_items),
                rejection_summaries=("upstream-b", "upstream-a")
                if reverse_host
                else ("upstream-a", "upstream-b"),
                rejected_provider_count=2,
                partial=True,
            )
            registry = tuple(
                reversed(registry_items) if reverse_registry else registry_items
            )
            registration = _registration(
                *(reversed(registration_items)
                  if reverse_registration
                  else registration_items)
            )
            with _forbid_active_discovery():
                result = discover_providers(
                    _snapshot(provider_markers=(".gamma",)),
                    inventory,
                    registry,
                    registration,
                )
            wires.append(result.to_dict())

        self.assertTrue(all(wire == wires[0] for wire in wires[1:]))

    def test_upstream_rejection_and_omission_counts_remain_exact_and_summaries_bounded(self) -> None:
        inventory = _inventory(
            rejected_provider_count=70,
            rejection_summaries=tuple(f"upstream-{index:02d}" for index in range(64)),
            omitted_provider_count=3,
            partial=True,
        )
        missing = _registration(_registration_entry("missing.provider"))

        with _forbid_active_discovery():
            result = discover_providers(_snapshot(), inventory, (), missing)

        self.assertEqual(result.rejected_provider_count, 71)
        self.assertEqual(result.omitted_provider_count, 3)
        self.assertEqual(len(result.rejection_summaries), 64)
        self.assertEqual(result.rejection_summaries, tuple(sorted(result.rejection_summaries)))
        self.assertTrue(result.partial)
        self.assertIn("inventory-partial", result.warnings)

    def test_exact_wire_counter_ceiling_is_portable_without_local_increments(self) -> None:
        ceiling = 2**31 - 1

        with _forbid_active_discovery():
            result = discover_providers(
                _snapshot(),
                _inventory(
                    rejected_provider_count=ceiling,
                    omitted_provider_count=ceiling,
                    partial=True,
                ),
                (),
                None,
            )

        self.assertEqual(result.rejected_provider_count, ceiling)
        self.assertEqual(result.omitted_provider_count, ceiling)
        self.assertEqual(DiscoverySnapshot.from_dict(result.to_dict()), result)

    def test_exact_rejection_count_overflow_fails_closed(self) -> None:
        inventory = _inventory(rejected_provider_count=2**31 - 1, partial=True)
        registration = _registration(_registration_entry("missing.provider"))

        with _forbid_active_discovery(), self.assertRaisesRegex(
            ValueError, "^rejected-provider-count-overflow$"
        ):
            discover_providers(_snapshot(), inventory, (), registration)

    def test_exact_omission_count_overflow_fails_closed(self) -> None:
        inventory = _inventory(omitted_provider_count=2**31 - 1, partial=True)
        registry = tuple(
            _descriptor(f"z-provider-{index:02d}") for index in range(64)
        )

        with _forbid_active_discovery(), self.assertRaisesRegex(
            ValueError, "^omitted-provider-count-overflow$"
        ):
            discover_providers(_snapshot(), inventory, registry, None)

    def test_inventory_partial_warning_survives_a_full_snapshot_warning_budget(self) -> None:
        snapshot = _snapshot(
            warnings=tuple(f"a-warning-{index:02d}" for index in range(64))
        )

        with _forbid_active_discovery():
            result = discover_providers(
                snapshot,
                _inventory(rejected_provider_count=1, partial=True),
                (),
                None,
            )

        self.assertEqual(len(result.warnings), 64)
        self.assertIn("inventory-partial", result.warnings)

    def test_output_byte_count_describes_the_final_canonical_wire(self) -> None:
        with _forbid_active_discovery():
            result = discover_providers(_snapshot(), _inventory(), (), None)

        from taf_context.models import canonical_json

        self.assertEqual(
            result.output_bytes,
            len(canonical_json(result.to_dict()).encode("utf-8")),
        )


def _snapshot(
    *,
    tracked_paths: tuple[str, ...] = ("README.md",),
    untracked_paths: tuple[str, ...] = (),
    provider_markers: tuple[str, ...] = (),
    warnings: tuple[str, ...] = (),
) -> RepositorySnapshot:
    return RepositorySnapshot(
        "1",
        "sha256:repo",
        "root",
        "sha256:root",
        "git",
        "common",
        "sha256:common",
        "sha256:worktree",
        "a" * 40,
        None,
        "sha256:clean",
        True,
        tuple(sorted(tracked_paths)),
        (),
        (),
        tuple(sorted(untracked_paths)),
        0,
        0,
        0,
        0,
        (("Python", 1),),
        (),
        tuple(sorted(provider_markers)),
        0,
        0,
        0,
        tuple(sorted(warnings)),
    )


def _descriptor(
    identity: str,
    *,
    source: DiscoverySource = DiscoverySource.USER_REGISTRY,
    availability: Availability = Availability.AVAILABLE,
    freshness: Freshness = Freshness.EXACT,
    confidence: Confidence = Confidence.VERIFIED,
    status_evidence: StatusEvidence = StatusEvidence.MANIFEST_VALIDATED,
    latency_ms: float | None = 1.0,
    marker_hints: tuple[str, ...] = (),
    required_actions: tuple[ContextAction, ...] = (),
) -> ProviderDescriptor:
    return ProviderDescriptor(
        schema_version="1",
        provider_identity=identity,
        provider_version="1.0.0",
        provider_schema_version="1",
        capabilities=("repository-map", "status"),
        locality=ProviderLocality.LOCAL,
        discovery_sources=(source,),
        availability=availability,
        registration=Registration.USER_REGISTERED,
        status_evidence=status_evidence,
        freshness=freshness,
        path_coverage=1.0,
        language_coverage=1.0,
        latency_ms=latency_ms,
        confidence=confidence,
        supported_actions=(ContextAction.QUERY,),
        required_actions=required_actions,
        marker_hints=tuple(sorted(marker_hints)),
        reason_codes=(),
        warnings=(),
    )


def _inventory(
    *providers: ProviderDescriptor,
    rejected_provider_count: int = 0,
    rejection_summaries: tuple[str, ...] = (),
    omitted_provider_count: int = 0,
    partial: bool = False,
) -> HostInventory:
    return HostInventory(
        "1",
        tuple(providers),
        rejected_provider_count,
        rejection_summaries,
        omitted_provider_count,
        partial,
    )


def _registration_entry(
    identity: str,
    *,
    schema: str = "1",
    required: tuple[str, ...] = ("repository-map",),
) -> ProjectRegistrationEntry:
    return ProjectRegistrationEntry(identity, schema, tuple(sorted(required)))


def _registration(*entries: ProjectRegistrationEntry) -> ProjectRegistration:
    return ProjectRegistration("1", "sha256:repo", tuple(entries))


def _identities(providers: tuple[ProviderDescriptor, ...]) -> tuple[str, ...]:
    return tuple(provider.provider_identity for provider in providers)


def _by_identity(
    providers: tuple[ProviderDescriptor, ...],
) -> dict[str, ProviderDescriptor]:
    return {provider.provider_identity: provider for provider in providers}


def _forbid_active_discovery():
    forbidden = AssertionError("discovery attempted forbidden I/O")
    return _PatchStack(
        patch.object(subprocess, "Popen", side_effect=forbidden),
        patch.object(subprocess, "run", side_effect=forbidden),
        patch.object(socket, "socket", side_effect=forbidden),
        patch.object(Path, "read_text", side_effect=forbidden),
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


if __name__ == "__main__":
    unittest.main()
