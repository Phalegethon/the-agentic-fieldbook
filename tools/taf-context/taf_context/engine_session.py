"""A session-scoped native engine process (`taf-level1 --serve`)."""

from __future__ import annotations

import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
from typing import Callable

from .native_transport import DEFAULT_REQUEST_TIMEOUT_SECONDS, NativeTransportError

READY_MARKER = b"__TAF_LEVEL1_SERVER_READY_V1__"
MAXIMUM_RESPONSE_BYTES = 262144  # the engine's MaximumStdoutBytes
MAXIMUM_STDERR_BYTES = 65536  # the engine's MaximumStderrBytes
_RETRYABLE = frozenset({"rejected", "timeout"})


class StderrRing:
    """One child generation's bounded stderr tail, newest bytes last.

    The reader thread is handed its generation's ring as an argument, so a
    reader that outlived its child cannot write into the next child's tail.
    ``appended`` counts every byte ever written, which lets a caller mark the
    start of a request and read back only what was written after it even
    though the ring itself keeps discarding its oldest bytes.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data = bytearray()
        self._appended = 0

    def append(self, chunk: bytes) -> None:
        with self._lock:
            self._data.extend(chunk)
            self._appended += len(chunk)
            if len(self._data) > MAXIMUM_STDERR_BYTES:
                del self._data[: len(self._data) - MAXIMUM_STDERR_BYTES]

    @property
    def appended(self) -> int:
        with self._lock:
            return self._appended

    def tail(self, since: int = 0) -> str:
        with self._lock:
            # The retained bytes span [appended - len(data), appended); a mark
            # older than that has already been discarded, so keep everything.
            skip = min(max(0, since - (self._appended - len(self._data))), len(self._data))
            return bytes(self._data[skip:]).decode("utf-8", "replace")


class Level1Session:
    """One `--serve` child, started on first use, closed on idleness, EOF, or failure."""

    def __init__(
        self,
        binary: Path,
        *,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
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
        self._stderr = StderrRing()
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._idle_timer: threading.Timer | None = None
        self._idle_generation = 0

    @property
    def child_pid(self) -> int | None:
        process = self._process
        return None if process is None or process.poll() is not None else process.pid

    @property
    def stderr_tail(self) -> str:
        return self._stderr.tail()

    def exchange(self, wire: bytes) -> bytes:
        with self._lock:
            self._cancel_idle_timer()
            try:
                process = self._ensure_started()
                # Only stderr written from here on belongs to this request; an
                # earlier request's diagnostic must not become this detail.
                since = self._stderr.appended
                try:
                    process.stdin.write(wire)
                    process.stdin.flush()
                except (BrokenPipeError, OSError):
                    detail = self._last_stderr_line(since)
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
                detail = self._last_stderr_line(since)
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

    def interrupt(self) -> None:
        """Kill the current child from another thread, without taking the lock.

        ``close`` cannot serve a watchdog: ``exchange`` holds the session lock
        for the whole response wait, so closing from a second thread would
        block until the request deadline - the very deadline the watchdog
        exists to pre-empt. Killing the child's process group instead leaves
        the blocked ``exchange`` to see EOF and raise, and it reaps the child
        here so nothing outlives the caller. The session's own bookkeeping is
        left to that ``exchange``, which owns the lock.
        """
        process = self._process
        if process is not None:
            self._kill_process(process)

    # -- internals -----------------------------------------------------------

    def _ensure_started(self) -> subprocess.Popen[bytes]:
        if self._process is not None and self._process.poll() is None:
            return self._process
        self._forget()
        self._ready = threading.Event()
        self._responses = queue.Queue()
        self._stderr = StderrRing()
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
        self._stdout_thread = threading.Thread(
            target=self._drain_stdout,
            args=(process, self._responses),
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            args=(process, self._ready, self._stderr),
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()
        if not self._ready.wait(self._ready_timeout) or process.poll() is not None:
            self._kill()
            raise NativeTransportError(
                "invocation-failed", "engine did not signal readiness"
            )
        if self._on_start is not None:
            try:
                self._on_start(process.pid)
            except Exception:
                # The hook is a diagnostic and the session owns no stream to
                # report its failure on; a raising hook must neither fail the
                # exchange nor leave a started child unaccounted for.
                pass
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
        self,
        process: subprocess.Popen[bytes],
        ready: threading.Event,
        ring: StderrRing,
    ) -> None:
        stream = process.stderr
        try:
            while True:
                # Bounded like the stdout drain: one newline-free line is never
                # buffered beyond what the ring is allowed to retain.
                line = stream.readline(MAXIMUM_STDERR_BYTES + 1)
                if not line:
                    return
                if line.rstrip(b"\r\n") == READY_MARKER:
                    ready.set()
                    continue
                ring.append(line)
        except (OSError, ValueError):
            return

    def _last_stderr_line(self, since: int = 0) -> str:
        process = self._process
        if process is not None:
            # Wait for the child rather than guess at a join timeout: the
            # stderr reader is guaranteed to reach EOF once the child's file
            # descriptors are gone, so the join below needs no bound.
            self._kill_process(process)
            try:
                process.wait()
            except OSError:
                pass
        reader = self._stderr_thread
        if reader is not None:
            reader.join()
        lines = [line for line in self._stderr.tail(since).splitlines() if line.strip()]
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
        readers = (self._stdout_thread, self._stderr_thread)
        for thread in readers:
            if thread is not None:
                thread.join(timeout=1.0)
        process = self._process
        if process is not None:
            for stream, reader in (
                (process.stdin, None),
                (process.stdout, self._stdout_thread),
                (process.stderr, self._stderr_thread),
            ):
                if stream is None:
                    continue
                if reader is not None and reader.is_alive():
                    # ``close()`` waits for the buffer lock a reader blocked in
                    # ``readline`` holds, i.e. forever. The daemon reader dies
                    # with the process; leave the descriptor to ``Popen``.
                    continue
                try:
                    stream.close()
                except OSError:
                    pass
        self._stdout_thread = None
        self._stderr_thread = None
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
