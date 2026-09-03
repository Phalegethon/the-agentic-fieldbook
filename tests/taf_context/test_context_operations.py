"""Tests for the transport seam and the extracted broker operations."""

from __future__ import annotations

import json
from pathlib import Path
import stat
import tempfile
import textwrap
import unittest

from taf_context.context_operations import (
    PrepareCLIError,
    QueryArguments,
    run_build,
    run_inspect,
    run_query,
    validate_query_request,
)
from taf_context.native_transport import NativeTransportError, OneShotTransport

from .repo_factory import init_committed_repo, write
from .test_prepare_cli import write_fake_native_engine


def _script(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class OneShotTransportTests(unittest.TestCase):
    def test_success_returns_stdout_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = _script(
                Path(directory) / "engine",
                "import sys; sys.stdout.write(sys.stdin.read().upper())\n",
            )
            self.assertEqual(
                OneShotTransport(binary).exchange(b'{"a":1}\n', idempotent=True), b'{"A":1}\n'
            )

    def test_non_zero_exit_is_rejected_with_the_stderr_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = _script(
                Path(directory) / "engine",
                "import sys; sys.stderr.write('invalid-native-level1-request\\n'); sys.exit(2)\n",
            )
            with self.assertRaises(NativeTransportError) as caught:
                OneShotTransport(binary).exchange(b"{}\n", idempotent=True)
            self.assertEqual(
                (caught.exception.reason, caught.exception.detail),
                ("rejected", "invalid-native-level1-request"),
            )

    def test_timeout_and_missing_binary_have_their_own_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = _script(Path(directory) / "engine", "import time; time.sleep(5)\n")
            with self.assertRaises(NativeTransportError) as caught:
                OneShotTransport(binary, timeout_seconds=0.2).exchange(b"{}\n", idempotent=True)
            self.assertEqual(caught.exception.reason, "timeout")
            with self.assertRaises(NativeTransportError) as missing:
                OneShotTransport(Path(directory) / "absent").exchange(b"{}\n", idempotent=True)
            self.assertEqual(missing.exception.reason, "invocation-failed")


class RecordingTransport:
    """Forwards to a fake engine binary and records what crossed the seam."""

    def __init__(self, binary: Path) -> None:
        self.inner = OneShotTransport(binary)
        self.frames: list[tuple[str, bool]] = []
        self.requests: list[dict] = []

    def exchange(self, wire: bytes, *, idempotent: bool) -> bytes:
        request = json.loads(wire)["request"]
        self.frames.append((request["operation"], idempotent))
        self.requests.append(request)
        return self.inner.exchange(wire, idempotent=idempotent)


class OperationTests(unittest.TestCase):
    def _environment(self, directory: str, binary: Path) -> dict[str, str]:
        return {
            "HOME": directory,
            "PATH": "",
            "TAF_LEVEL1_BINARY": str(binary),
            "TAF_STATE_HOME": str(Path(directory) / "state"),
        }

    def test_build_then_query_cross_the_seam_with_the_idempotency_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = init_committed_repo(Path(directory) / "repo")
            binary = Path(directory) / "engine"
            write_fake_native_engine(binary)
            environment = self._environment(directory, binary)
            transports: list[RecordingTransport] = []

            def transport_for(path: Path) -> RecordingTransport:
                transports.append(RecordingTransport(path))
                return transports[-1]

            built = run_build(repository, environment=environment, transport_for=transport_for)
            self.assertEqual((built["mode"], built["next_safe_action"]), ("build", "use-index"))
            arguments = QueryArguments("search-symbols", "main", (), [], [], [], [], 8, 4000, False)
            queried = run_query(
                repository, arguments, environment=environment, transport_for=transport_for
            )
            self.assertEqual(
                (queried["mode"], queried["operation"], queried["status"]),
                ("query", "search-symbols", "ready"),
            )
            inspected = run_inspect(repository, environment=environment, transport_for=transport_for)
            self.assertEqual((inspected["mode"], inspected["next_safe_action"]), ("inspect", "use-index"))
            frames = [frame for transport in transports for frame in transport.frames]
            self.assertEqual(frames, [("build", False), ("search-symbols", True), ("status", True)])

    def test_an_edit_sends_the_refresh_update_across_the_seam_as_non_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = init_committed_repo(Path(directory) / "repo")
            binary = Path(directory) / "engine"
            write_fake_native_engine(binary)
            environment = self._environment(directory, binary)
            transports: list[RecordingTransport] = []

            def transport_for(path: Path) -> RecordingTransport:
                transports.append(RecordingTransport(path))
                return transports[-1]

            run_build(repository, environment=environment, transport_for=transport_for)
            arguments = QueryArguments("search-symbols", "main", (), [], [], [], [], 8, 4000, False)
            run_query(repository, arguments, environment=environment, transport_for=transport_for)
            write(repository / "tracked.txt", "edited\n")
            refreshed = run_query(
                repository, arguments, environment=environment, transport_for=transport_for
            )
            self.assertTrue(refreshed["refresh"]["performed"])
            frames = [frame for transport in transports for frame in transport.frames]
            self.assertEqual(
                frames,
                [
                    ("build", False),
                    ("search-symbols", True),
                    ("update", False),
                    ("search-symbols", True),
                ],
            )

    def test_only_the_relationship_query_crosses_the_seam_as_schema_two(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = init_committed_repo(Path(directory) / "repo")
            binary = Path(directory) / "engine"
            write_fake_native_engine(binary)
            environment = self._environment(directory, binary)
            transports: list[RecordingTransport] = []

            def transport_for(path: Path) -> RecordingTransport:
                transports.append(RecordingTransport(path))
                return transports[-1]

            run_build(repository, environment=environment, transport_for=transport_for)
            anchor = "sha256:" + "a" * 64
            run_query(
                repository,
                QueryArguments("search-symbols", "main", (), [], [], [], [], 8, 4000, False),
                environment=environment,
                transport_for=transport_for,
            )
            related = run_query(
                repository,
                QueryArguments(
                    "related-symbols", None, (anchor,), [], [], [], [], 8, 4000, False, "callers"
                ),
                environment=environment,
                transport_for=transport_for,
            )

            requests = [request for transport in transports for request in transport.requests]
            by_operation = {request["operation"]: request for request in requests}
            self.assertEqual(by_operation["search-symbols"]["schema_version"], "1")
            self.assertNotIn("direction", by_operation["search-symbols"])
            self.assertEqual(by_operation["build"]["schema_version"], "1")
            self.assertNotIn("direction", by_operation["build"])
            self.assertEqual(by_operation["related-symbols"]["schema_version"], "2")
            self.assertEqual(by_operation["related-symbols"]["direction"], "callers")
            self.assertEqual(by_operation["related-symbols"]["result_identities"], [anchor])
            self.assertIsNone(by_operation["related-symbols"]["query"])
            self.assertEqual(
                related["findings"][0]["relation"], "call"
            )
            self.assertEqual(related["findings"][0]["edge_evidence"], "verified")

    def test_transport_failures_keep_the_cli_messages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = init_committed_repo(Path(directory) / "repo")
            binary = Path(directory) / "engine"
            write_fake_native_engine(binary)
            environment = self._environment(directory, binary)

            class Failing:
                def __init__(self, reason: str) -> None:
                    self.reason = reason

                def exchange(self, wire: bytes, *, idempotent: bool) -> bytes:
                    raise NativeTransportError(self.reason)

            for reason, message in (
                ("timeout", "native engine invocation failed"),
                ("invocation-failed", "native engine invocation failed"),
                ("rejected", "native engine rejected the request"),
                ("restarted", "native engine rejected the request"),
            ):
                with self.assertRaises(PrepareCLIError) as caught:
                    run_build(
                        repository,
                        environment=environment,
                        transport_for=lambda _path, reason=reason: Failing(reason),
                    )
                self.assertEqual(str(caught.exception), message)

    def test_build_without_engine_or_installer_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = init_committed_repo(Path(directory) / "repo")
            environment = {
                "HOME": directory,
                "PATH": "",
                "TAF_STATE_HOME": str(Path(directory) / "state"),
            }
            with self.assertRaises(PrepareCLIError) as caught:
                run_build(repository, environment=environment, transport_for=OneShotTransport)
            self.assertEqual(str(caught.exception), "native engine is unavailable")


class ValidateQueryRequestTests(unittest.TestCase):
    def test_rules_match_the_cli(self) -> None:
        sha = "sha256:" + "a" * 64
        self.assertEqual(validate_query_request("search-symbols", " main ", ()), ("main", ()))
        self.assertEqual(validate_query_request("source-snippets", None, (sha, sha)), (None, (sha,)))
        for operation, query, ids, message in (
            ("search-docs", "  ", (), "selected query operation requires --query"),
            ("repository-map", "x", (), "selected query operation does not accept --query"),
            ("source-snippets", None, (), "source-snippets requires at least one --result-id"),
            ("search-symbols", "x", (sha,), "selected query operation does not accept --result-id"),
            ("source-snippets", None, ("bad",), "query result identity is invalid"),
            ("related-symbols", "x", (sha,), "selected query operation does not accept --query"),
            ("related-symbols", None, (), "related-symbols requires at least one --result-id"),
        ):
            with self.assertRaises(PrepareCLIError) as caught:
                validate_query_request(operation, query, ids)
            self.assertEqual(str(caught.exception), message)

    def test_direction_rules_match_the_cli(self) -> None:
        sha = "sha256:" + "a" * 64
        self.assertEqual(
            validate_query_request("related-symbols", None, (sha,), "callers"), (None, (sha,))
        )
        self.assertEqual(validate_query_request("search-symbols", "main", ()), ("main", ()))
        anchors = tuple(sorted("sha256:" + f"{index:064x}" for index in range(17)))
        for operation, ids, direction, message in (
            ("related-symbols", (sha,), None, "related-symbols requires --direction"),
            ("related-symbols", (sha,), "sideways", "selected query direction is invalid"),
            ("search-symbols", (), "callers", "selected query operation does not accept --direction"),
            ("source-snippets", (sha,), "callers", "selected query operation does not accept --direction"),
            ("related-symbols", anchors, "callers", "related-symbols accepts at most 16 --result-id values"),
        ):
            with self.subTest(operation=operation, direction=direction):
                query = "main" if operation == "search-symbols" else None
                with self.assertRaises(PrepareCLIError) as caught:
                    validate_query_request(operation, query, ids, direction)
                self.assertEqual(str(caught.exception), message)


if __name__ == "__main__":
    unittest.main()
