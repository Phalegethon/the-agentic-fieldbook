"""Behavioral tests for deterministic context freshness precedence."""

from __future__ import annotations

from dataclasses import replace
import unittest

from taf_context.freshness import (
    FreshnessExpectation,
    HeadRelation,
    assess_freshness,
)
from taf_context.models import BackgroundState, ContextManifest, Freshness, RepositorySnapshot


def manifest(**changes: object) -> ContextManifest:
    values: dict[str, object] = {
        "schema_version": "1",
        "repository_identity": "sha256:repository",
        "canonical_root_fingerprint": "sha256:root",
        "git_common_dir_fingerprint": "sha256:common",
        "worktree_identity": "sha256:worktree",
        "head_sha": "a" * 40,
        "dirty_fingerprint": "sha256:clean",
        "provider_name": "taf-context",
        "provider_version": "0.1.0",
        "provider_index_id": "level0:fixture",
        "provider_schema_version": "1",
        "index_levels": ("level0",),
        "capabilities": ("repository-map",),
        "created_at": "2026-08-25T00:00:00Z",
        "updated_at": "2026-08-25T00:00:00Z",
        "include_rules_hash": "sha256:include",
        "exclude_rules_hash": "sha256:exclude",
        "language_coverage": (("Python", 1.0),),
        "path_coverage": 1.0,
        "tracked_file_count": 1,
        "indexed_file_count": 1,
        "skipped_file_count": 0,
        "parse_failure_count": 0,
        "generated_or_vendored_count": 0,
        "storage_bytes": 0,
        "background_state": BackgroundState.READY,
        "warnings": (),
    }
    values.update(changes)
    return ContextManifest(**values)  # type: ignore[arg-type]


def snapshot(**changes: object) -> RepositorySnapshot:
    values: dict[str, object] = {
        "schema_version": "1",
        "repository_identity": "sha256:repository",
        "canonical_root": "/repo",
        "canonical_root_fingerprint": "sha256:root",
        "git_dir": "/repo/.git",
        "git_common_dir": "/repo/.git",
        "git_common_dir_fingerprint": "sha256:common",
        "worktree_identity": "sha256:worktree",
        "head_sha": "a" * 40,
        "branch": "main",
        "dirty_fingerprint": "sha256:clean",
        "dirty_fingerprint_complete": True,
        "tracked_paths": ("tracked.py",),
        "staged_paths": (),
        "unstaged_paths": (),
        "untracked_paths": (),
        "ignored_entry_count": 0,
        "generated_or_vendored_count": 0,
        "binary_file_count": 0,
        "oversized_file_count": 0,
        "language_counts": (("Python", 1),),
        "candidate_artifacts": (),
        "provider_markers": (),
        "insertions": 0,
        "deletions": 0,
        "dirty_bytes_hashed": 0,
        "warnings": (),
    }
    values.update(changes)
    return RepositorySnapshot(**values)  # type: ignore[arg-type]


def expectation(**changes: object) -> FreshnessExpectation:
    values: dict[str, object] = {
        "repository_authorized": True,
        "provider_compatible": True,
        "provider_schema_version": "1",
        "include_rules_hash": "sha256:include",
        "exclude_rules_hash": "sha256:exclude",
        "required_path_coverage": 1.0,
        "head_relation": HeadRelation.MATCHES,
        "changed_path_count": 0,
        "maximum_changed_path_count": 10,
        "dirty_state_proven": True,
        "manifest_is_corrupt": False,
    }
    values.update(changes)
    return FreshnessExpectation(**values)  # type: ignore[arg-type]


