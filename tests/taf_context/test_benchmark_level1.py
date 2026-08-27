"""Integration tests for immutable Level 1 benchmark evidence retention."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from .benchmark_level1 import (
    BenchmarkSample,
    level1_sample_schedule,
    main,
    retain_candidate_evidence,
    run_benchmark_schedule,
)
from .level1_scoring import GateStatus
from .test_level1_scoring import evidence_lines


class BenchmarkRetentionTests(unittest.TestCase):
    def test_cli_retains_an_explicit_unsupported_candidate_decision(self) -> None:
        candidate = {
            "schema_version": "1",
            "candidate_identity": "go",
            "candidate_version": "0.1.0",
            "language": "Go",
            "protocol_version": "1",
            "availability": "unsupported",
            "unsupported_reason_codes": ["unsupported-toolchain"],
            "executable": "",
            "arguments": [],
            "environment_allowlist": [],
            "declared_child_processes": [],
            "dependency_lock": "",
            "license_inventory": "",
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "candidate.json"
            manifest.write_text(json.dumps(candidate), encoding="utf-8")
            evidence_root = root / "evidence"

            exit_code = main(
                [
                    "--candidate-manifest", str(manifest),
                    "--corpus-class", "small",
                    "--evidence-root", str(evidence_root),
                    "--reference-machine", "macos-arm64",
                ]
            )

            support = json.loads((evidence_root / "unsupported.jsonl").read_text(encoding="utf-8"))
            decision = json.loads((evidence_root / "decision.json").read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 0)
        self.assertEqual(support["reason_codes"], ["unsupported-toolchain"])
        self.assertEqual(decision["status"], "NO-GO")
        self.assertIsNone(decision["recommended_candidate"])

    def test_cli_preflight_failure_becomes_explicit_unsupported_evidence(self) -> None:
        candidate = {
            "schema_version": "1",
            "candidate_identity": "missing",
            "candidate_version": "0.1.0",
            "language": "Python",
            "protocol_version": "1",
            "availability": "ready",
            "unsupported_reason_codes": [],
            "executable": "missing.py",
            "arguments": [],
            "environment_allowlist": [],
            "declared_child_processes": [],
            "dependency_lock": "missing.lock",
            "license_inventory": "missing-licenses.json",
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "candidate.json"
            manifest.write_text(json.dumps(candidate), encoding="utf-8")
            evidence_root = root / "evidence"
            exit_code = main(
                [
                    "--candidate-manifest", str(manifest),
                    "--corpus-class", "small",
                    "--evidence-root", str(evidence_root),
                    "--reference-machine", "macos-arm64",
                ]
            )
            support = json.loads((evidence_root / "unsupported.jsonl").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertIn("unsafe-executable", support["reason_codes"])

    def test_cli_builds_a_disposable_corpus_and_writes_a_derived_decision(self) -> None:
        fixture = evidence_lines()
        by_sample = {str(line["sample_identity"]): line for line in fixture[1:]}

        def factory(*_args: object) -> object:
            def execute(sample: BenchmarkSample) -> dict[str, object]:
                return {
                    key: value
                    for key, value in by_sample[sample.sample_identity].items()
                    if key not in {
                        "record_type", "sample_identity", "environment_identity",
                        "candidate_identity", "candidate_digest", "phase",
                        "vector_identity", "retained", "ordinal", "permuted",
                        "query_class",
                    }
                }
            return execute

        candidate = {
            "schema_version": "1",
            "candidate_identity": "python",
            "candidate_version": "0.1.0",
            "language": "Python",
            "protocol_version": "1",
            "availability": "ready",
            "unsupported_reason_codes": [],
            "executable": "candidate.py",
            "arguments": [],
            "environment_allowlist": [],
            "declared_child_processes": [],
            "dependency_lock": "candidate.lock",
            "license_inventory": "licenses.json",
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "candidate.json"
            manifest.write_text(json.dumps(candidate), encoding="utf-8")
            evidence_root = root / "evidence"

            exit_code = main(
                [
                    "--candidate-manifest", str(manifest),
                    "--corpus-class", "small",
                    "--evidence-root", str(evidence_root),
                    "--reference-machine", "macos-arm64",
                ],
                execute_factory=factory,
            )

            decision = json.loads((evidence_root / "decision.json").read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 0)
        self.assertEqual(decision["status"], "GO")
        self.assertEqual(decision["recommended_candidate"], "python")

    def test_controller_runs_the_exact_declared_schedule(self) -> None:
        fixture = evidence_lines()
        by_sample = {
            str(line["sample_identity"]): line
            for line in fixture[1:]
        }
        observed: list[BenchmarkSample] = []

        def execute(sample: BenchmarkSample) -> dict[str, object]:
            observed.append(sample)
            source = by_sample[sample.sample_identity]
            return {
                key: value
                for key, value in source.items()
                if key not in {
                    "record_type",
                    "sample_identity",
                    "environment_identity",
                    "candidate_identity",
                    "candidate_digest",
                    "phase",
                    "vector_identity",
                    "retained",
                    "ordinal",
                    "permuted",
                    "query_class",
                }
            }

        with tempfile.TemporaryDirectory() as temp:
            evidence, report = run_benchmark_schedule(
                run_identity="run-python",
                environment_identity="macos-arm64",
                candidate_identity="python",
                candidate_digest="sha256:" + "1" * 64,
                corpus_identity="sha256:" + "3" * 64,
                evidence_root=Path(temp) / "evidence",
                execute=execute,
            )

        schedule = level1_sample_schedule()
        self.assertEqual(tuple(observed), schedule)
        self.assertEqual(len(schedule), 259)
        self.assertEqual(sum(item.record_type == "rebuild" for item in schedule), 8)
        self.assertEqual(sum(item.record_type == "update" for item in schedule), 6)
        self.assertEqual(sum(item.record_type == "query" for item in schedule), 147)
        self.assertEqual(sum(item.record_type == "determinism" for item in schedule), 96)
        self.assertEqual(evidence.candidate_identity, "python")
        self.assertIs(report.gate_statuses["correctness"], GateStatus.PASS)

    def test_executor_cannot_override_controller_owned_schedule_fields(self) -> None:
        def execute(_sample: BenchmarkSample) -> dict[str, object]:
            return {"sample_identity": "forged"}

        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "controller-owned"):
                run_benchmark_schedule(
                    run_identity="run-forged",
                    environment_identity="macos-arm64",
                    candidate_identity="python",
                    candidate_digest="sha256:" + "1" * 64,
                    corpus_identity="sha256:" + "3" * 64,
                    evidence_root=Path(temp) / "evidence",
                    execute=execute,
                )

    def test_failed_schedule_retains_bounded_partial_leaves_without_error_text(self) -> None:
        fixture = evidence_lines()
        by_sample = {str(line["sample_identity"]): line for line in fixture[1:]}
        calls = 0

        def execute(sample: BenchmarkSample) -> dict[str, object]:
            nonlocal calls
            calls += 1
            if calls == 4:
                raise RuntimeError("token=must-not-be-retained")
            return {
                key: value
                for key, value in by_sample[sample.sample_identity].items()
                if key not in {
                    "record_type", "sample_identity", "environment_identity",
                    "candidate_identity", "candidate_digest", "phase",
                    "vector_identity", "retained", "ordinal", "permuted",
                    "query_class",
                }
            }

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "evidence"
            with self.assertRaises(RuntimeError):
                run_benchmark_schedule(
                    run_identity="run-partial",
                    environment_identity="macos-arm64",
                    candidate_identity="python",
                    candidate_digest="sha256:" + "1" * 64,
                    corpus_identity="sha256:" + "3" * 64,
                    evidence_root=root,
                    execute=execute,
                )
            partial = (root / "partial-evidence.jsonl").read_text(encoding="ascii").splitlines()
            failure = json.loads((root / "failure.json").read_text(encoding="ascii"))
            complete_exists = (root / "evidence.jsonl").exists()

        self.assertEqual(len(partial), 4)
        self.assertFalse(complete_exists)
        self.assertEqual(failure["exception_type"], "RuntimeError")
        self.assertNotIn("must-not-be-retained", json.dumps(failure))

    def test_complete_run_is_fsynced_reopened_and_summarized(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "evidence"
            evidence, report = retain_candidate_evidence(evidence_lines(), root)

            self.assertEqual(evidence.candidate_identity, "python")
            self.assertIs(report.gate_statuses["correctness"], GateStatus.PASS)
            self.assertTrue((root / "evidence.jsonl").is_file())
            summary = json.loads((root / "gate-report.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["candidate_identity"], "python")
            self.assertEqual(summary["warm_query_p95_ns"], 100_000_000)

    def test_retry_cannot_overwrite_a_complete_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "evidence"
            retain_candidate_evidence(evidence_lines(), root)
            retained = (root / "evidence.jsonl").read_bytes()

            with self.assertRaises(FileExistsError):
                retain_candidate_evidence(evidence_lines("rust"), root)

            self.assertEqual((root / "evidence.jsonl").read_bytes(), retained)

    def test_leaky_candidate_is_retained_honestly_but_ineligible(self) -> None:
        lines = evidence_lines("leaky")
        query = next(line for line in lines if line.get("record_type") == "query" and line.get("retained"))
        query["leak_detected"] = True
        with tempfile.TemporaryDirectory() as temp:
            _evidence, report = retain_candidate_evidence(lines, Path(temp) / "leaky")
        self.assertIs(report.gate_statuses["safety"], GateStatus.FAIL)
        self.assertIn("gate-safety", report.failure_reason_codes)


if __name__ == "__main__":
    unittest.main()
