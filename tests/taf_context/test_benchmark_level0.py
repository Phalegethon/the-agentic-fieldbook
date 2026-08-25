"""Fail-closed regression tests for the Level 0 benchmark harness."""

from __future__ import annotations

import builtins
import _io
import io
import json
import mmap
import os
from pathlib import Path
import socket
import _socket
import subprocess
import tempfile
import unittest
from unittest import mock

from tests.taf_context import benchmark_level0 as benchmark


class InstrumentationGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.clean = self.repo / "clean.txt"
        self.dirty = self.repo / "dirty.txt"
        self.clean.write_bytes(b"clean bytes")
        self.dirty.write_bytes(b"dirty bytes")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_popen_rejects_git_option_remote_and_llm_bypasses(self) -> None:
        commands = (
            ["git", "-c", "color.ui=false", "status"],
            ["git", "archive", "--remote=example.invalid:repo", "HEAD"],
            ["codex", "--version"],
            ["python3", "-c", "print('bypass')"],
        )
        with mock.patch("subprocess.Popen") as underlying:
            with benchmark._guards(self.repo, ["dirty.txt"]) as counters:
                for command in commands:
                    with self.subTest(command=command):
                        with self.assertRaises(benchmark.InstrumentationViolation):
                            subprocess.Popen(command, cwd=self.repo)
        underlying.assert_not_called()
        self.assertEqual(counters["rejected_process_calls"], 4)
        self.assertEqual(counters["network_calls"], 2)
        self.assertEqual(counters["llm_calls"], 1)

    def test_socket_construction_and_preexisting_udp_sendto_are_blocked(self) -> None:
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(udp.close)
        with benchmark._guards(self.repo, ["dirty.txt"]) as counters:
            with self.assertRaises(benchmark.InstrumentationViolation):
                socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            with self.assertRaises(benchmark.InstrumentationViolation):
                udp.sendto(b"blocked", ("127.0.0.1", 9))
        self.assertEqual(counters["network_calls"], 2)

    def test_native_socket_constructor_aliases_are_blocked(self) -> None:
        with benchmark._guards(self.repo, ["dirty.txt"]) as counters:
            for constructor in (socket.SocketType, _socket.socket):
                with self.subTest(constructor=constructor):
                    try:
                        created = constructor(socket.AF_INET, socket.SOCK_DGRAM)
                    except benchmark.InstrumentationViolation:
                        continue
                    created.close()
                    self.fail("native socket constructor alias was not blocked")
        self.assertEqual(counters["network_calls"], 2)

    def test_open_surfaces_measure_actual_returned_bytes_once(self) -> None:
        with benchmark._guards(self.repo, ["dirty.txt"]) as counters:
            with Path(self.dirty).open("rb") as source:
                self.assertEqual(source.read(), b"dirty bytes")
            with builtins.open(self.clean, "rb") as source:
                self.assertEqual(source.read(5), b"clean")
            with io.open(self.clean, "rb") as source:
                self.assertEqual(source.read(), b"clean bytes")
        self.assertEqual(counters["measured_dirty_bytes_read"], 11)
        self.assertEqual(counters["measured_clean_bytes_read"], 16)
        self.assertEqual(counters["dirty_file_content_reads"], 1)
        self.assertEqual(counters["clean_file_content_reads"], 2)

    def test_native_open_and_wrapped_stream_escape_hatches_are_blocked(self) -> None:
        with benchmark._guards(self.repo, ["dirty.txt"]) as counters:
            with _io.open(self.dirty, "rb") as source:
                self.assertEqual(source.read(), b"dirty bytes")
            with builtins.open(self.clean, "rb") as source:
                with self.assertRaises(benchmark.InstrumentationViolation):
                    source.raw.read()
            for attribute in ("buffer", "fileno", "detach"):
                with self.subTest(attribute=attribute):
                    with builtins.open(self.clean, "rb") as source:
                        with self.assertRaises(benchmark.InstrumentationViolation):
                            escaped = getattr(source, attribute)
                            if callable(escaped):
                                escaped()
        self.assertEqual(counters["measured_dirty_bytes_read"], 11)
        self.assertEqual(counters["measured_clean_bytes_read"], 0)
        self.assertEqual(counters["native_bypass_rejections"], 4)

    def test_fd_reads_are_measured_and_mmap_is_rejected(self) -> None:
        with benchmark._guards(self.repo, ["dirty.txt"]) as counters:
            descriptor = os.open(self.dirty, os.O_RDONLY)
            try:
                self.assertEqual(os.read(descriptor, 5), b"dirty")
                with self.assertRaises(benchmark.InstrumentationViolation):
                    mmap.mmap(descriptor, 0, access=mmap.ACCESS_READ)
            finally:
                os.close(descriptor)
            with self.assertRaises(benchmark.InstrumentationViolation):
                io.FileIO(self.clean, "r")
        self.assertEqual(counters["measured_dirty_bytes_read"], 5)
        self.assertEqual(counters["native_bypass_rejections"], 2)


