"""One-line warnings about dependents a commit does not carry.

A pre-commit launcher runs `prepare hook run --repo <repo>`; the hook asks the
already-built index which symbols the staged change set has dependents on,
keeps the dependents whose file is not part of the commit, and writes at most
five one-line warnings to stderr. It is advisory only: it never builds,
activates, downloads, removes, or garbage-collects, it never writes to stdout,
it always exits 0, and it gives itself a hard wall-clock budget so a slow or
cold engine can never hold up a commit.
"""

from __future__ import annotations

from pathlib import Path
import threading
import time
from typing import Collection, Mapping, TextIO

from .context_operations import QueryArguments, run_inspect, run_query
from .git_snapshot import SnapshotError, _git
from .native_transport import NativeTransport, NativeTransportError


HOOK_TIME_LIMIT_SECONDS = 3.0  # wall clock the whole run gets before it goes silent
HOOK_MAXIMUM_LINES = 5  # warning lines written before the summary line takes over
HOOK_MAXIMUM_RESULTS = 16  # candidates the one impact query asks the engine for
HOOK_OUTPUT_CHARACTERS = 12000  # output budget of that query
HOOK_DISABLE_VARIABLE = "TAF_HOOK"  # set to exactly "0" to disable the hook entirely
HOOK_LINE_PREFIX = "TAF: "  # every warning the hook writes starts here

# How long the watchdog waits for an abandoned worker to unwind after its
# engine child was killed. The worker only has to see EOF, close its streams
# and return, so this is a courtesy for tidy shutdown, not a second deadline.
_ABANDON_JOIN_SECONDS = 0.25


def run_hook(
    repository: Path,
    *,
    environment: Mapping[str, str],
    stderr: TextIO,
    verbose: bool = False,
) -> int:
    """Warn about untouched dependents of the staged change set; always return 0.

    Every step runs on one daemon worker thread, and the main thread waits for
    it for the remainder of `HOOK_TIME_LIMIT_SECONDS`. A worker that is still
    running at the deadline is abandoned: its engine child is killed, nothing
    is written, and the commit proceeds. A worker that answered after the
    deadline never reaches this stream either, because only a worker that
    finished in time has its lines written here.
    """
    started = time.monotonic()
    if environment.get(HOOK_DISABLE_VARIABLE) == "0":
        _explain(stderr, verbose, f"disabled by {HOOK_DISABLE_VARIABLE}=0")
        return 0

    run = _HookRun(repository, environment)
    worker = threading.Thread(target=run.collect, name="taf-impact-hook", daemon=True)
    worker.start()
    worker.join(max(0.0, HOOK_TIME_LIMIT_SECONDS - (time.monotonic() - started)))
    if worker.is_alive():
        run.abandon()
        worker.join(_ABANDON_JOIN_SECONDS)
        # A worker that was already inside the engine's start-up when the
        # first kill landed owns a child the first kill could not see.
        run.abandon()
        _explain(
            stderr, verbose, f"exceeded the {HOOK_TIME_LIMIT_SECONDS} second limit"
        )
        return 0
    if run.reason is not None:
        _explain(stderr, verbose, run.reason)
        return 0
    for line in run.lines:
        stderr.write(line + "\n")
    return 0


def untouched_dependents(
    result: Mapping[str, object], staged_paths: Collection[str]
) -> list[dict]:
    """The result's verified candidates whose file is not part of the commit.

    A dependent inside the commit is by definition already handled, and an
    inferred edge is a guess a hook must never make; what is left keeps the
    result's own candidate order.
    """
    touched = set(staged_paths)
    findings = result.get("findings") or []
    return [
        candidate
        for candidate in findings
        if candidate.get("path") not in touched
        and candidate.get("edge_evidence") == "verified"
    ]


def format_warning_line(candidate: Mapping[str, object]) -> str:
    """One warning: the changed symbol, the dependent, and what it means."""
    # The composition sorts a candidate's anchors strongest first and copies
    # that anchor's reference line onto the candidate itself, so the two
    # halves of the line always describe the same edge.
    anchor = candidate["anchors"][0]
    return (
        f"{HOOK_LINE_PREFIX}{anchor['qualified_name']} changed; "
        f"{candidate['path']}:{candidate['reference_line']} "
        "depends on it and is not in this commit"
    )


def format_summary_line(remaining: int) -> str:
    """The line that speaks for the dependents beyond the five-line cap."""
    return (
        f"{HOOK_LINE_PREFIX}… and {remaining} more "
        "(run: prepare query --operation impact-candidates --staged)"
    )


