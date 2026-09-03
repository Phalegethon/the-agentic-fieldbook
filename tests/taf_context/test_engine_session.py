"""Tests for the session-scoped native engine process."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import textwrap
import time
import unittest

from taf_context.engine_session import Level1Session, SessionTransport
from taf_context.native_transport import NativeTransportError


def write_fake_serve_engine(path: Path, mode: str, marker: Path) -> Path:
    source = textwrap.dedent(
        """\
        #!/usr/bin/env python3
        import json, os, sys, time
        from pathlib import Path
        mode = __MODE__
        marker = Path(__MARKER__)
        if sys.argv[1:] != ["--serve"]:
            sys.stderr.write("invalid-native-level1-request\\n")
            sys.exit(2)
        if mode == "no-ready":
            time.sleep(30)
            sys.exit(0)
        sys.stderr.write("__TAF_LEVEL1_SERVER_READY_V1__\\n")
        sys.stderr.flush()
        count = 0
        for line in sys.stdin.buffer:
            count += 1
            request = json.loads(line)["request"]
            if mode == "crash-after-ready":
                sys.stderr.write("native-level1-internal-error\\n")
                sys.stderr.flush()
                sys.exit(3)
            if mode == "exit-on-second" and count == 2:
                sys.stderr.write("invalid-native-level1-request\\n")
                sys.stderr.flush()
                sys.exit(2)
            if mode == "slow-once" and not marker.exists():
                marker.touch()
                time.sleep(5)
            if mode == "oversized":
                sys.stdout.write("x" * 262145 + "\\n")
                sys.stdout.flush()
                continue
            if mode == "chatty":
                sys.stderr.write("noise " * 40000 + "\\n")
                sys.stderr.flush()
            sys.stdout.write(json.dumps({"request_identity": request["request_identity"], "pid": os.getpid(), "sequence": count}) + "\\n")
            sys.stdout.flush()
        """
    ).replace("__MODE__", repr(mode)).replace("__MARKER__", repr(str(marker)))
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def frame(identity: str) -> bytes:
    return json.dumps({"request": {"request_identity": identity}}).encode("utf-8") + b"\n"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_until(predicate, seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class Level1SessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.addCleanup(self._directory.cleanup)

    def _session(self, mode: str, **options) -> Level1Session:
        binary = write_fake_serve_engine(self.root / f"engine-{mode}", mode, self.root / "marker")
        session = Level1Session(binary, **options)
        self.addCleanup(session.close)
        return session

    def test_child_starts_lazily_and_is_reused(self) -> None:
        session = self._session("echo")
        self.assertIsNone(session.child_pid)
        first = json.loads(session.exchange(frame("r1")))
        second = json.loads(session.exchange(frame("r2")))
        self.assertEqual((first["request_identity"], first["sequence"]), ("r1", 1))
        self.assertEqual((second["request_identity"], second["sequence"], second["pid"]), ("r2", 2, first["pid"]))
        self.assertEqual(session.child_pid, first["pid"])

    def test_missing_ready_marker_kills_the_child(self) -> None:
        session = self._session("no-ready", ready_timeout_seconds=0.3)
        with self.assertRaises(NativeTransportError) as caught:
            session.exchange(frame("r1"))
        self.assertEqual(caught.exception.reason, "invocation-failed")
        self.assertIsNone(session.child_pid)

    def test_request_deadline_kills_and_the_next_exchange_restarts(self) -> None:
        session = self._session("slow-once", request_timeout_seconds=0.3)
        with self.assertRaises(NativeTransportError) as caught:
            session.exchange(frame("r1"))
        self.assertEqual(caught.exception.reason, "timeout")
        self.assertIsNone(session.child_pid)
        again = json.loads(session.exchange(frame("r2")))
        self.assertEqual(again["sequence"], 1)

    def test_child_exit_before_answering_is_rejected_with_the_stderr_reason(self) -> None:
        session = self._session("crash-after-ready")
        with self.assertRaises(NativeTransportError) as caught:
            session.exchange(frame("r1"))
        self.assertEqual(caught.exception.reason, "rejected")
        self.assertIn("native-level1-internal-error", caught.exception.detail)
        self.assertIsNone(session.child_pid)

    def test_oversized_response_is_rejected(self) -> None:
        session = self._session("oversized")
        with self.assertRaises(NativeTransportError) as caught:
            session.exchange(frame("r1"))
        self.assertEqual(caught.exception.reason, "rejected")
        self.assertIsNone(session.child_pid)

    def test_idle_timeout_closes_the_child_and_a_later_exchange_restarts(self) -> None:
        session = self._session("echo", idle_timeout_seconds=0.2)
        first = json.loads(session.exchange(frame("r1")))
        self.assertTrue(_wait_until(lambda: session.child_pid is None))
        self.assertFalse(_alive(first["pid"]))
        second = json.loads(session.exchange(frame("r2")))
        self.assertNotEqual(second["pid"], first["pid"])

    def test_close_is_idempotent_and_leaves_no_process(self) -> None:
        session = self._session("echo")
        pid = json.loads(session.exchange(frame("r1")))["pid"]
        session.close()
        session.close()
        self.assertIsNone(session.child_pid)
        self.assertTrue(_wait_until(lambda: not _alive(pid)))

    def test_stderr_buffer_stays_bounded(self) -> None:
        session = self._session("chatty")
        session.exchange(frame("r1"))
        session.exchange(frame("r2"))
        self.assertLessEqual(len(session.stderr_tail.encode("utf-8")), 65536)

    def test_start_hook_receives_the_pid_once_per_child(self) -> None:
        pids: list[int] = []
        session = self._session("echo", on_start=pids.append)
        session.exchange(frame("r1"))
        session.exchange(frame("r2"))
        self.assertEqual(pids, [session.child_pid])


class SessionTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.addCleanup(self._directory.cleanup)

    def _transport(self, mode: str) -> SessionTransport:
        binary = write_fake_serve_engine(self.root / f"engine-{mode}", mode, self.root / "marker")
        session = Level1Session(binary)
        self.addCleanup(session.close)
        return SessionTransport(session)

    def test_idempotent_request_is_retried_once_on_a_fresh_child(self) -> None:
        transport = self._transport("exit-on-second")
        first = json.loads(transport.exchange(frame("r1"), idempotent=True))
        second = json.loads(transport.exchange(frame("r2"), idempotent=True))
        self.assertEqual((second["request_identity"], second["sequence"]), ("r2", 1))
        self.assertNotEqual(second["pid"], first["pid"])

    def test_non_idempotent_request_is_never_retried(self) -> None:
        transport = self._transport("exit-on-second")
        transport.exchange(frame("r1"), idempotent=False)
        with self.assertRaises(NativeTransportError) as caught:
            transport.exchange(frame("r2"), idempotent=False)
        self.assertEqual(caught.exception.reason, "rejected")

    def test_second_failure_is_reported_as_restarted(self) -> None:
        transport = self._transport("crash-after-ready")
        with self.assertRaises(NativeTransportError) as caught:
            transport.exchange(frame("r1"), idempotent=True)
        self.assertEqual(caught.exception.reason, "restarted")
        self.assertIn("native-level1-internal-error", caught.exception.detail)


if __name__ == "__main__":
    unittest.main()
