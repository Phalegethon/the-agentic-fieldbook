"""Replacement schedule ownership and immutable evidence tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from .level1_replacement_controller import (
    ReplacementSample,
    replacement_stage_a_schedule,
    replacement_stage_b_schedule,
    run_replacement_schedule,
)
from .level1_replacement_scoring import ReplacementEvidence


SHA = "sha256:" + "1" * 64


def expected(schedule: tuple[ReplacementSample, ...]) -> dict[str, tuple[str, ...]]:
    return {
        sample.sample_identity: (SHA,)
        for sample in schedule
        if sample.record_type == "query"
    }


def observation(sample: ReplacementSample) -> dict[str, object]:
    isolation = {
        "repository_writes": 0,
        "network_attempts": 0,
        "undeclared_child_processes": 0,
        "state_escapes": 0,
    }
    if sample.record_type == "product":
        return {
            "artifact_size_bytes": 1,
            "cross_platform_targets": 5,
            "dependency_license_checks_passed": 1,
            "dependency_license_checks_total": 1,
            "maintenance_checks_passed": 1,
            "maintenance_checks_total": 1,
            "unsafe_code_blocks": 0,
        }
    if sample.record_type == "build":
        return {
            "elapsed_ns": 1,
            "peak_rss_bytes": 1,
            "index_and_staging_bytes": 1,
            "index_digest": SHA,
            **isolation,
        }
    if sample.record_type == "query":
        return {
            "elapsed_ns": 1,
            "actual_result_identities": [SHA],
            "citation_match": True,
            "evidence_class_match": True,
            "freshness_honest": True,
            "forbidden_result_count": 0,
            "response_characters": 1,
            "repository_files_opened": 0,
            "repository_bytes_read": 0,
            "considered_records": 1,
            "full_repository_operations": 0,
            **isolation,
        }
    if sample.record_type == "update":
        return {
            "elapsed_ns": 1,
            "enumerated_repository_files": 1,
            "parsed_repository_files": 1,
            "incremental_digest": SHA,
            "rebuild_digest": SHA,
            **isolation,
        }
    if sample.record_type == "index-determinism":
        return {"first_index_digest": SHA, "second_index_digest": SHA}
    raise AssertionError(sample.record_type)


class ReplacementScheduleTests(unittest.TestCase):
    def test_stage_schedules_are_deterministic_unique_and_frozen(self) -> None:
        stage_a = replacement_stage_a_schedule()
        stage_b = replacement_stage_b_schedule()

        self.assertEqual(stage_a, replacement_stage_a_schedule())
        self.assertEqual(stage_b, replacement_stage_b_schedule())
        self.assertEqual(len(stage_a), 52)
        self.assertEqual(len(stage_b), 136)
        for schedule in (stage_a, stage_b):
            identities = tuple(sample.sample_identity for sample in schedule)
            self.assertEqual(len(identities), len(set(identities)))
            self.assertEqual(sum(sample.record_type == "product" for sample in schedule), 1)
            self.assertEqual(sum(sample.record_type == "index-determinism" for sample in schedule), 1)
            self.assertTrue(any(sample.query_class == "exact" for sample in schedule))
            self.assertTrue(any(sample.query_class == "fuzzy" for sample in schedule))

    def test_stage_b_keeps_existing_query_vector_and_retention_identity(self) -> None:
        schedule = replacement_stage_b_schedule()

        query = next(sample for sample in schedule if sample.sample_identity == "query-l1-q-015-5")
        self.assertEqual(query.query_class, "fuzzy")
        self.assertEqual(query.ordinal, 5)
        self.assertTrue(query.retained)
        self.assertFalse(any("l1-q-003" in sample.sample_identity for sample in schedule))


class ReplacementControllerTests(unittest.TestCase):
    def run_once(self, root: Path, *, execute=observation) -> ReplacementEvidence:
        schedule = replacement_stage_a_schedule()
        return run_replacement_schedule(
            run_identity="replacement-run-0001",
            environment_identity="linux-arm64-container",
            policy_identity="sha256:" + "a" * 64,
            candidate_identity="rust-compact",
            candidate_digest="sha256:" + "2" * 64,
            corpus_identity="sha256:" + "4" * 64,
            evidence_root=root,
            schedule=schedule,
            expected_result_identities=expected(schedule),
            relevant_source_bytes=1,
            model_output_budget_characters=2000,
            declared_changed_files={
                sample.sample_identity: 1
                for sample in schedule
                if sample.record_type == "update"
            },
            execute=execute,
        )

    def test_complete_schedule_is_canonical_reopenable_and_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "evidence"
            first = self.run_once(root)
            raw = (root / "evidence.jsonl").read_bytes()

            self.assertEqual(first.candidate_identity, "rust-compact")
            self.assertEqual(ReplacementEvidence.from_jsonl(root / "evidence.jsonl"), first)
            self.assertTrue(raw.endswith(b"\n"))
            with self.assertRaisesRegex(ValueError, "already exists"):
                self.run_once(root)
            self.assertEqual((root / "evidence.jsonl").read_bytes(), raw)

    def test_mapping_order_cannot_change_installed_evidence_bytes(self) -> None:
        def reversed_observation(sample: ReplacementSample) -> dict[str, object]:
            return dict(reversed(tuple(observation(sample).items())))

        with tempfile.TemporaryDirectory() as temp:
            first_root = Path(temp) / "first"
            second_root = Path(temp) / "second"
            self.run_once(first_root)
            self.run_once(second_root, execute=reversed_observation)

            self.assertEqual(
                (first_root / "evidence.jsonl").read_bytes(),
                (second_root / "evidence.jsonl").read_bytes(),
            )

    def test_candidate_cannot_set_controller_owned_fields(self) -> None:
        forbidden = (
            "record_type", "sample_identity", "retained", "ordinal",
            "query_class", "expected_result_identities", "policy_identity",
            "candidate_identity", "candidate_digest", "environment_identity",
            "relevant_source_bytes", "budget_characters",
            "declared_changed_files",
        )
        for field in forbidden:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp:
                def hostile(sample: ReplacementSample, field: str = field) -> dict[str, object]:
                    return {**observation(sample), field: "forged"}

                with self.assertRaisesRegex(ValueError, "controller-owned"):
                    self.run_once(Path(temp) / "evidence", execute=hostile)

    def test_expected_identity_map_must_be_exact_and_controller_owned(self) -> None:
        schedule = replacement_stage_a_schedule()
        expected_map = expected(schedule)
        expected_map.pop(next(iter(expected_map)))

        with tempfile.TemporaryDirectory() as temp, self.assertRaisesRegex(ValueError, "expected-result map"):
            run_replacement_schedule(
                run_identity="replacement-run-0001",
                environment_identity="linux-arm64-container",
                policy_identity="sha256:" + "a" * 64,
                candidate_identity="rust-compact",
                candidate_digest="sha256:" + "2" * 64,
                corpus_identity="sha256:" + "4" * 64,
                evidence_root=Path(temp) / "evidence",
                schedule=schedule,
                expected_result_identities=expected_map,
                relevant_source_bytes=1,
                model_output_budget_characters=2000,
                declared_changed_files={
                    sample.sample_identity: 1
                    for sample in schedule
                    if sample.record_type == "update"
                },
                execute=observation,
            )

    def test_interruption_retains_bounded_failure_without_exception_text(self) -> None:
        secret = "/Users/private/source token=secret-value"
        calls = 0

        def interrupted(sample: ReplacementSample) -> dict[str, object]:
            nonlocal calls
            calls += 1
            if calls == 4:
                raise RuntimeError(secret)
            return observation(sample)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "evidence"
            with self.assertRaises(RuntimeError):
                self.run_once(root, execute=interrupted)

            failure = json.loads((root / "failure.json").read_text(encoding="utf-8"))
            partial = (root / "partial-evidence.jsonl").read_text(encoding="utf-8")
            combined = json.dumps(failure) + partial
            self.assertEqual(failure["exception_type"], "RuntimeError")
            self.assertEqual(failure["failed_sample_identity"], "build-2")
            self.assertNotIn(secret, combined)
            self.assertNotIn("secret-value", combined)
            self.assertNotIn("/Users/", combined)
            self.assertFalse((root / "evidence.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
