"""Strict replacement-v2 evidence, gates, scores, and decisions."""

from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

from .level1_replacement_scoring import (
    ReplacementEvidence,
    ReplacementGateStatus,
    decide_replacement_bakeoff,
    evaluate_replacement_gates,
    score_replacement_candidates,
)


SHA = "sha256:" + "1" * 64


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def evidence_lines(candidate: str = "rust-compact") -> list[dict[str, object]]:
    candidate_digit = "2" if candidate == "rust-compact" else "3"
    common = {
        "environment_identity": "linux-arm64-container",
        "policy_identity": "sha256:" + "a" * 64,
        "candidate_identity": candidate,
        "candidate_digest": "sha256:" + candidate_digit * 64,
    }
    lines: list[dict[str, object]] = [
        {
            "record_type": "run",
            "schema_version": "1",
            "evidence_version": "replacement-v2",
            "run_identity": "replacement-run-0001",
            "corpus_identity": "sha256:" + "4" * 64,
            **common,
        },
        {
            "record_type": "product",
            "sample_identity": "product-metadata",
            "artifact_size_bytes": 4_000_000,
            "cross_platform_targets": 5,
            "dependency_license_checks_passed": 4,
            "dependency_license_checks_total": 4,
            "maintenance_checks_passed": 4,
            "maintenance_checks_total": 4,
            "unsafe_code_blocks": 0,
            **common,
        },
    ]
    for ordinal in range(5):
        lines.append(
            {
                "record_type": "build",
                "sample_identity": f"build-{ordinal}",
                "retained": True,
                "ordinal": ordinal,
                "elapsed_ns": 10_000_000_000,
                "peak_rss_bytes": 512 * 1024 * 1024,
                "index_and_staging_bytes": 3_000_000,
                "relevant_source_bytes": 2_000_000,
                "index_digest": SHA,
                "repository_writes": 0,
                "network_attempts": 0,
                "undeclared_child_processes": 0,
                "state_escapes": 0,
                **common,
            }
        )
    for query_class in ("exact", "fuzzy"):
        for ordinal in range(20):
            lines.append(
                {
                    "record_type": "query",
                    "sample_identity": f"query-{query_class}-{ordinal}",
                    "query_class": query_class,
                    "retained": True,
                    "ordinal": ordinal,
                    "elapsed_ns": 150_000_000,
                    "expected_result_identities": [SHA],
                    "actual_result_identities": [SHA],
                    "citation_match": True,
                    "evidence_class_match": True,
                    "freshness_honest": True,
                    "forbidden_result_count": 0,
                    "response_characters": 2000,
                    "budget_characters": 2000,
                    "repository_files_opened": 32,
                    "repository_bytes_read": 4 * 1024 * 1024,
                    "considered_records": 4096,
                    "full_repository_operations": 0,
                    "repository_writes": 0,
                    "network_attempts": 0,
                    "undeclared_child_processes": 0,
                    "state_escapes": 0,
                    **common,
                }
            )
    for ordinal in range(5):
        lines.append(
            {
                "record_type": "update",
                "sample_identity": f"update-{ordinal}",
                "retained": True,
                "ordinal": ordinal,
                "elapsed_ns": 2_000_000_000,
                "declared_changed_files": 1,
                "enumerated_repository_files": 1,
                "parsed_repository_files": 1,
                "incremental_digest": SHA,
                "rebuild_digest": SHA,
                "repository_writes": 0,
                "network_attempts": 0,
                "undeclared_child_processes": 0,
                "state_escapes": 0,
                **common,
            }
        )
    lines.append(
        {
            "record_type": "index-determinism",
            "sample_identity": "index-determinism",
            "first_index_digest": SHA,
            "second_index_digest": SHA,
            **common,
        }
    )
    return lines


def write_evidence(path: Path, lines: list[dict[str, object]]) -> None:
    path.write_text("".join(canonical(line) + "\n" for line in lines), encoding="utf-8")