class EvidenceSemanticsTests(unittest.TestCase):
    def test_fixture_git_run_uses_hermetic_configuration(self) -> None:
        completed = subprocess.CompletedProcess(["git", "status"], 0, "", "")
        with mock.patch("subprocess.run", return_value=completed) as run:
            benchmark._run(Path("/fixture"), ["git", "status"])
        environment = run.call_args.kwargs["env"]
        self.assertEqual(environment["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(environment["GIT_OPTIONAL_LOCKS"], "0")
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")

    @staticmethod
    def _ok_sample() -> dict:
        return {
            "status": "ok",
            "correctness_passed": True,
            "correctness_failure": False,
            "performance_failure": False,
            "cold_wall_seconds": 0.2,
            "warm_wall_seconds": 0.1,
            "cold_cpu_seconds": 0.1,
            "warm_cpu_seconds": 0.05,
            "peak_rss_bytes": 1024,
            "paths_inspected": 10,
            "dirty_bytes_hashed": 0,
            "dossier_characters": 10,
            "reported_dossier_characters": 10,
            "measured_dirty_bytes_read": 0,
            "eligible_dirty_bytes": 0,
            "clean_file_content_reads": 0,
            "dirty_file_content_reads": 0,
            "measured_clean_bytes_read": 0,
            "artifact_sizes_bytes": {
                "manifest.json": 20,
                "snapshot.json": 70,
                "dossier.md": 10,
            },
            "artifact_total_bytes": 100,
            "network_calls": 0,
            "llm_calls": 0,
            "allowed_process_calls": 12,
            "rejected_process_calls": 0,
            "native_bypass_rejections": 0,
            "correctness_checks": {"valid": True},
            "git_commands": [["status"]],
        }

    def test_dossier_characters_are_decoded_independently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "manifest.json").write_bytes(b"{}\n")
            (output / "snapshot.json").write_bytes(b"{}\n")
            dossier = "# Résumé 😀\n"
            (output / "dossier.md").write_text(dossier, encoding="utf-8")
            metrics = benchmark._read_artifact_metrics(
                output, {"dossier_characters": 1}
            )
        self.assertEqual(metrics["dossier_characters"], len(dossier))
        self.assertNotEqual(metrics["dossier_characters"], 1)

    def test_retention_decision_requires_correctness_and_gates(self) -> None:
        self.assertIn("Retain Python", benchmark._retention_decision(True, True))
        for correctness, gates in ((False, True), (True, False), (False, False)):
            with self.subTest(correctness=correctness, gates=gates):
                self.assertIn(
                    "reference implementation",
                    benchmark._retention_decision(correctness, gates),
                )

    def test_driver_describes_the_actual_warm_timer_boundary(self) -> None:
        completed_class = {
            "correctness_passed": True,
            "mandatory_gates_passed": True,
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence.json"
            with mock.patch.object(benchmark, "CASES", ({},)), mock.patch.object(
                benchmark, "_case_result", return_value=completed_class
            ), mock.patch.object(benchmark, "_machine", return_value={}):
                self.assertEqual(benchmark._driver(output), 0)
            evidence = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(
            evidence["timing_definitions"]["warm"],
            "worker timer excludes guard setup and collector import; includes CLI parsing, collection, rendering, and artifact emission",
        )

    def test_timeout_is_retained_as_performance_failure(self) -> None:
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(["worker"], 180),
        ):
            sample = benchmark._measured_worker(
                Path("/fixture"), Path("/output"), 10, []
            )
        self.assertEqual(sample["status"], "timeout")
        self.assertTrue(sample["performance_failure"])
        self.assertFalse(sample["correctness_failure"])

    def test_missing_and_malformed_worker_json_are_retained(self) -> None:
        malformed = (
            subprocess.CompletedProcess(["worker"], 0, "not-json", ""),
            subprocess.CompletedProcess(["worker"], 0, "", ""),
        )
        for completed in malformed:
            with self.subTest(stdout=completed.stdout):
                with mock.patch("subprocess.run", return_value=completed):
                    sample = benchmark._measured_worker(
                        Path("/fixture"), Path("/output"), 10, []
                    )
                self.assertEqual(sample["status"], "worker-protocol-error")
                self.assertTrue(sample["correctness_failure"])
                json.dumps(sample)

    def test_case_preserves_prior_samples_when_a_later_run_times_out(self) -> None:
        timeout = {
            "status": "timeout",
            "error": "worker exceeded timeout",
            "correctness_passed": None,
            "correctness_failure": False,
            "performance_failure": True,
            "cold_wall_seconds": 180.0,
            "cold_cpu_seconds": 0.1,
        }
        outcomes = [self._ok_sample()]
        outcomes.extend([self._ok_sample(), timeout])
        outcomes.extend([self._ok_sample() for _ in range(3)])
        completed = subprocess.CompletedProcess(["git", "status"], 0, "", "")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            with mock.patch.object(
                benchmark, "_fixture", return_value=(repo, [])
            ), mock.patch.object(
                benchmark, "_measured_worker", side_effect=outcomes
            ), mock.patch.object(benchmark, "_run", return_value=completed):
                result = benchmark._case_result(
                    root / "case",
                    {"tracked_files": 10, "dirty_files": 0, "warm_p95_seconds": 2.0},
                )
        self.assertEqual(len(result["samples"]), 5)
        self.assertEqual(result["samples"][0]["status"], "ok")
        self.assertEqual(result["samples"][1]["status"], "timeout")
        self.assertTrue(result["correctness_passed"])
        self.assertFalse(result["mandatory_gates_passed"])
        self.assertEqual(result["performance_failures"][0]["run"], 2)

    def test_structurally_malformed_ok_sample_is_retained_after_prior_success(self) -> None:
        malformed_json = json.dumps(
            {
                "status": "ok",
                "correctness_passed": True,
                "warm_wall_seconds": 0.1,
            }
        )
        completed = subprocess.CompletedProcess(["worker"], 0, malformed_json, "")
        with mock.patch("subprocess.run", return_value=completed):
            malformed = benchmark._measured_worker(
                Path("/fixture"), Path("/output"), 10, []
            )
        self.assertEqual(malformed["status"], "worker-structure-error")
        self.assertTrue(malformed["correctness_failure"])

        outcomes = [self._ok_sample(), self._ok_sample(), malformed]
        outcomes.extend([self._ok_sample() for _ in range(3)])
        status = subprocess.CompletedProcess(["git", "status"], 0, "", "")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            with mock.patch.object(
                benchmark, "_fixture", return_value=(repo, [])
            ), mock.patch.object(
                benchmark, "_measured_worker", side_effect=outcomes
            ), mock.patch.object(benchmark, "_run", return_value=status):
                result = benchmark._case_result(
                    root / "case",
                    {"tracked_files": 10, "dirty_files": 0, "warm_p95_seconds": 2.0},
                )
        self.assertEqual(len(result["samples"]), 5)
        self.assertEqual(result["samples"][0]["status"], "ok")
        self.assertEqual(result["samples"][1]["status"], "worker-structure-error")
        self.assertFalse(result["correctness_passed"])
        self.assertFalse(result["mandatory_gates_passed"])

    def test_huge_integer_metric_is_retained_without_overflow_or_sample_loss(self) -> None:
        huge = self._ok_sample()
        huge["warm_wall_seconds"] = 10**400
        completed = subprocess.CompletedProcess(
            ["worker"], 0, json.dumps(huge), ""
        )
        with mock.patch("subprocess.run", return_value=completed):
            malformed = benchmark._measured_worker(
                Path("/fixture"), Path("/output"), 10, []
            )
        self.assertEqual(malformed["status"], "worker-structure-error")
        self.assertTrue(malformed["correctness_failure"])

        outcomes = [self._ok_sample(), self._ok_sample(), malformed]
        outcomes.extend([self._ok_sample() for _ in range(3)])
        status = subprocess.CompletedProcess(["git", "status"], 0, "", "")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            with mock.patch.object(
                benchmark, "_fixture", return_value=(repo, [])
            ), mock.patch.object(
                benchmark, "_measured_worker", side_effect=outcomes
            ), mock.patch.object(benchmark, "_run", return_value=status):
                result = benchmark._case_result(
                    root / "case",
                    {"tracked_files": 10, "dirty_files": 0, "warm_p95_seconds": 2.0},
                )
        self.assertEqual(len(result["samples"]), 5)
        self.assertEqual(result["samples"][0]["status"], "ok")
        self.assertEqual(result["samples"][1]["status"], "worker-structure-error")
        self.assertFalse(result["correctness_passed"])
        self.assertFalse(result["mandatory_gates_passed"])


if __name__ == "__main__":
    unittest.main()
