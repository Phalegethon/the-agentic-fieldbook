"""Fail-closed regressions for the discovery and routing benchmark."""

from __future__ import annotations

import builtins
import _io
import io
import json
import math
import mmap
import os
from pathlib import Path
import socket
import _socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.taf_context import benchmark_discovery as benchmark


class InstrumentationGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "source.txt"
        self.path.write_bytes(b"forbidden source")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_prebound_python_fd_mmap_and_metadata_aliases_fail_closed(self) -> None:
        prebound = {
            "builtin-open": lambda: builtins.open(self.path, "rb"),
            "native-open": lambda: _io.open(self.path, "rb"),
            "fileio": lambda: io.FileIO(self.path, "r"),
            "os-open": lambda: os.open(self.path, os.O_RDONLY),
            "stat": lambda: os.stat(self.path),
            "path-stat": lambda: Path.stat(self.path),
        }
        descriptor = os.open(self.path, os.O_RDONLY)
        self.addCleanup(os.close, descriptor)
        prebound["fd-read"] = lambda: os.read(descriptor, 1)
        prebound["mmap"] = lambda: mmap.mmap(descriptor, 0, access=mmap.ACCESS_READ)

        with benchmark._guards() as counters:
            for name, operation in prebound.items():
                with self.subTest(name=name):
                    with self.assertRaises(benchmark.InstrumentationViolation):
                        value = operation()
                        if hasattr(value, "close"):
                            value.close()

        self.assertGreaterEqual(counters["source_read_calls"], 6)
        self.assertGreaterEqual(counters["native_bypass_attempts"], 2)
        self.assertEqual(counters["source_bytes_read"], 0)

    def test_process_network_dns_llm_and_native_socket_aliases_fail_closed(self) -> None:
        prebound_popen = subprocess.Popen
        prebound_socket = socket.socket
        prebound_native_socket = _socket.socket
        prebound_getaddrinfo = socket.getaddrinfo

        operations = (
            ("provider", lambda: prebound_popen(["provider-tool", "--status"])),
            ("git", lambda: subprocess.Popen(["git", "status"])),
            ("llm", lambda: subprocess.Popen(["codex", "--version"])),
            ("socket", lambda: prebound_socket()),
            ("native-socket", lambda: prebound_native_socket()),
            ("dns", lambda: prebound_getaddrinfo("example.invalid", 443)),
        )
        with benchmark._guards() as counters:
            for name, operation in operations:
                with self.subTest(name=name):
                    with self.assertRaises(benchmark.InstrumentationViolation):
                        operation()

        self.assertEqual(counters["provider_process_calls"], 3)
        self.assertEqual(counters["git_process_calls"], 1)
        self.assertEqual(counters["llm_calls"], 1)
        self.assertEqual(counters["network_calls"], 3)
        self.assertGreaterEqual(counters["native_bypass_attempts"], 3)


