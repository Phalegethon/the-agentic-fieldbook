"""Controller-owned validation, gate, score, and decision tests."""

from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

from .level1_scoring import (
    CandidateEvidence,
    GateStatus,
    decide_bakeoff,
    evaluate_gates,
    percentile_nearest_rank,
    score_candidate,
)


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def evidence_lines(candidate: str = "python", *, latency: int = 100_000_000) -> list[dict[str, object]]:
    common = {
        "environment_identity": "macos-arm64",
        "candidate_identity": candidate,
        "candidate_digest": "sha256:" + ("1" if candidate == "python" else "2") * 64,
    }
    lines: list[dict[str, object]] = [
        {
            "record_type": "run",
            "schema_version": "1",
            "run_identity": f"run-{candidate}",
            "environment_identity": common["environment_identity"],
            "candidate_identity": candidate,
            "candidate_digest": common["candidate_digest"],
            "corpus_identity": "sha256:" + "3" * 64,
        }
    ]
    lines.append(
        {
            **common,
            "record_type": "process",
            "sample_identity": "product-metadata",
            "phase": "product-metadata",
            "retained": True,
            "ordinal": 0,
            "elapsed_ns": 0,
            "peak_rss_bytes": 0,
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "exit_code": 0,
            "escape_count": 0,
            "runtime_artifacts": 1,
            "single_self_contained_artifact": True,
            "cross_platform_targets": 2,
            "no_system_dependency": True,
            "lock_license_complete": True,
            "startup_simple": True,
            "security_checks_passed": 5,
            "security_checks_total": 5,
            "maintenance_checks_passed": 5,
            "maintenance_checks_total": 5,
        }
    )
    rebuild_phases = [("cold", True, 0), ("warmup", False, 0)] + [("warm", True, item) for item in range(1, 6)] + [("after-mutation", True, 1)]
    for phase, retained, ordinal in rebuild_phases:
        lines.append(
            {
                **common,
                "record_type": "rebuild",
                "sample_identity": f"rebuild-{phase}-{ordinal}",
                "phase": phase,
                "retained": retained,
                "ordinal": ordinal,
                "elapsed_ns": 10_000_000_000 if phase == "cold" else 5_000_000_000,
                "peak_rss_bytes": 128 * 1024 * 1024,
                "index_and_staging_bytes": 1_000_000,
                "relevant_source_bytes": 2_000_000,
            }
        )
    for ordinal in range(0, 6):
        lines.append(
            {
                **common,
                "record_type": "update",
                "sample_identity": f"update-{ordinal}",
                "retained": ordinal > 0,
                "ordinal": ordinal,
                "elapsed_ns": 500_000_000,
                "incremental_digest": "sha256:" + "4" * 64,
                "rebuild_digest": "sha256:" + "4" * 64,
            }
        )
    for vector in range(1, 25):
        vector_id = f"L1-Q-{vector:03d}"
        query_class = "state" if vector in {3, 4, 5, 6} else ("fuzzy" if vector in {9, 15} else "exact")
        for ordinal in range(0, 6):
            lines.append(
                {
                    **common,
                    "record_type": "query",
                    "sample_identity": f"query-{vector_id}-{ordinal}",
                    "vector_identity": vector_id,
                    "query_class": query_class,
                    "retained": ordinal > 0,
                    "ordinal": ordinal,
                    "elapsed_ns": latency,
                    "expected_result_identities": ["sha256:" + f"{vector:064x}"],
                    "actual_result_identities": ["sha256:" + f"{vector:064x}"],
                    "citation_match": True,
                    "evidence_class_match": True,
                    "freshness_honest": True,
                    "forbidden_result_count": 0,
                    "response_characters": 1000,
                    "budget_characters": 2000,
                    "escape_count": 0,
                    "repository_hash_match": True,
                    "leak_detected": False,
                    "result_digest": "sha256:" + f"{vector:064x}",
                }
            )
        for ordinal in range(4):
            lines.append(
                {
                    **common,
                    "record_type": "determinism",
                    "sample_identity": f"determinism-{vector_id}-{ordinal}",
                    "vector_identity": vector_id,
                    "permuted": ordinal >= 2,
                    "result_digest": "sha256:" + f"{vector:064x}",
                }
            )
    for vector_id in ("L1-S-CORRUPT", "L1-S-MOVED", "L1-S-WORKTREE"):
        lines.append(
            {
                **common,
                "record_type": "query",
                "sample_identity": f"state-{vector_id}",
                "vector_identity": vector_id,
                "query_class": "state",
                "retained": True,
                "ordinal": 1,
                "elapsed_ns": latency * 10,
                "expected_result_identities": [],
                "actual_result_identities": [],
                "citation_match": True,
                "evidence_class_match": True,
                "freshness_honest": True,
                "forbidden_result_count": 0,
                "response_characters": 1000,
                "budget_characters": 2000,
                "escape_count": 0,
                "repository_hash_match": True,
                "leak_detected": False,
                "result_digest": "sha256:" + "6" * 64,
            }
        )
    lines.append(
        {
            **common,
            "record_type": "repository",
            "sample_identity": "repository-final",
            "before_hash": "sha256:" + "5" * 64,
            "after_hash": "sha256:" + "5" * 64,
        }
    )
    return lines


