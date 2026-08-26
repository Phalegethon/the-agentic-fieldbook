"""Behavioral tests for strict, portable work-recovery records."""

from __future__ import annotations

import copy
import unittest
from dataclasses import FrozenInstanceError

from taf_context.models import ManifestError, canonical_json
from taf_context.recovery_models import (
    EvidenceClass,
    RecoveryClaim,
    RecoveryCoverage,
    RecoveryDossier,
    WorkState,
    WorkstreamState,
)


CLAIM = {
    "claim_id": "current.state",
    "evidence_class": "observed",
    "text": "The current worktree has tracked changes.",
    "repository_identity": "sha256:repo",
    "worktree_identity": "sha256:worktree",
    "provenance": ["git/status"],
    "freshness": "exact",
    "supports": ["current.next-action"],
    "conflicts": [],
    "qualifications": [],
}

WORKSTREAM = {
    "worktree_identity": "sha256:worktree",
    "branch": "codex/work-recovery",
    "head_sha": "a" * 40,
    "base_sha": "b" * 40,
    "state": "active-dirty",
    "staged_count": 1,
    "unstaged_count": 2,
    "untracked_count": 3,
    "ahead_count": 4,
    "behind_count": 0,
    "reason_codes": ["dirty-tracked", "unique-commits"],
}

COVERAGE = {
    "changed_path_count": 6,
    "examined_path_count": 4,
    "cluster_count": 3,
    "included_cluster_count": 2,
    "omitted_item_count": 2,
    "omitted_characters": 120,
    "budget_characters": 4000,
}

DOSSIER = {
    "schema_version": "1",
    "repository_identity": "sha256:repo",
    "worktree_identity": "sha256:worktree",
    "current": WORKSTREAM,
    "candidates": [],
    "claims": [CLAIM],
    "coverage": COVERAGE,
    "warnings": ["base-resolution-fallback"],
    "next_action_hint": "Review the staged refactor before continuing.",
}


class RecoveryRecordRoundTripTests(unittest.TestCase):
    def test_dossier_round_trip_is_exact_and_collections_are_frozen(self) -> None:
        dossier = RecoveryDossier.from_dict(DOSSIER)

        self.assertEqual(dossier.to_dict(), DOSSIER)
        self.assertEqual(dossier.claims[0].provenance, ("git/status",))
        self.assertIs(dossier.claims[0].evidence_class, EvidenceClass.OBSERVED)
        self.assertIs(dossier.current.state, WorkState.ACTIVE_DIRTY)
        self.assertEqual(canonical_json(dossier.to_dict()), canonical_json(DOSSIER))
        with self.assertRaises(FrozenInstanceError):
            dossier.repository_identity = "changed"  # type: ignore[misc]

    def test_each_record_has_an_explicit_round_trip(self) -> None:
        self.assertEqual(RecoveryClaim.from_dict(CLAIM).to_dict(), CLAIM)
        self.assertEqual(WorkstreamState.from_dict(WORKSTREAM).to_dict(), WORKSTREAM)
        self.assertEqual(RecoveryCoverage.from_dict(COVERAGE).to_dict(), COVERAGE)


