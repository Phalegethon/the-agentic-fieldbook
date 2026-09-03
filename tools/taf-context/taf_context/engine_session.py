"""A session-scoped native engine process (`taf-level1 --serve`)."""

from __future__ import annotations

import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
from typing import Callable

from .native_transport import NativeTransportError

READY_MARKER = b"__TAF_LEVEL1_SERVER_READY_V1__"
MAXIMUM_RESPONSE_BYTES = 262144  # the engine's MaximumStdoutBytes
MAXIMUM_STDERR_BYTES = 65536  # the engine's MaximumStderrBytes
_RETRYABLE = frozenset({"rejected", "timeout"})


class Level1Session:
    """One `--serve` child, started on first use, closed on idleness, EOF, or failure."""

    def __init__(
        self,
        binary: Path,
        *,
        request_timeout_seconds: float = 120.0,
        ready_timeout_seconds: float = 5.0,
        idle_timeout_seconds: float = 600.0,
        on_start: Callable[[int], None] | None = None,
    ) -> None:
        self._binary = binary
        self._request_timeout = request_timeout_seconds
        self._ready_timeout = ready_timeout_seconds
        self._idle_timeout = idle_timeout_seconds
        self._on_start = on_start
        # Reentrant: the idle timer takes the lock and then calls ``close``,
        # which takes it again.
        self._lock = threading.RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._responses: queue.Queue[tuple[str, bytes]] = queue.Queue()
        self._ready = threading.Event()
        self._stderr = bytearray()
        self._stderr_lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._idle_timer: threading.Timer | None = None
        self._idle_generation = 0

    @property
    def child_pid(self) -> int | None:
        process = self._process
        return None if process is None or process.poll() is not None else process.pid

    @property
    def stderr_tail(self) -> str:
        with self._stderr_lock:
            return bytes(self._stderr).decode("utf-8", "replace")

    def exchange(self, wire: bytes) -> bytes:
        with self._lock:
            self._cancel_idle_timer()
            try:
                process = self._ensure_started()
                try:
                    process.stdin.write(wire)
                    process.stdin.flush()
                except (BrokenPipeError, OSError):
                    detail = self._last_stderr_line()
                    self._kill()
                    raise NativeTransportError("rejected", detail)
                try:
                    kind, payload = self._responses.get(timeout=self._request_timeout)
                except queue.Empty:
                    self._kill()
                    raise NativeTransportError("timeout")
                if kind == "line":
                    return payload
                if kind == "oversized":
                    self._kill()
                    raise NativeTransportError("rejected", "oversized response")
                # The child closed stdout without answering: read its reason
                # before the streams are torn down.
                detail = self._last_stderr_line()
                self._kill()
                raise NativeTransportError("rejected", detail)
            finally:
                if self._process is not None:
                    self._arm_idle_timer()

    def close(self) -> None:
        with self._lock:
            self._cancel_idle_timer()
            process = self._process
            if process is None:
                return
            try:
                if process.stdin is not None:
                    process.stdin.close()
                process.wait(timeout=2.0)
            except (OSError, subprocess.TimeoutExpired):
                process.terminate()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    self._kill_process(process)
            self._forget()

    # -- internals -----------------------------------------------------------

    def _ensure_started(self) -> subprocess.Popen[bytes]:
        if self._process is not None and self._process.poll() is None:
            return self._process
        self._forget()
        self._ready = threading.Event()
        self._responses = queue.Queue()
        with self._stderr_lock:
            self._stderr = bytearray()
        try:
            process = subprocess.Popen(
                [str(self._binary), "--serve"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=os.name == "posix",
            )
        except OSError as exc:
            raise NativeTransportError("invocation-failed") from exc
        self._process = process
        self._threads = [
            threading.Thread(
                target=self._drain_stdout,
                args=(process, self._responses),
                daemon=True,
            ),
            threading.Thread(
                target=self._drain_stderr,
                args=(process, self._ready),
                daemon=True,
            ),
        ]
        for thread in self._threads:
            thread.start()
        if not self._ready.wait(self._ready_timeout) or process.poll() is not None:
            self._kill()
            raise NativeTransportError(
                "invocation-failed", "engine did not signal readiness"
            )
        if self._on_start is not None:
            self._on_start(process.pid)
        return process

    def _drain_stdout(
        self, process: subprocess.Popen[bytes], responses: queue.Queue
    ) -> None:
        stream = process.stdout
        try:
            while True:
                line = stream.readline(MAXIMUM_RESPONSE_BYTES + 1)
                if not line:
                    responses.put(("eof", b""))
                    return
                if not line.endswith(b"\n"):
                    kind = "oversized" if len(line) > MAXIMUM_RESPONSE_BYTES else "eof"
                    responses.put((kind, b""))
                    return
                responses.put(("line", line[:-1]))
        except (OSError, ValueError):
            # The stream was torn down under us while the child was being killed.
            responses.put(("eof", b""))

    def _drain_stderr(
        self, process: subprocess.Popen[bytes], ready: threading.Event
    ) -> None:
        stream = process.stderr
        try:
            for line in iter(stream.readline, b""):
                if line.rstrip(b"\r\n") == READY_MARKER:
                    ready.set()
                    continue
                with self._stderr_lock:
                    self._stderr.extend(line)
                    if len(self._stderr) > MAXIMUM_STDERR_BYTES:
                        del self._stderr[: len(self._stderr) - MAXIMUM_STDERR_BYTES]
        except (OSError, ValueError):
            return

    def _last_stderr_line(self) -> str:
        for thread in self._threads:
            thread.join(timeout=0.5)
        lines = [line for line in self.stderr_tail.splitlines() if line.strip()]
        return lines[-1][:200] if lines else ""

    def _kill(self) -> None:
        process = self._process
        if process is not None:
            self._kill_process(process)
        self._forget()

    @staticmethod
    def _kill_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (ProcessLookupError, PermissionError, OSError):
            process.kill()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass

    def _forget(self) -> None:
        for thread in self._threads:
            thread.join(timeout=1.0)
        self._threads = []
        process = self._process
        if process is not None:
            for stream in (process.stdin, process.stdout, process.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass
        self._process = None

    def _arm_idle_timer(self) -> None:
        self._cancel_idle_timer()
        self._idle_generation += 1
        generation = self._idle_generation
        timer = threading.Timer(
            self._idle_timeout, self._close_when_idle, args=(generation,)
        )
        timer.daemon = True
        self._idle_timer = timer
        timer.start()

    def _close_when_idle(self, generation: int) -> None:
        with self._lock:
            # A timer that fired while an exchange held the lock has been
            # superseded by the one that exchange armed; only the timer of the
            # current generation may close the child.
            if self._idle_timer is None or generation != self._idle_generation:
                return
            self.close()

    def _cancel_idle_timer(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None


class SessionTransport:
    """`NativeTransport` over a session: reads retry once on a fresh child, writes never."""

    def __init__(self, session: Level1Session) -> None:
        self._session = session

    def exchange(self, wire: bytes, *, idempotent: bool) -> bytes:
        try:
            return self._session.exchange(wire)
        except NativeTransportError as exc:
            if not idempotent or exc.reason not in _RETRYABLE:
                raise
        try:
            return self._session.exchange(wire)
        except NativeTransportError as retry_exc:
            raise NativeTransportError("restarted", retry_exc.detail) from retry_exc