class FreshnessPrecedenceTests(unittest.TestCase):
    def test_precedence_table_reports_stable_reasons_and_actions(self) -> None:
        clean_manifest = manifest()
        clean_snapshot = snapshot()
        clean_expectation = expectation()
        cases = (
            (
                "unusable wins over all lower-precedence signals",
                replace(clean_manifest, warnings=("manifest-corrupt",)),
                replace(clean_snapshot, dirty_fingerprint="sha256:changed"),
                replace(
                    clean_expectation,
                    repository_authorized=False,
                    provider_compatible=False,
                    head_relation=HeadRelation.DIVERGED,
                ),
                Freshness.UNUSABLE,
                (
                    "repository-unauthorized",
                    "manifest-corrupt",
                    "provider-incompatible",
                    "head-diverged",
                ),
                False,
                False,
            ),
            (
                "structural mismatch wins over unproven current state",
                replace(
                    clean_manifest,
                    repository_identity="sha256:other",
                    provider_schema_version="2",
                    include_rules_hash="sha256:other-include",
                    exclude_rules_hash="sha256:other-exclude",
                ),
                clean_snapshot,
                replace(clean_expectation, dirty_state_proven=False),
                Freshness.STRUCTURALLY_STALE,
                (
                    "repository-identity-mismatch",
                    "provider-schema-mismatch",
                    "include-rules-mismatch",
                    "exclude-rules-mismatch",
                    "dirty-state-unproven",
                ),
                True,
                False,
            ),
            (
                "unproven head relation is unknown",
                clean_manifest,
                clean_snapshot,
                replace(clean_expectation, head_relation=HeadRelation.UNKNOWN),
                Freshness.UNKNOWN,
                ("head-relation-unproven",),
                False,
                False,
            ),
            (
                "bounded forward head is incrementally stale",
                clean_manifest,
                clean_snapshot,
                replace(
                    clean_expectation,
                    head_relation=HeadRelation.FORWARD,
                    changed_path_count=3,
                ),
                Freshness.INCREMENTALLY_STALE,
                ("head-forward", "changed-path-set-bounded"),
                False,
                True,
            ),
            (
                "missing coverage and incomplete current fingerprint are partial",
                replace(clean_manifest, path_coverage=0.5),
                replace(clean_snapshot, dirty_fingerprint_complete=False),
                clean_expectation,
                Freshness.PARTIAL,
                (
                    "required-path-coverage-absent",
                    "dirty-fingerprint-incomplete",
                ),
                False,
                False,
            ),
            (
                "matching head with different dirty overlay is worktree stale",
                clean_manifest,
                replace(clean_snapshot, dirty_fingerprint="sha256:changed"),
                clean_expectation,
                Freshness.COMMIT_FRESH_WORKTREE_STALE,
                ("dirty-fingerprint-mismatch",),
                False,
                True,
            ),
            (
                "all matching evidence is exact",
                clean_manifest,
                clean_snapshot,
                clean_expectation,
                Freshness.EXACT,
                ("exact-match",),
                False,
                False,
            ),
        )

        for (
            name,
            case_manifest,
            case_snapshot,
            case_expectation,
            expected_freshness,
            expected_reasons,
            expected_rebuild,
            expected_incremental,
        ) in cases:
            with self.subTest(name=name):
                assessment = assess_freshness(
                    case_manifest, case_snapshot, case_expectation
                )

                self.assertEqual(assessment.freshness, expected_freshness)
                self.assertEqual(assessment.reason_codes, expected_reasons)
                self.assertEqual(assessment.requires_rebuild, expected_rebuild)
                self.assertEqual(
                    assessment.can_incrementally_update, expected_incremental
                )

    def test_partial_dirty_fingerprint_never_becomes_exact_when_head_matches(self) -> None:
        assessment = assess_freshness(
            manifest(warnings=("dirty-fingerprint-incomplete",)),
            snapshot(),
            expectation(),
        )

        self.assertEqual(assessment.freshness, Freshness.PARTIAL)
        self.assertEqual(assessment.reason_codes, ("dirty-fingerprint-incomplete",))
        self.assertFalse(assessment.requires_rebuild)
        self.assertFalse(assessment.can_incrementally_update)

    def test_different_worktree_is_unusable_even_with_matching_repository_and_head(
        self,
    ) -> None:
        assessment = assess_freshness(
            manifest(worktree_identity="sha256:other-worktree"),
            snapshot(),
            expectation(),
        )

        self.assertEqual(assessment.freshness, Freshness.UNUSABLE)
        self.assertEqual(assessment.reason_codes, ("worktree-scope-mismatch",))
        self.assertFalse(assessment.requires_rebuild)
        self.assertFalse(assessment.can_incrementally_update)

    def test_matching_verified_head_is_exact_without_changed_path_count(self) -> None:
        assessment = assess_freshness(
            manifest(),
            snapshot(),
            expectation(changed_path_count=None),
        )

        self.assertEqual(assessment.freshness, Freshness.EXACT)
        self.assertEqual(assessment.reason_codes, ("exact-match",))
        self.assertFalse(assessment.requires_rebuild)
        self.assertFalse(assessment.can_incrementally_update)


if __name__ == "__main__":
    unittest.main()
