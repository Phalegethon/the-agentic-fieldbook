"""Freshness derivation tests for actively inspected providers."""

from __future__ import annotations

from dataclasses import replace
import unittest

from taf_context.models import Confidence, ContextAction, Freshness, RepositorySnapshot
from taf_context.provider_execution_models import InspectionRecord, Readiness
from taf_context.provider_freshness import derive_provider_freshness, refine_descriptor
from taf_context.provider_models import (
    Availability,
    DiscoverySource,
    ProviderDescriptor,
    ProviderLocality,
    Registration,
    StatusEvidence,
)


REPO = "sha256:" + "a" * 64
WORKTREE = "sha256:" + "b" * 64
DIRTY = "sha256:" + "c" * 64
INDEX = "sha256:" + "d" * 64
HEAD = "1" * 40


def snapshot() -> RepositorySnapshot:
    return RepositorySnapshot(
        "1", REPO, "root", "sha256:root", "git", "common",
        "sha256:common", WORKTREE, HEAD, "main", DIRTY, True,
        ("src/app.py",), (), (), (), 0, 0, 0, 0, (("Python", 1),),
        (), (), 0, 0, 0, (),
    )


def inspection() -> InspectionRecord:
    return InspectionRecord(
        "1", "fixture.stdio", "fixture.graph", "2.0.0", REPO,
        WORKTREE, HEAD, DIRTY, INDEX, Readiness.READY,
        ("repository-map", "search-symbols"), 1.0, 1.0, 4096, (), (),
    )


def descriptor() -> ProviderDescriptor:
    return ProviderDescriptor(
        "1", "fixture.graph", "2.0.0", "1",
        ("repository-map", "search-symbols"), ProviderLocality.LOCAL,
        (DiscoverySource.USER_REGISTRY,), Availability.AVAILABLE,
        Registration.USER_REGISTERED, StatusEvidence.UNINSPECTED,
        Freshness.UNKNOWN, None, None, None, Confidence.UNCERTAIN,
        (ContextAction.INSPECT, ContextAction.QUERY),
        (ContextAction.INSPECT, ContextAction.QUERY), (), (), (),
    )


class ProviderFreshnessTests(unittest.TestCase):
    def test_exact_binding_is_verified(self) -> None:
        result = derive_provider_freshness(inspection(), snapshot())
        self.assertEqual(result.freshness, Freshness.EXACT)
        self.assertEqual(result.reason_codes, ("exact-match",))

    def test_provider_cannot_override_repository_or_worktree_mismatch(self) -> None:
        repository = derive_provider_freshness(
            replace(inspection(), repository_identity="sha256:" + "e" * 64),
            snapshot(),
        )
        worktree = derive_provider_freshness(
            replace(inspection(), worktree_identity="sha256:" + "f" * 64),
            snapshot(),
        )
        self.assertEqual(repository.freshness, Freshness.STRUCTURALLY_STALE)
        self.assertEqual(repository.reason_codes, ("repository-identity-mismatch",))
        self.assertEqual(worktree.freshness, Freshness.UNUSABLE)
        self.assertEqual(worktree.reason_codes, ("worktree-identity-mismatch",))

    def test_corrupt_partial_head_and_dirty_states_downgrade_deterministically(self) -> None:
        cases = (
            (replace(inspection(), readiness=Readiness.CORRUPT, reason_codes=("index-corrupt",)), snapshot(), Freshness.UNUSABLE, "index-corrupt"),
            (replace(inspection(), readiness=Readiness.PARTIAL), snapshot(), Freshness.PARTIAL, "provider-partial"),
            (replace(inspection(), committed_head="2" * 40), snapshot(), Freshness.UNKNOWN, "head-mismatch"),
            (replace(inspection(), dirty_overlay_fingerprint="sha256:" + "e" * 64), snapshot(), Freshness.COMMIT_FRESH_WORKTREE_STALE, "dirty-overlay-mismatch"),
            (inspection(), replace(snapshot(), dirty_fingerprint_complete=False), Freshness.PARTIAL, "dirty-state-incomplete"),
        )
        for inspected, current, expected, reason in cases:
            with self.subTest(reason=reason):
                result = derive_provider_freshness(inspected, current)
                self.assertEqual(result.freshness, expected)
                self.assertIn(reason, result.reason_codes)

    def test_refinement_uses_derived_freshness_and_inspection_coverage(self) -> None:
        assessment = derive_provider_freshness(inspection(), snapshot())
        refined = refine_descriptor(descriptor(), inspection(), assessment)
        self.assertEqual(refined.status_evidence, StatusEvidence.PROVIDER_INSPECTED)
        self.assertEqual(refined.freshness, Freshness.EXACT)
        self.assertEqual(refined.path_coverage, 1.0)
        self.assertEqual(refined.language_coverage, 1.0)
        self.assertEqual(refined.confidence, Confidence.VERIFIED)

    def test_refinement_rejects_identity_mismatch(self) -> None:
        with self.assertRaises(ValueError):
            refine_descriptor(
                replace(descriptor(), provider_identity="other.provider"),
                inspection(),
                derive_provider_freshness(inspection(), snapshot()),
            )


if __name__ == "__main__":
    unittest.main()