class _HookRun:
    """The readiness, staged, and query steps, on a thread a watchdog can abandon.

    The run owns exactly one engine session and hands every engine call the
    same one. `abandon` is the watchdog's entry point and runs on the main
    thread: it takes only this object's own short-lived lock, never the
    session's, so it cannot be made to wait by the request it is cancelling.
    """

    def __init__(self, repository: Path, environment: Mapping[str, str]) -> None:
        self._repository = repository
        self._environment = environment
        self._guard = threading.Lock()
        self._session = None
        self._abandoned = False
        self.lines: list[str] = []
        self.reason: str | None = None

    def collect(self) -> None:
        try:
            summary = run_inspect(
                self._repository,
                environment=self._environment,
                transport_for=self._transport_for,
            )
            reason = _readiness_refusal(summary)
            if reason is not None:
                self.reason = reason
                return
            staged = _staged_paths(self._repository)
            if staged is None:
                self.reason = "the staged change set could not be read"
                return
            result = run_query(
                self._repository,
                _hook_query(),
                environment=self._environment,
                transport_for=self._transport_for,
            )
            untouched = untouched_dependents(result, staged)
            if not untouched:
                self.reason = "no untouched dependents"
                return
            lines = [
                format_warning_line(candidate)
                for candidate in untouched[:HOOK_MAXIMUM_LINES]
            ]
            remaining = len(untouched) - HOOK_MAXIMUM_LINES
            if remaining > 0:
                lines.append(format_summary_line(remaining))
            self.lines = lines
        except Exception as exc:  # the hook is advisory: nothing may escape it
            self.reason = _exception_reason(exc)
        finally:
            self._close()

    def abandon(self) -> None:
        """Refuse any further engine call and kill the child, from any thread."""
        with self._guard:
            self._abandoned = True
            session = self._session
        if session is not None:
            session.interrupt()

    def exchange(self, wire: bytes) -> bytes:
        with self._guard:
            session = None if self._abandoned else self._session
        if session is None:
            raise NativeTransportError("timeout")
        return session.exchange(wire)

    def _transport_for(self, binary: Path) -> NativeTransport:
        """One session for the whole run, started on the first engine call.

        A change query makes one engine call per changed symbol, and a fresh
        process per call would reload the index every time - far more than the
        hook's budget allows.
        """
        from .engine_session import Level1Session  # hook and change queries only

        with self._guard:
            if self._session is None and not self._abandoned:
                self._session = Level1Session(
                    binary, ready_timeout_seconds=HOOK_TIME_LIMIT_SECONDS
                )
        return _HookTransport(self)

    def _close(self) -> None:
        with self._guard:
            session = self._session
            self._session = None
        if session is not None:
            session.close()


class _HookTransport:
    """`NativeTransport` over the run's one session, which never retries.

    `SessionTransport` retries an idempotent request once on a fresh child;
    after the watchdog has killed this run's child that retry would start a
    replacement engine process the hook is no longer there to reap.
    """

    def __init__(self, run: _HookRun) -> None:
        self._run = run

    def exchange(self, wire: bytes, *, idempotent: bool) -> bytes:
        del idempotent  # see the class docstring: a hook request is never retried
        return self._run.exchange(wire)


def _hook_query() -> QueryArguments:
    """The one query the hook makes: the staged change set's dependents."""
    return QueryArguments(
        operation="impact-candidates",
        query=None,
        result_identities=(),
        path_prefixes=[],
        languages=[],
        symbol_kinds=[],
        source_types=[],
        maximum_results=HOOK_MAXIMUM_RESULTS,
        maximum_output_characters=HOOK_OUTPUT_CHARACTERS,
        allow_inferred=False,
        direction=None,
        base=None,
        staged=True,
    )


def _readiness_refusal(summary: Mapping[str, object]) -> str | None:
    """Why the hook must stay silent, or None when the index is usable.

    Only `use-index` lets the hook ask anything: it never installs the engine,
    never builds and never rebuilds. The incremental refresh a query performs
    on a bound index is the one exception, and it is what every query does.
    """
    engine = summary.get("engine") or {}
    if engine.get("availability") != "available":
        return "the native engine is not installed"
    action = summary.get("next_safe_action")
    if action != "use-index":
        return f"context is not ready (next safe action: {action})"
    return None


def _staged_paths(repository: Path) -> set[str] | None:
    """The paths of the change set `git commit` would record, or None.

    Read from Git rather than from the query result: the result's `changed`
    list is what the output budget left of the change set, and a trimmed entry
    would turn a dependent that is in the commit into a warning.
    """
    try:
        raw = _git(
            repository,
            "diff",
            "--cached",
            "--name-only",
            "-z",
            "--no-renames",
            "--ignore-submodules=all",
            "HEAD",
            "--",
            allow_failure=True,
        )
    except SnapshotError:
        return None
    if raw is None:
        return None
    return {
        field.decode("utf-8", "surrogateescape")
        for field in raw.split(b"\x00")
        if field
    }


def _exception_reason(exc: Exception) -> str:
    """One line naming a failure's class and message, for `--verbose` only."""
    message = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _explain(stderr: TextIO, verbose: bool, reason: str) -> None:
    """Say why the hook is silent - and only when the caller asked."""
    if verbose:
        stderr.write(f"TAF hook: {reason}\n")