class RetainedEvidenceTests(unittest.TestCase):
    @staticmethod
    def _case(name: str = "16x16-mixed-consent-freshness") -> dict[str, object]:
        return next(dict(item) for item in benchmark.CASES if item["name"] == name)

    @staticmethod
    def _ok_sample(case: dict[str, object] | None = None) -> dict[str, object]:
        case = case or RetainedEvidenceTests._case()
        expected = benchmark._expected_counts(case)
        zeros = {
            "audit_reads": 0,
            "audit_writes": 0,
            "state_writes": 0,
            "source_read_calls": 0,
            "source_bytes_read": 0,
            "provider_process_calls": 0,
            "git_process_calls": 0,
            "network_calls": 0,
            "llm_calls": 0,
            "native_bypass_attempts": 0,
        }
        return {
            "status": "ok",
            "case_name": case["name"],
            "run_index": 1,
            "correctness_passed": True,
            "correctness_failure": False,
            "performance_failure": False,
            "cold_wall_seconds": 0.02,
            "cold_cpu_seconds": 0.01,
            "warm_wall_seconds": 0.004,
            "warm_cpu_seconds": 0.003,
            "discovery_warm_seconds": 0.002,
            "routing_warm_seconds": 0.001,
            "consent_decision_warm_seconds": 0.001,
            "peak_rss_bytes": 1024,
            "host_inventory_bytes": 2048,
            "user_registry_bytes": 1024,
            "registration_bytes": 512,
            "consent_bytes": 1024,
            "request_bytes": 512,
            "discovery_input_bytes": 4096,
            "discovery_output_bytes": 8192,
            "routing_input_bytes": 9216,
            "routing_output_bytes": 1024,
            "model_summary_characters": 120,
            "discovery_artifact_bytes": 8192,
            "corpus_provider_count": case["provider_count"],
            "external_provider_count": case["provider_count"] - 1,
            "capabilities_per_external_provider": case["capabilities_per_provider"],
            "source_descriptor_count": case["provider_count"] - 1,
            "discovered_descriptor_count": expected["discovered_descriptor_count"],
            "discovered_capability_count": expected["discovered_capability_count"],
            "rejected_provider_count": expected["rejected_provider_count"],
            "omitted_provider_count": expected["omitted_provider_count"],
            "state_reads": 3,
            **zeros,
            "discovery_output_sha256": "a" * 64,
            "routing_output_sha256": "b" * 64,
            "reversed_discovery_output_sha256": "a" * 64,
            "reversed_routing_output_sha256": "b" * 64,
            "permuted_discovery_output_sha256": "a" * 64,
            "permuted_routing_output_sha256": "b" * 64,
            "correctness_checks": {name: True for name in benchmark.CHECK_NAMES},
            "worker_exit_code": 0,
        }

    def _completed_class(self, case: dict[str, object] | None = None) -> dict[str, object]:
        case = case or self._case()
        samples = []
        for index in range(5):
            sample = self._ok_sample(case)
            sample["run_index"] = index + 1
            samples.append(sample)
        return benchmark._assemble_class(case, self._ok_sample(case), samples)

    def test_missing_malformed_duplicate_and_nonfinite_worker_json_are_retained(self) -> None:
        payloads = ("", "not-json", '{"status":"ok","status":"ok"}', '{"x":NaN}')
        for payload in payloads:
            with self.subTest(payload=payload):
                completed = subprocess.CompletedProcess(["worker"], 0, payload, "")
                with mock.patch("subprocess.run", return_value=completed):
                    sample = benchmark._measured_worker(self._case(), 1)
                self.assertEqual(sample["status"], "worker-protocol-error")
                self.assertTrue(sample["correctness_failure"])
                json.dumps(sample, allow_nan=False)

    def test_timeout_is_retained_as_performance_failure(self) -> None:
        with mock.patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired(["worker"], 30)
        ):
            sample = benchmark._measured_worker(self._case(), 1)
        self.assertEqual(sample["status"], "timeout")
        self.assertTrue(sample["performance_failure"])
        self.assertFalse(sample["correctness_failure"])

    def test_oversized_worker_output_is_bounded_and_retained(self) -> None:
        payload = "x" * (benchmark.MAX_WORKER_OUTPUT_BYTES + 1)
        completed = subprocess.CompletedProcess(["worker"], 0, payload, "secret-stderr")
        with mock.patch("subprocess.run", return_value=completed):
            sample = benchmark._measured_worker(self._case(), 1)
        self.assertEqual(sample["status"], "worker-protocol-error")
        self.assertEqual(sample["worker_stdout_bytes"], len(payload))
        self.assertLessEqual(
            len(sample["worker_stdout_excerpt"].encode("utf-8")),
            benchmark.MAX_DIAGNOSTIC_BYTES,
        )

    def test_nonfinite_negative_and_oversized_metrics_fail_structure(self) -> None:
        mutations = (
            ("warm_wall_seconds", -0.1),
            ("warm_cpu_seconds", math.inf),
            ("peak_rss_bytes", 2**63),
            ("routing_output_bytes", -1),
        )
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                sample = self._ok_sample()
                sample[field] = value
                completed = subprocess.CompletedProcess(
                    ["worker"], 0, json.dumps(sample), ""
                )
                with mock.patch("subprocess.run", return_value=completed):
                    retained = benchmark._measured_worker(self._case(), 1)
                self.assertEqual(retained["status"], "worker-structure-error")
                self.assertTrue(retained["correctness_failure"])

    def test_wrong_counts_and_noncanonical_permutations_fail_structure(self) -> None:
        mutations = (
            ("discovered_descriptor_count", 1),
            ("discovered_capability_count", 1),
            ("omitted_provider_count", 99),
            ("reversed_discovery_output_sha256", "c" * 64),
            ("permuted_routing_output_sha256", "c" * 64),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                sample = self._ok_sample()
                sample[field] = value
                completed = subprocess.CompletedProcess(
                    ["worker"], 0, json.dumps(sample), ""
                )
                with mock.patch("subprocess.run", return_value=completed):
                    retained = benchmark._measured_worker(self._case(), 1)
                self.assertEqual(retained["status"], "worker-structure-error")

    def test_forbidden_counters_and_output_budgets_fail_structure(self) -> None:
        mutations = (
            ("source_bytes_read", 1),
            ("provider_process_calls", 1),
            ("network_calls", 1),
            ("llm_calls", 1),
            ("native_bypass_attempts", 1),
            ("model_summary_characters", 2001),
            ("routing_output_bytes", 16 * 1024 + 1),
            ("discovery_artifact_bytes", 256 * 1024 + 1),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                sample = self._ok_sample()
                sample[field] = value
                completed = subprocess.CompletedProcess(
                    ["worker"], 0, json.dumps(sample), ""
                )
                with mock.patch("subprocess.run", return_value=completed):
                    retained = benchmark._measured_worker(self._case(), 1)
                self.assertEqual(retained["status"], "worker-structure-error")

    def test_one_warmup_is_discarded_and_five_samples_are_retained(self) -> None:
        outcomes = [self._ok_sample() for _ in range(6)]
        with mock.patch.object(benchmark, "_measured_worker", side_effect=outcomes):
            result = benchmark._case_result(self._case())
        self.assertEqual(result["warmup"]["run_index"], 1)
        self.assertEqual(len(result["samples"]), 5)
        self.assertEqual([item["run_index"] for item in result["samples"]], [1] * 5)
        self.assertTrue(result["correctness_passed"])

    def test_incomplete_class_is_no_go_even_if_retained_samples_pass(self) -> None:
        result = benchmark._assemble_class(
            self._case(), self._ok_sample(), [self._ok_sample() for _ in range(4)]
        )
        self.assertFalse(result["correctness_passed"])
        self.assertFalse(result["mandatory_gates_passed"])
        self.assertFalse(result["gates"]["all_five_samples_retained"])

    def test_sixty_four_provider_latency_gates_use_fixed_thresholds(self) -> None:
        case = self._case("64x64-conflicts-denials-network-markers")
        samples = []
        for index in range(5):
            sample = self._ok_sample(case)
            sample["run_index"] = index + 1
            sample["routing_warm_seconds"] = 0.051 if index == 4 else 0.001
            sample["consent_decision_warm_seconds"] = 0.011 if index == 4 else 0.001
            samples.append(sample)
        result = benchmark._assemble_class(case, self._ok_sample(case), samples)
        self.assertFalse(result["gates"]["routing_warm_p95_at_most_0_050_seconds"])
        self.assertFalse(result["gates"]["consent_decision_warm_p95_at_most_0_010_seconds"])
        self.assertFalse(result["mandatory_gates_passed"])

    def test_strict_evidence_schema_rejects_unknown_fields(self) -> None:
        case = self._case()
        evidence = benchmark._evidence([self._completed_class(case)], machine={})
        benchmark._validate_evidence(evidence)
        evidence["unexpected"] = True
        with self.assertRaisesRegex(ValueError, "evidence fields"):
            benchmark._validate_evidence(evidence)

    def test_strict_evidence_schema_recomputes_aggregate_decision_flags(self) -> None:
        case = self._case()
        evidence = benchmark._evidence([self._completed_class(case)], machine={})
        evidence["correctness_passed"] = False
        evidence["mandatory_gates_passed"] = False
        evidence["python_retention_decision"] = benchmark._retention_decision(
            False, False
        )
        with self.assertRaisesRegex(ValueError, "evidence aggregate flags"):
            benchmark._validate_evidence(evidence)

    def test_partial_rerun_cannot_overwrite_valid_evidence(self) -> None:
        case = self._case()
        completed = self._completed_class(case)
        valid = benchmark._evidence([completed], machine={})
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "discovery-raw.json"
            output.write_text(benchmark._canonical_document(valid), encoding="utf-8")
            original = output.read_bytes()
            failed = benchmark._failed_class(case, RuntimeError("fixture failed"))
            with mock.patch.object(benchmark, "CASES", (case,)), mock.patch.object(
                benchmark, "_case_result", return_value=failed
            ), mock.patch.object(benchmark, "_machine", return_value={}):
                self.assertEqual(benchmark._driver(output), 1)
            self.assertEqual(output.read_bytes(), original)

    def test_partial_evidence_is_written_when_no_valid_evidence_exists(self) -> None:
        case = self._case()
        failed = benchmark._failed_class(case, RuntimeError("cleanup failed"))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "discovery-raw.json"
            with mock.patch.object(benchmark, "CASES", (case,)), mock.patch.object(
                benchmark, "_case_result", return_value=failed
            ), mock.patch.object(benchmark, "_machine", return_value={}):
                self.assertEqual(benchmark._driver(output), 1)
            evidence = json.loads(output.read_text(encoding="utf-8"))
        self.assertFalse(evidence["correctness_passed"])
        self.assertEqual(evidence["classes"][0]["status"], "class-error")

    def test_retention_decision_has_exact_contract_wording(self) -> None:
        self.assertEqual(
            benchmark._retention_decision(True, True),
            "GO — Retain Python for discovery and routing.",
        )
        for correctness, gates in ((False, True), (True, False), (False, False)):
            self.assertEqual(
                benchmark._retention_decision(correctness, gates),
                "NO-GO — Keep the contract; write a replacement bakeoff plan before production implementation continues.",
            )


if __name__ == "__main__":
    unittest.main()