def write_evidence(path: Path, lines: list[dict[str, object]]) -> None:
    path.write_text("".join(canonical(line) + "\n" for line in lines), encoding="utf-8")


class EvidenceValidationTests(unittest.TestCase):
    def test_summary_without_leaves_and_oversized_evidence_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            summary = root / "summary.jsonl"
            write_evidence(summary, evidence_lines()[:1])
            oversized = root / "oversized.jsonl"
            with oversized.open("wb") as handle:
                handle.truncate(64 * 1024 * 1024 + 1)

            for path in (summary, oversized):
                with self.subTest(path=path.name), self.assertRaises(ValueError):
                    CandidateEvidence.from_jsonl(path)

    def test_missing_binary_product_evidence_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "evidence.jsonl"
            lines = [
                line for line in evidence_lines()
                if line.get("record_type") != "process"
            ]
            write_evidence(path, lines)
            with self.assertRaises(ValueError):
                CandidateEvidence.from_jsonl(path)

    def test_complete_leaf_schedule_round_trips_and_passes_gates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "evidence.jsonl"
            write_evidence(path, evidence_lines())
            evidence = CandidateEvidence.from_jsonl(path)

        report = evaluate_gates(evidence)
        self.assertIs(report.support_status, GateStatus.PASS)
        self.assertTrue(all(value is GateStatus.PASS for value in report.gate_statuses.values()))
        self.assertEqual(report.exact_top_five_recall, 1.0)
        self.assertEqual(report.fuzzy_top_ten_recall, 1.0)
        self.assertEqual(report.warm_query_p95_ns, 100_000_000)
        self.assertEqual(report.incremental_update_p95_ns, 500_000_000)
        self.assertEqual(report.response_budget_overruns, 0)

    def test_malformed_or_incomplete_leaves_fail_closed(self) -> None:
        mutations = []
        duplicate = evidence_lines()
        duplicate.append(dict(duplicate[1]))
        mutations.append(duplicate)
        unknown = evidence_lines()
        unknown[1]["unknown_metric"] = 1
        mutations.append(unknown)
        mixed = evidence_lines()
        mixed[2]["environment_identity"] = "other-host"
        mutations.append(mixed)
        negative = evidence_lines()
        negative[2]["elapsed_ns"] = -1
        mutations.append(negative)
        nonfinite = evidence_lines()
        nonfinite[2]["elapsed_ns"] = math.nan
        mutations.append(nonfinite)
        incomplete = [line for line in evidence_lines() if line.get("sample_identity") != "query-L1-Q-001-5"]
        mutations.append(incomplete)
        missing_state = [line for line in evidence_lines() if line.get("sample_identity") != "state-L1-S-MOVED"]
        mutations.append(missing_state)

        for ordinal, lines in enumerate(mutations):
            with self.subTest(ordinal=ordinal), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "evidence.jsonl"
                write_evidence(path, lines)
                with self.assertRaises(ValueError):
                    CandidateEvidence.from_jsonl(path)

    def test_nearest_rank_percentile_is_integer_and_deterministic(self) -> None:
        self.assertEqual(percentile_nearest_rank((5, 1, 4, 2, 3), 95), 5)
        self.assertEqual(percentile_nearest_rank((10,) * 5, 95), 10)
        with self.assertRaises(ValueError):
            percentile_nearest_rank((), 95)