class ReplacementEvidenceTests(unittest.TestCase):
    def load(self, lines: list[dict[str, object]]) -> ReplacementEvidence:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "evidence.jsonl"
        write_evidence(path, lines)
        return ReplacementEvidence.from_jsonl(path)

    def test_valid_boundary_evidence_round_trips_and_passes_every_gate(self) -> None:
        report = evaluate_replacement_gates(self.load(evidence_lines()))

        self.assertIs(report.support_status, ReplacementGateStatus.PASS)
        self.assertTrue(all(value is ReplacementGateStatus.PASS for value in report.gate_statuses.values()))
        self.assertEqual(report.exact_top_five_recall, 1.0)
        self.assertEqual(report.fuzzy_top_ten_recall, 1.0)
        self.assertEqual(report.storage_ratio, 1.5)
        self.assertEqual(report.warm_query_p95_ns, 150_000_000)

    def test_each_numeric_hard_gate_fails_one_unit_beyond_boundary(self) -> None:
        mutations = {
            "query-latency": ("query", "elapsed_ns", 150_000_001),
            "query-files": ("query", "repository_files_opened", 33),
            "query-bytes": ("query", "repository_bytes_read", 4 * 1024 * 1024 + 1),
            "query-records": ("query", "considered_records", 4097),
            "full-repository-work": ("query", "full_repository_operations", 1),
            "build": ("build", "elapsed_ns", 10_000_000_001),
            "memory": ("build", "peak_rss_bytes", 512 * 1024 * 1024 + 1),
            "storage": ("build", "index_and_staging_bytes", 3_000_001),
            "incremental": ("update", "elapsed_ns", 2_000_000_001),
            "update-locality": ("update", "enumerated_repository_files", 2),
        }
        for gate, (record_type, field, value) in mutations.items():
            with self.subTest(gate=gate):
                lines = evidence_lines()
                for line in lines:
                    if line.get("record_type") == record_type:
                        line[field] = value
                report = evaluate_replacement_gates(self.load(lines))
                self.assertIs(report.gate_statuses[gate], ReplacementGateStatus.FAIL)

    def test_retrieval_integrity_and_isolation_fail_closed(self) -> None:
        mutations = {
            "retrieval": ("query", "actual_result_identities", []),
            "citations": ("query", "citation_match", False),
            "evidence": ("query", "evidence_class_match", False),
            "freshness": ("query", "freshness_honest", False),
            "forbidden-results": ("query", "forbidden_result_count", 1),
            "model-budget": ("query", "response_characters", 2001),
            "repository-read-only": ("query", "repository_writes", 1),
            "network-offline": ("query", "network_attempts", 1),
            "child-processes": ("query", "undeclared_child_processes", 1),
            "state-boundary": ("query", "state_escapes", 1),
            "incremental-equivalence": ("update", "rebuild_digest", "sha256:" + "9" * 64),
            "index-determinism": ("index-determinism", "second_index_digest", "sha256:" + "8" * 64),
        }
        for gate, (record_type, field, value) in mutations.items():
            with self.subTest(gate=gate):
                lines = evidence_lines()
                target = next(line for line in lines if line["record_type"] == record_type)
                target[field] = value
                report = evaluate_replacement_gates(self.load(lines))
                self.assertIs(report.gate_statuses[gate], ReplacementGateStatus.FAIL)

    def test_malformed_mixed_and_prior_v1_evidence_are_rejected(self) -> None:
        cases = []
        extra = evidence_lines()
        extra[1]["candidate_summary"] = "pass"
        cases.append(extra)
        boolean_counter = evidence_lines()
        boolean_counter[2]["elapsed_ns"] = True
        cases.append(boolean_counter)
        nonfinite = evidence_lines()
        nonfinite[2]["elapsed_ns"] = math.nan
        cases.append(nonfinite)
        mixed = evidence_lines()
        mixed[2]["policy_identity"] = "sha256:" + "f" * 64
        cases.append(mixed)
        prior = evidence_lines()
        prior[0]["evidence_version"] = "level1-v1"
        cases.append(prior)

        for ordinal, lines in enumerate(cases):
            with self.subTest(ordinal=ordinal), self.assertRaises(ValueError):
                self.load(lines)

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "duplicate.jsonl"
            raw = canonical(evidence_lines()[0])
            raw = raw[:-1] + ',"schema_version":"1"}\n'
            path.write_text(raw, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate"):
                ReplacementEvidence.from_jsonl(path)

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "blank.jsonl"
            path.write_text("\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                ReplacementEvidence.from_jsonl(path)


class ReplacementDecisionTests(unittest.TestCase):
    def load(self, candidate: str) -> ReplacementEvidence:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / f"{candidate}.jsonl"
        write_evidence(path, evidence_lines(candidate))
        return ReplacementEvidence.from_jsonl(path)

    def test_two_complete_finalists_are_required_before_naming_a_winner(self) -> None:
        rust = self.load("rust-compact")
        rust_report = evaluate_replacement_gates(rust)
        rust_score = score_replacement_candidates((rust,))[0]

        decision = decide_replacement_bakeoff((rust_report,), (rust_score,))

        self.assertEqual(decision.status, "NO-GO")
        self.assertIsNone(decision.recommended_candidate)
        self.assertIn("insufficient-complete-finalists", decision.reason_codes)

    def test_eligible_population_scores_and_decision_are_deterministic(self) -> None:
        rust = self.load("rust-compact")
        go = self.load("go-compact")
        scores = score_replacement_candidates((go, rust))
        reports = tuple(evaluate_replacement_gates(item) for item in (go, rust))

        decision = decide_replacement_bakeoff(tuple(reversed(reports)), tuple(reversed(scores)))

        self.assertEqual(decision.status, "GO")
        self.assertEqual(decision.recommended_candidate, "go-compact")
        self.assertEqual(tuple(score.candidate_identity for score in scores), ("go-compact", "rust-compact"))

    def test_failed_candidate_cannot_distort_eligible_scores(self) -> None:
        go = self.load("go-compact")
        rust = self.load("rust-compact")
        failed_lines = evidence_lines("python-compact")
        for line in failed_lines:
            if line.get("record_type") == "query":
                line["elapsed_ns"] = 900_000_000
        failed = self.load_from_lines(failed_lines)

        baseline = score_replacement_candidates((go, rust))
        with_failed = score_replacement_candidates((failed, go, rust))

        self.assertEqual(
            tuple(score.total_points for score in with_failed if score.eligible),
            tuple(score.total_points for score in baseline),
        )

    def test_mixed_policy_environment_or_corpus_cannot_be_compared(self) -> None:
        rust = self.load("rust-compact")
        mutations = (
            ("policy_identity", "sha256:" + "f" * 64),
            ("environment_identity", "different-environment"),
            ("corpus_identity", "sha256:" + "e" * 64),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                lines = evidence_lines("go-compact")
                lines[0][field] = value
                if field in {"policy_identity", "environment_identity"}:
                    for line in lines[1:]:
                        line[field] = value
                other = self.load_from_lines(lines)
                with self.assertRaisesRegex(ValueError, "mixed comparison"):
                    score_replacement_candidates((rust, other))

    def load_from_lines(self, lines: list[dict[str, object]]) -> ReplacementEvidence:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "candidate.jsonl"
        write_evidence(path, lines)
        return ReplacementEvidence.from_jsonl(path)


if __name__ == "__main__":
    unittest.main()