class RecoveryRecordValidationTests(unittest.TestCase):
    def test_rejects_missing_and_unknown_fields_without_echoing_values(self) -> None:
        for mutation, field in (("missing", "text"), ("unknown", "secret_field")):
            with self.subTest(mutation=mutation):
                invalid = copy.deepcopy(CLAIM)
                if mutation == "missing":
                    del invalid[field]
                else:
                    invalid[field] = "/private/company/value"
                with self.assertRaisesRegex(ManifestError, field) as caught:
                    RecoveryClaim.from_dict(invalid)
                self.assertNotIn("/private/company/value", str(caught.exception))

    def test_rejects_unknown_schema_and_enum_values(self) -> None:
        invalid_schema = copy.deepcopy(DOSSIER)
        invalid_schema["schema_version"] = "2"
        with self.assertRaisesRegex(ManifestError, "schema_version"):
            RecoveryDossier.from_dict(invalid_schema)

        for record, field, value in (
            (CLAIM, "evidence_class", "guessed"),
            (CLAIM, "freshness", "yesterday"),
            (WORKSTREAM, "state", "probably-active"),
        ):
            with self.subTest(field=field):
                invalid = copy.deepcopy(record)
                invalid[field] = value
                loader = RecoveryClaim.from_dict if record is CLAIM else WorkstreamState.from_dict
                with self.assertRaisesRegex(ManifestError, field):
                    loader(invalid)

    def test_rejects_boolean_negative_and_unbounded_counters(self) -> None:
        for field, value in (
            ("staged_count", True),
            ("unstaged_count", -1),
            ("untracked_count", 2**31),
            ("ahead_count", False),
        ):
            with self.subTest(field=field):
                invalid = copy.deepcopy(WORKSTREAM)
                invalid[field] = value
                with self.assertRaisesRegex(ManifestError, field):
                    WorkstreamState.from_dict(invalid)

        invalid_coverage = copy.deepcopy(COVERAGE)
        invalid_coverage["budget_characters"] = True
        with self.assertRaisesRegex(ManifestError, "budget_characters"):
            RecoveryCoverage.from_dict(invalid_coverage)

    def test_rejects_unsorted_or_duplicate_set_like_fields(self) -> None:
        mutations = (
            (CLAIM, "provenance", ["git/status", "git/diff"]),
            (CLAIM, "supports", ["same", "same"]),
            (WORKSTREAM, "reason_codes", ["z", "a"]),
            (DOSSIER, "warnings", ["warning", "warning"]),
        )
        for record, field, value in mutations:
            with self.subTest(field=field):
                invalid = copy.deepcopy(record)
                invalid[field] = value
                if record is CLAIM:
                    loader = RecoveryClaim.from_dict
                elif record is WORKSTREAM:
                    loader = WorkstreamState.from_dict
                else:
                    loader = RecoveryDossier.from_dict
                with self.assertRaisesRegex(ManifestError, field):
                    loader(invalid)

    def test_rejects_absolute_or_traversing_evidence_references(self) -> None:
        for reference in ("/private/repo/file.py", "../secret", "git/../../secret", "C:\\secret"):
            with self.subTest(reference=reference):
                invalid = copy.deepcopy(CLAIM)
                invalid["provenance"] = [reference]
                with self.assertRaisesRegex(ManifestError, "provenance") as caught:
                    RecoveryClaim.from_dict(invalid)
                self.assertNotIn(reference, str(caught.exception))

    def test_rejects_invalid_object_ids(self) -> None:
        for field in ("head_sha", "base_sha"):
            for object_id in ("a" * 39, "g" * 40, "a" * 65):
                with self.subTest(field=field, object_id=object_id):
                    invalid = copy.deepcopy(WORKSTREAM)
                    invalid[field] = object_id
                    with self.assertRaisesRegex(ManifestError, field):
                        WorkstreamState.from_dict(invalid)

    def test_accepts_absent_optional_relationship_values(self) -> None:
        value = copy.deepcopy(WORKSTREAM)
        for field in ("branch", "head_sha", "base_sha", "ahead_count", "behind_count"):
            value[field] = None

        state = WorkstreamState.from_dict(value)

        self.assertIsNone(state.branch)
        self.assertIsNone(state.ahead_count)

    def test_rejects_noncanonical_ids_and_overlong_strings(self) -> None:
        for field, value in (
            ("claim_id", "Current State"),
            ("claim_id", "current..state"),
            ("repository_identity", "x" * 513),
            ("text", "İ" * 513),
        ):
            with self.subTest(field=field):
                invalid = copy.deepcopy(CLAIM)
                invalid[field] = value
                with self.assertRaisesRegex(ManifestError, field):
                    RecoveryClaim.from_dict(invalid)

    def test_rejects_candidate_and_claim_collection_limits(self) -> None:
        too_many_candidates = copy.deepcopy(DOSSIER)
        too_many_candidates["candidates"] = [copy.deepcopy(WORKSTREAM) for _ in range(65)]
        with self.assertRaisesRegex(ManifestError, "candidates"):
            RecoveryDossier.from_dict(too_many_candidates)

        too_many_claims = copy.deepcopy(DOSSIER)
        too_many_claims["claims"] = [copy.deepcopy(CLAIM) for _ in range(1025)]
        with self.assertRaisesRegex(ManifestError, "claims"):
            RecoveryDossier.from_dict(too_many_claims)

    def test_rejects_duplicate_candidate_and_claim_ids(self) -> None:
        duplicate_candidates = copy.deepcopy(DOSSIER)
        duplicate_candidates["candidates"] = [copy.deepcopy(WORKSTREAM), copy.deepcopy(WORKSTREAM)]
        with self.assertRaisesRegex(ManifestError, "candidates"):
            RecoveryDossier.from_dict(duplicate_candidates)

        duplicate_claims = copy.deepcopy(DOSSIER)
        duplicate_claims["claims"] = [copy.deepcopy(CLAIM), copy.deepcopy(CLAIM)]
        with self.assertRaisesRegex(ManifestError, "claims"):
            RecoveryDossier.from_dict(duplicate_claims)


if __name__ == "__main__":
    unittest.main()