class ScoringAndDecisionTests(unittest.TestCase):
    def load(self, candidate: str, latency: int) -> CandidateEvidence:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "evidence.jsonl"
        write_evidence(path, evidence_lines(candidate, latency=latency))
        return CandidateEvidence.from_jsonl(path)

    def load_lines(self, lines: list[dict[str, object]]) -> CandidateEvidence:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "evidence.jsonl"
        write_evidence(path, lines)
        return CandidateEvidence.from_jsonl(path)

    def test_fixed_checklist_dimensions_are_not_population_normalized(self) -> None:
        lines = evidence_lines()
        metadata = next(line for line in lines if line.get("record_type") == "process")
        metadata.update(
            {
                "single_self_contained_artifact": False,
                "cross_platform_targets": 1,
                "no_system_dependency": False,
                "lock_license_complete": False,
                "startup_simple": False,
                "security_checks_passed": 2,
                "security_checks_total": 5,
                "maintenance_checks_passed": 1,
                "maintenance_checks_total": 5,
            }
        )
        candidate = self.load_lines(lines)

        score = score_candidate(candidate, (candidate,))

        self.assertEqual(score.dimension_points["packaging"], 0.0)
        self.assertEqual(score.dimension_points["security"], 2.0)
        self.assertEqual(score.dimension_points["maintenance"], 1.0)

    def test_failed_candidates_do_not_change_eligible_normalization(self) -> None:
        fast = self.load("python", 80_000_000)
        slow = self.load("rust", 160_000_000)
        failed_lines = evidence_lines("failed", latency=1)
        failed_query = next(
            line for line in failed_lines
            if line.get("record_type") == "query" and line.get("retained")
        )
        failed_query["leak_detected"] = True
        failed = self.load_lines(failed_lines)

        baseline = score_candidate(fast, (fast, slow))
        with_failed = score_candidate(fast, (fast, slow, failed))

        self.assertEqual(with_failed.dimension_points, baseline.dimension_points)
        self.assertEqual(with_failed.total_points, baseline.total_points)

    def test_scores_are_controller_owned_and_ties_use_declared_order(self) -> None:
        fast = self.load("python", 80_000_000)
        slow = self.load("rust", 160_000_000)
        population = (fast, slow)
        fast_score = score_candidate(fast, population)
        slow_score = score_candidate(slow, population)
        self.assertTrue(fast_score.eligible)
        self.assertGreater(fast_score.total_points, slow_score.total_points)

        decision = decide_bakeoff(
            (evaluate_gates(fast), evaluate_gates(slow)),
            (fast_score, slow_score),
        )
        self.assertEqual(decision.status, "GO")
        self.assertEqual(decision.recommended_candidate, "python")
        self.assertFalse(decision.provisional)

    def test_no_go_when_every_candidate_fails(self) -> None:
        failed = self.load("python", 300_000_000)
        decision = decide_bakeoff((evaluate_gates(failed),))
        self.assertEqual(decision.status, "NO-GO")
        self.assertIsNone(decision.recommended_candidate)

    def test_decision_rejects_mismatched_score_population(self) -> None:
        python = self.load("python", 80_000_000)
        rust = self.load("rust", 160_000_000)
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            decide_bakeoff(
                (evaluate_gates(python),),
                (score_candidate(rust, (rust,)),),
            )


if __name__ == "__main__":
    unittest.main()
