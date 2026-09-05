"""One-line warnings about dependents a commit does not carry.

A pre-commit launcher runs `prepare hook run --repo <repo>`; the hook asks the
already-built index which symbols the staged change set has dependents on,
keeps the dependents whose file is not part of the commit, and writes at most
five one-line warnings to stderr. It is advisory only: it never builds,
activates, downloads, removes, or garbage-collects, it never writes to stdout,
it always exits 0, and it gives itself a hard wall-clock budget so a slow or
cold engine can never hold up a commit.

`install_hook`, `remove_hook`, and `hook_status` manage the launcher itself:
the only write this module ever makes to a repository, and only under
`--confirm-hook-write`, inside `.git/hooks`.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import time
from typing import Collection, Mapping, TextIO

from .context_operations import (
    PrepareCLIError,
    QueryArguments,
    TransportFactory,
    _is_test_path,
    _state_paths,
    run_inspect,
    run_query,
)
from .git_snapshot import SnapshotError, _git
from .native_transport import NativeTransport, NativeTransportError


HOOK_TIME_LIMIT_SECONDS = 3.0  # wall clock the whole run gets before it goes silent
HOOK_MAXIMUM_LINES = 5  # warning lines written before the summary line takes over
# The engine bounds each relationship call at 64 results per changed symbol
# and direction, and follows at most 64 changed symbols
# (`IMPACT_CHANGED_MAXIMUM_RESULTS` in context_operations.py), so the merged
# candidate list is finite regardless of what this constant names. The
# composition's own slice (`compose_impact_candidates`'s `maximum_results`
# argument) must never bind for the hook, which prints five lines but must
# choose them from, and count, the whole set - not from whatever a
# caller-supplied result cap left standing.
HOOK_MAXIMUM_RESULTS = 1_000_000
# The shared composition path (`_run_change_query`, via `trim_to_budget`)
# always takes an output-character budget, but the hook never serializes its
# answer anywhere - stdout, a file, a wire reply - so no budget it could name
# would ever be observed. This value exists only so that budget can never
# bind: it is not exposed on the CLI or the MCP server (both keep their
# 2000-12000 choices), and raising it is not "a bigger budget" so much as
# turning the budget off for the one caller that never needed one.
HOOK_OUTPUT_CHARACTERS = 10_000_000
HOOK_DISABLE_VARIABLE = "TAF_HOOK"  # set to exactly "0" to disable the hook entirely
HOOK_HEADER_PREFIX = "TAF impact: "  # every header the hook writes starts here (D16)
HOOK_DETAIL_INDENT = "  "  # every detail and trailer line starts with two spaces (D16)
# A redirection to the surface that can show the full list (MCP
# `impact_candidates`, `staged: true`), not a command: `query impact-candidates
# --staged` is not runnable in a plugin installation (no `prepare` on PATH),
# and the CLI's own budget shows only part of a wide result anyway.
HOOK_POINTER = "(ask your agent to list TAF impact for this commit)"

HOOK_FILE_NAME = "pre-commit"
CHAINED_HOOK_NAME = "pre-commit.taf-chained"
# The second line of every launcher this module writes; a `pre-commit` file
# containing this exact line is a TAF launcher, anything else is foreign.
LAUNCHER_MARKER = "# TAF commit-time impact warning (managed by: prepare hook install)"

# Where the launcher's self-healing pointer lives, under TAF's user-local
# state root: `<state root>/hook/launcher-target`. The launcher reads it to
# find the broker that last ran on this machine instead of trusting the
# embedded paths a plugin update may have orphaned.
LAUNCHER_TARGET_DIRECTORY = "hook"
LAUNCHER_TARGET_FILE = "launcher-target"

_HOOK_GIT_TIMEOUT_SECONDS = 20  # wall clock for the small git calls hook management makes

# Environment variables that locate a repository and override `-C <path>`;
# hook management drops them so it can only ever act on the named repository.
_GIT_LOCATION_VARIABLES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
)

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

    The whole body is guarded: the hook is advisory, so thread start,
    `_explain`, and the warning writes below may never raise out to the
    caller and abort the commit. Each warning line is written under its own
    `contextlib.suppress` so a stderr failure on one line (a closed pipe, an
    encoding a summary character does not fit) does not discard lines already
    written.
    """
    try:
        started = time.monotonic()
        if environment.get(HOOK_DISABLE_VARIABLE) == "0":
            _explain(stderr, verbose, f"disabled by {HOOK_DISABLE_VARIABLE}=0")
            return 0

        run = _HookRun(repository, environment, colour=_hook_colour_enabled(stderr, environment))
        worker = threading.Thread(
            target=run.collect, name="taf-impact-hook", daemon=True
        )
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
            with contextlib.suppress(Exception):
                stderr.write(line + "\n")
        return 0
    except Exception:  # the hook is advisory: nothing may escape it, ever
        return 0


def _hook_colour_enabled(stderr: TextIO, environment: Mapping[str, str]) -> bool:
    """Bold header only on a real TTY stderr, honouring `NO_COLOR` and `TERM=dumb` (D16).

    `isatty` is read through `getattr` rather than called directly: a GUI
    client's or CI's stderr double is not guaranteed to define it at all,
    and a missing method must read as "not a TTY", never raise out of the
    hook. `NO_COLOR` disables colour whenever it is set in the commit's
    environment at all (its value is never inspected); `TERM=dumb` disables
    it only for that exact value, the same convention `git` itself follows.
    """
    isatty = getattr(stderr, "isatty", None)
    if not callable(isatty) or not isatty():
        return False
    if "NO_COLOR" in environment:
        return False
    if environment.get("TERM") == "dumb":
        return False
    return True


def untouched_dependents(
    result: Mapping[str, object], staged_paths: Collection[str]
) -> list[dict]:
    """One representative candidate per untouched file the change set depends on.

    A dependent inside the commit is by definition already handled, and an
    inferred edge is a guess a hook must never make, so those candidates are
    dropped first, along with an anchorless one (so `format_report`'s
    `anchors[0]` can never index an empty list) and one carrying no usable
    `path` (a warning line names a path and a line, so there would be nothing
    to print). What is left is grouped by `path`, keeping each path's
    first-seen position: a warning names the *file*, not every candidate
    inside it, so a file that turns up as both an import and a call site gets
    one line, not two. Within a path the call
    representative wins over an import - a call is the stronger evidence that
    the file actually depends on the changed symbol at run time - falling
    back to the first candidate of that path (the composition's own order:
    evidence, anchor count, path, start line) when no call is present.
    Finally, the representatives are stable-partitioned so every non-test
    path prints before every test path, each group keeping its own order;
    this is the same `_is_test_path` rule the composition already applies to
    its `changed` list, not a second copy of it. A test that references the
    changed symbol is still a legitimate warning - it only yields to the
    production dependents once the five-line cap bites.
    """
    touched = set(staged_paths)
    findings = result.get("findings") or []
    filtered = [
        candidate
        for candidate in findings
        # A warning line reads `<path>:<line>`, so a candidate the engine
        # somehow left without a usable path has nothing to name. Defensive:
        # the engine always sets one.
        if isinstance(candidate.get("path"), str)
        and candidate.get("path")
        and candidate.get("path") not in touched
        and candidate.get("edge_evidence") == "verified"
        and candidate.get("anchors")
    ]
    by_path: dict[str, list[dict]] = {}
    for candidate in filtered:
        by_path.setdefault(candidate["path"], []).append(candidate)
    representatives = [
        next((item for item in group if item.get("relation") == "call"), group[0])
        for group in by_path.values()
    ]
    non_test = [item for item in representatives if not _is_test_path(item["path"])]
    test = [item for item in representatives if _is_test_path(item["path"])]
    return non_test + test


def format_report(untouched: list[dict], *, truncated: bool, colour: bool) -> list[str]:
    """The header, at most five detail lines, and one trailer (addendum D16).

    Returns an empty list when there is nothing to report. `untouched` empty
    and not truncated is the ordinary "nothing depends" case; `untouched`
    empty but `truncated` true is deliberately reported the same way - an
    omission the engine cannot even name a file for is not something a user
    can act on, so a header with nothing under it would only mislead.

    Detail lines come from whichever group is non-empty first: production
    files whenever there are any, test files only when there are none -
    `_is_test_path` applied here rather than trusted from the caller's
    ordering, so this function's own contract does not depend on
    `untouched_dependents`' stable partition. Once a production dependent
    exists, every test dependent is folded into the trailer's test-file
    count instead of taking one of the five slots (D16 item 4): a forgotten
    test file is less important than a forgotten production one.

    The trailer's `remaining` (R) counts untouched files of the *printed*
    group past the five shown; `other_test_count` (T) counts test files not
    printed at all, which is only ever nonzero when the printed group is
    production. When the printed group is already test (no production
    dependent at all), there is no separate test category left to name, so
    `T` is 0 and any test files past the cap are folded into `R` instead -
    an extension of D16's own R/T model to the test-only case, which the
    addendum's wording does not spell out on its own.
    """
    if not untouched:
        return []
    non_test = [candidate for candidate in untouched if not _is_test_path(candidate["path"])]
    test = [candidate for candidate in untouched if _is_test_path(candidate["path"])]
    if non_test:
        printed_group, is_test_header, other_test_count = non_test, False, len(test)
    else:
        printed_group, is_test_header, other_test_count = test, True, 0
    printed = printed_group[:HOOK_MAXIMUM_LINES]
    remaining = len(printed_group) - len(printed)
    header = _format_header(len(printed_group), is_test=is_test_header, truncated=truncated)
    if colour:
        header = f"\x1b[1m{header}\x1b[0m"
    lines = [header, *_format_detail_lines(printed)]
    trailer = _format_trailer(remaining, other_test_count, truncated=truncated)
    if trailer is not None:
        lines.append(trailer)
    return lines


def _format_header(count: int, *, is_test: bool, truncated: bool) -> str:
    """`TAF impact: N file(s) depend(s) on this change and are not in this commit`.

    `count` is the untouched file count of whichever group `format_report`
    is printing (production, or test when no production file depends);
    `truncated` marks it with a trailing `+` when the engine omitted
    candidates in some direction, so the count is only a lower bound.
    """
    display = f"{count}+" if truncated else str(count)
    if count == 1 and not truncated:
        noun = "test file" if is_test else "file"
        return (
            f"{HOOK_HEADER_PREFIX}{display} {noun} depends on this change "
            "and is not in this commit"
        )
    noun = "test files" if is_test else "files"
    return (
        f"{HOOK_HEADER_PREFIX}{display} {noun} depend on this change "
        "and are not in this commit"
    )


def _format_detail_lines(candidates: list[dict]) -> list[str]:
    """Two-space indent, `<path>:<line>` padded so every `<-` column aligns.

    The composition sorts a candidate's anchors strongest first and copies
    that anchor's reference line onto the candidate itself, so the two
    halves of a line always describe the same edge.
    """
    locations = [f"{candidate['path']}:{candidate['reference_line']}" for candidate in candidates]
    width = max((len(location) for location in locations), default=0)
    return [
        f"{HOOK_DETAIL_INDENT}{location.ljust(width)}  <- {candidate['anchors'][0]['qualified_name']}"
        for location, candidate in zip(locations, candidates)
    ]


def _format_trailer(remaining: int, other_test_count: int, *, truncated: bool) -> str | None:
    """The one optional line naming what the five detail lines left out (D16).

    `remaining` (R) and `other_test_count` (T) pick one of four shapes:
    both positive names both counts, only one positive names that one
    alone, and neither positive falls back to "possibly more" when the
    result is truncated - or to nothing at all when it is not (there is
    genuinely nothing left to report). The `+` marks whichever count is
    the uncertain one (D16 note, fix wave 1): `R` gets it whenever `R` is
    positive (unchanged from the addendum's literal wording - `T` stays
    plain in that combined shape); when `R` is 0 and only `T` remains, `T`
    is the sole count naming the omission, so it gets the `+` instead.
    """
    remaining_display = f"{remaining}+" if truncated and remaining > 0 else str(remaining)
    test_only_truncated = truncated and remaining == 0 and other_test_count > 0
    test_only_display = f"{other_test_count}+" if test_only_truncated else str(other_test_count)
    test_noun = "test file" if other_test_count == 1 else "test files"
    if remaining > 0 and other_test_count > 0:
        return (
            f"{HOOK_DETAIL_INDENT}... and {remaining_display} more, plus "
            f"{other_test_count} {test_noun} {HOOK_POINTER}"
        )
    if remaining > 0:
        return f"{HOOK_DETAIL_INDENT}... and {remaining_display} more {HOOK_POINTER}"
    if other_test_count > 0:
        return f"{HOOK_DETAIL_INDENT}... plus {test_only_display} {test_noun} {HOOK_POINTER}"
    if truncated:
        return f"{HOOK_DETAIL_INDENT}... and possibly more {HOOK_POINTER}"
    return None


class _HookRun:
    """The readiness, staged, and query steps, on a thread a watchdog can abandon.

    The run owns exactly one engine session and hands every engine call the
    same one. `abandon` is the watchdog's entry point and runs on the main
    thread: it takes only this object's own short-lived lock, never the
    session's, so it cannot be made to wait by the request it is cancelling.
    """

    def __init__(
        self, repository: Path, environment: Mapping[str, str], *, colour: bool
    ) -> None:
        self._repository = repository
        self._environment = environment
        self._colour = colour
        self._guard = threading.Lock()
        # `Level1Session` is imported lazily in `_transport_for` (hook and
        # change queries only), so the annotation is a string to avoid a
        # module-level import a type checker would otherwise need to resolve.
        self._session: "Level1Session | None" = None
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
            truncated = bool(result.get("truncated"))
            untouched = untouched_dependents(result, staged)
            if not untouched:
                self.reason = "no untouched dependents"
                return
            self.lines = format_report(untouched, truncated=truncated, colour=self._colour)
        except Exception as exc:  # the hook is advisory: nothing may escape it
            self.reason = _exception_reason(exc)
        finally:
            # A raise from the teardown would escape `collect` itself, reach
            # `threading.excepthook`, and print a Python traceback onto the
            # commit's stderr - the one thing the hook must never do.
            with contextlib.suppress(Exception):
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
    """The one query the hook makes: the staged change set's dependents.

    `maximum_results` and `maximum_output_characters` are `HOOK_MAXIMUM_RESULTS`
    (1,000,000, chosen so the composition's own slice can never bind) and
    `HOOK_OUTPUT_CHARACTERS` (10,000,000, chosen so the shared trim can never
    bind): the hook never serializes this answer, so it must compose the
    full candidate set and choose its five lines from all of it, not from
    whatever a result cap or an output-budget trim left standing.
    """
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


def install_hook(
    repository: Path,
    *,
    chain: bool,
    environment: Mapping[str, str],
    interpreter: Path | None = None,
) -> dict[str, object]:
    """Write the pre-commit launcher; idempotent, and the only write this module makes.

    `interpreter` exists so tests can pin it; every production caller leaves
    it as the default, which resolves `sys.executable` exactly the way the
    launcher's own embedded interpreter path must be resolved. `environment`
    resolves the state root embedded in the launcher (where the self-healing
    pointer file lives) and, once the launcher is written, is handed to
    `refresh_launcher_target` (best effort, with this same `interpreter`) so
    a fresh install starts with a pointer that already matches it.
    """
    _require_posix()
    state, hooks_dir, hooks_path_value = _resolve_hooks_directory(repository)
    if state == "redirected":
        raise PrepareCLIError(
            f"core.hooksPath redirects hooks to {hooks_path_value}; TAF installs only "
            "under the repository's own hooks directory"
        )
    script = _entry_point_script()
    if not script.is_file():
        raise PrepareCLIError(
            "the TAF plugin entry point prepare_repo_context.py could not be located"
        )
    resolved_interpreter = (
        interpreter if interpreter is not None else Path(sys.executable)
    ).resolve()
    hook_path = hooks_dir / HOOK_FILE_NAME
    chained_path = hooks_dir / CHAINED_HOOK_NAME
    kind = _classify_pre_commit(hook_path)
    chained_hook_path: Path | None = None
    moved_aside = False
    if kind == "foreign":
        if not chain:
            raise PrepareCLIError(
                "a foreign pre-commit hook exists; pass --chain to run it after TAF, "
                "or remove it first"
            )
        if not os.access(hook_path, os.X_OK):
            # Git skips a pre-commit file without the executable bit, so
            # chaining it would start running a hook the user's commits never
            # ran - a behaviour change they did not ask for (D10).
            raise PrepareCLIError(
                "the foreign pre-commit hook is not executable, so git was not "
                "running it; chmod +x it before chaining, or remove it"
            )
        # `lexists`, not `exists`: a dangling symlink is a backup that is in
        # the way just as much as a regular file, and overwriting it would
        # lose whatever the user put there.
        if os.path.lexists(chained_path):
            raise PrepareCLIError(
                f"{CHAINED_HOOK_NAME} already exists; remove it before chaining another hook"
            )
        os.replace(hook_path, chained_path)  # mode is preserved by the rename
        chained_hook_path = chained_path
        moved_aside = True
    else:
        # `kind` is "taf" (a re-install never drops a chained hook, regardless
        # of `--chain`) or "absent" (a `pre-commit.taf-chained` backup can be
        # left behind by hand or by a crash between the foreign->chained
        # rename and the launcher write below; either way, an existing
        # backup is adopted rather than orphaned).
        if os.path.lexists(chained_path):
            chained_hook_path = chained_path
    try:
        source = _render_launcher(
            interpreter=resolved_interpreter,
            script=script,
            chained_hook_path=chained_hook_path,
            state_root=_state_paths(environment).root,
        )
        _write_launcher_atomically(hooks_dir, hook_path, source)
    except BaseException:
        # The foreign hook was moved aside for a launcher that never landed:
        # git would now look at nothing at all. Put it back before the error
        # is reported, so a failed install leaves the repository as it was.
        if moved_aside:
            with contextlib.suppress(OSError):
                os.replace(chained_path, hook_path)
        raise
    refresh_launcher_target(environment, interpreter=interpreter)
    return {
        "schema_version": "1",
        "mode": "hook-install",
        "hook_path": str(hook_path),
        "written": True,
        "chained": chained_hook_path is not None,
        "chained_hook_path": (
            str(chained_hook_path) if chained_hook_path is not None else None
        ),
        "interpreter": str(resolved_interpreter),
        "script": str(script),
        "next_safe_action": "none",
    }


def remove_hook(repository: Path) -> dict[str, object]:
    """Remove the TAF launcher and restore a chained hook if one was moved aside."""
    _require_posix()
    state, hooks_dir, hooks_path_value = _resolve_hooks_directory(repository)
    if state == "redirected":
        raise PrepareCLIError(
            f"core.hooksPath redirects hooks to {hooks_path_value}; nothing to remove there"
        )
    hook_path = hooks_dir / HOOK_FILE_NAME
    chained_path = hooks_dir / CHAINED_HOOK_NAME
    kind = _classify_pre_commit(hook_path)
    if kind == "foreign":
        raise PrepareCLIError("pre-commit is not a TAF launcher; nothing removed")
    removed = False
    if kind == "taf":
        hook_path.unlink()
        removed = True
    # A `pre-commit.taf-chained` backup is restored whenever it exists, not
    # only when `pre-commit` itself was a TAF launcher: the backup can be
    # left behind (by hand, or by a crash) while `pre-commit` is absent, and
    # `remove` must not leave it stranded. `lexists` so a backup that is a
    # dangling symlink is restored too, rather than left behind for good.
    restored = False
    if os.path.lexists(chained_path):
        os.replace(chained_path, hook_path)
        restored = True
    return {
        "schema_version": "1",
        "mode": "hook-remove",
        "hook_path": str(hook_path),
        "removed": removed,
        "restored": restored,
        "next_safe_action": "none",
    }


def hook_status(
    repository: Path,
    *,
    environment: Mapping[str, str],
    transport_for: TransportFactory,
) -> dict[str, object]:
    """Report the launcher's state and the index's readiness; this never writes.

    The hook classification (installed/foreign/absent/redirected) is decided
    first and independently of the index: a broken or unborn index still gets
    an honest `hook` field, with the readiness failure folded into its own
    `readiness.error` instead of aborting the whole report.

    `hook: "absent"` together with `chained: true` is a legitimate state, not
    a contradiction: a `pre-commit.taf-chained` backup can be left on disk
    with no `pre-commit` beside it (deleted by hand, or by a crash between
    the foreign->chained rename and the launcher write), and it simply means
    a chained backup awaits the next `install` or `remove` to adopt or
    restore it.
    """
    state, hooks_dir, hooks_path_value = _resolve_hooks_directory(repository)
    chained = False
    launcher_current: bool | None = None
    hook_path_value: str | None = None
    if state == "redirected":
        hook_field = "redirected"
    else:
        hook_path = hooks_dir / HOOK_FILE_NAME
        chained_path = hooks_dir / CHAINED_HOOK_NAME
        # `lexists` so a dangling symlink backup is reported, not hidden.
        chained = os.path.lexists(chained_path)
        hook_path_value = str(hook_path)
        kind = _classify_pre_commit(hook_path)
        if kind == "taf":
            hook_field = "installed"
            try:
                state_root = _state_paths(environment).root
            except PrepareCLIError:
                # An unresolvable state root only means the launcher cannot be
                # compared; `launcher_current` already has an honest value for
                # that ("cannot be computed"). The readiness check below hits
                # the same failure and folds it into `readiness.error`.
                state_root = None
            if state_root is None:
                launcher_current = None
            else:
                expected = _render_launcher(
                    interpreter=Path(sys.executable).resolve(),
                    script=_entry_point_script(),
                    chained_hook_path=chained_path if chained else None,
                    state_root=state_root,
                )
                try:
                    actual = hook_path.read_text(encoding="utf-8", errors="surrogateescape")
                except OSError:
                    actual = None
                launcher_current = actual == expected
        else:
            hook_field = kind  # "foreign" or "absent"
    error: str | None = None
    next_action: str | None = None
    try:
        summary = run_inspect(repository, environment=environment, transport_for=transport_for)
        next_action = summary.get("next_safe_action")
    except PrepareCLIError as exc:
        error = str(exc)
    return {
        "schema_version": "1",
        "mode": "hook-status",
        "hook": hook_field,
        "hooks_path": hooks_path_value,
        "hook_path": hook_path_value,
        "chained": chained,
        "launcher_current": launcher_current,
        # `status` reports everywhere; only `install` and `remove` refuse off
        # POSIX, and this field is how a caller learns that before asking.
        "posix": os.name == "posix",
        "readiness": {"next_safe_action": next_action, "error": error},
    }


def _require_posix() -> None:
    """The launcher is POSIX `sh` and its write path needs `fchmod`.

    `hook run` and the queries are unaffected; only the two verbs that write
    a launcher refuse, and they refuse cleanly rather than raising the
    `AttributeError` an absent `os.fchmod` would.
    """
    if os.name != "posix":
        raise PrepareCLIError("the commit-time hook is available on macOS and Linux only")


def _entry_point_script() -> Path:
    """The plugin's stable entry point, located from this package's own path."""
    return (
        Path(__file__).resolve().parents[3]
        / "skills"
        / "prepare-repo-context"
        / "scripts"
        / "prepare_repo_context.py"
    )


def launcher_target_path(state_root: Path) -> Path:
    """Where the launcher's self-healing pointer lives under a state root."""
    return state_root / LAUNCHER_TARGET_DIRECTORY / LAUNCHER_TARGET_FILE


def refresh_launcher_target(
    environment: Mapping[str, str], *, interpreter: Path | None = None
) -> bool:
    """Point the launcher's pointer file at the broker that just ran; best effort.

    Every `prepare` command except `hook run`, `hook install` (which does
    this itself, with the interpreter it just wrote into the launcher), and
    the MCP server at startup call this once they have succeeded, so the
    launcher in `.git/hooks` can find the broker that actually last ran on
    this machine instead of trusting an embedded path a plugin update may
    have orphaned. The pointer is written only when the state root already
    exists: state-write consent was given at some point in the past, and an
    `inspect` before any `build` must still create nothing. The content is
    two lines, the resolved interpreter and the plugin's entry-point script,
    written atomically (temp file, `fchmod(0o600)`, `os.replace`) into a
    `hook` directory the state root gets, mode 0700; nothing is written when
    the file already holds exactly that content. Every exception - state
    paths unavailable, an unwritable state root, anything else - is
    swallowed into `False`: writing this pointer is a convenience, never a
    reason a command should fail. `hook run` never calls this, so a stale
    broker recorded once can never re-assert itself through a later commit.
    """
    try:
        state_root = _state_paths(environment).root
        if not state_root.is_dir():
            return False
        resolved_interpreter = (
            Path(sys.executable) if interpreter is None else interpreter
        ).resolve()
        script = _entry_point_script()
        content = f"{resolved_interpreter}\n{script}\n"
        target = launcher_target_path(state_root)
        try:
            existing = target.read_text(encoding="utf-8", errors="surrogateescape")
        except OSError:
            existing = None
        if existing == content:
            return False
        directory = target.parent
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
        _write_launcher_target_atomically(directory, target, content)
        return True
    except Exception:  # writing the pointer is a convenience, never a failure
        return False


def _run_repository_git(repository: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    """A plain `git -C <repo>` call over the caller's real environment.

    Unlike `git_snapshot._git`, this must see the caller's actual effective
    Git configuration - including a real `core.hooksPath` - so it copies
    `os.environ` rather than pinning the `GIT_CONFIG_*` overrides that would
    mask it. The variables that *locate* a repository are dropped from that
    copy: they take precedence over `-C <repo>`, so an install run under an
    exported `GIT_DIR` (from inside another hook, a `git rebase -i` shell, or
    a wrapper) would otherwise resolve the hooks directory of a repository
    the user never named and write there. `core.hooksPath` is configuration,
    not environment, so nothing of D2 is lost. `hook run`'s own git calls go
    through `git_snapshot._git`, which keeps inheriting `GIT_INDEX_FILE` on
    purpose: inside `git commit` it is the exact index being recorded.
    """
    environment = os.environ.copy()
    for name in _GIT_LOCATION_VARIABLES:
        environment.pop(name, None)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        return subprocess.run(
            ["git", "-C", str(repository), *args],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=_HOOK_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PrepareCLIError("repository is not a Git work tree") from exc


def _resolve_hooks_directory(repository: Path) -> tuple[str, Path | None, str]:
    """Where TAF may write the launcher, or why it must not.

    Returns `("redirected", None, <raw core.hooksPath value>)` when hooks are
    redirected elsewhere, or `("resolved", <absolute hooks dir>, <str of it>)`
    otherwise. Raises when `repository` is not a Git work tree at all.
    """
    repository = repository.resolve()
    configured = _run_repository_git(repository, "config", "--get", "core.hooksPath")
    if configured.returncode == 0:
        value = configured.stdout.decode("utf-8", "surrogateescape").rstrip("\n")
        return "redirected", None, value
    located = _run_repository_git(repository, "rev-parse", "--git-path", "hooks")
    if located.returncode != 0:
        raise PrepareCLIError("repository is not a Git work tree")
    raw = located.stdout.decode("utf-8", "surrogateescape").rstrip("\n")
    hooks_dir = Path(raw)
    if not hooks_dir.is_absolute():
        hooks_dir = repository / hooks_dir
    hooks_dir = hooks_dir.resolve()
    return "resolved", hooks_dir, str(hooks_dir)


def _classify_pre_commit(hook_path: Path) -> str:
    """"absent", "taf", or "foreign" - the only three things a hook file can be."""
    try:
        content = hook_path.read_text(encoding="utf-8", errors="surrogateescape")
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "foreign"
    return "taf" if LAUNCHER_MARKER in content.splitlines() else "foreign"


def _render_launcher(
    *, interpreter: Path, script: Path, chained_hook_path: Path | None, state_root: Path
) -> str:
    """The POSIX `sh` launcher body; every embedded path is `shlex.quote`d.

    `state_root` locates the self-healing pointer file
    (`refresh_launcher_target`'s `launcher_target_path(state_root)`); the
    launcher reads it first and falls back to the embedded `interpreter` and
    `script` only when the pointer is missing, unreadable, or names a script
    that no longer exists. When the chosen interpreter is not executable,
    `command -v python3` is tried as a last resort; when no interpreter or no
    script can be found, the launcher stays silent and the commit proceeds.
    """
    quoted_script = shlex.quote(str(script))
    quoted_interpreter = shlex.quote(str(interpreter))
    quoted_target = shlex.quote(str(launcher_target_path(state_root)))
    lines = [
        "#!/bin/sh",
        LAUNCHER_MARKER,
        '# Advisory: prints a short "TAF impact:" report on stderr and never blocks a commit.',
        "# Follows the TAF broker that last ran on this machine (a pointer under TAF's own",
        "# state); the embedded paths below are the fallback when that pointer is missing.",
        "taf_interpreter=" + quoted_interpreter,
        "taf_script=" + quoted_script,
        "taf_target=" + quoted_target,
        "taf_line1= taf_line2=",
        'if [ -r "$taf_target" ]; then',
        '  { IFS= read -r taf_line1; IFS= read -r taf_line2; } < "$taf_target"',
        '  if [ -n "$taf_line1" ] && [ -f "$taf_line2" ]; then',
        "    taf_interpreter=$taf_line1",
        "    taf_script=$taf_line2",
        "  fi",
        "fi",
        '[ -x "$taf_interpreter" ] || taf_interpreter=$(command -v python3 || :)',
        'if [ "${TAF_HOOK:-}" != "0" ] && [ -n "$taf_interpreter" ] && [ -f "$taf_script" ]; then',
        '  "$taf_interpreter" "$taf_script" hook run --repo "$PWD" || :',
        "fi",
    ]
    if chained_hook_path is not None:
        # `exec` terminates a non-interactive shell when its target cannot be
        # executed (126/127), which would make a deleted or no-longer-executable
        # backup block every commit in the repository. A chained hook that runs
        # decides the commit with its own exit code; one that cannot be run is
        # treated as absent, so the launcher stays advisory (D10).
        quoted_chained = shlex.quote(str(chained_hook_path))
        lines.append("if [ -x " + quoted_chained + " ]; then")
        lines.append("  exec " + quoted_chained + ' "$@"')
        lines.append("fi")
    return "\n".join(lines) + "\n"


def _write_launcher_atomically(hooks_dir: Path, hook_path: Path, source: str) -> None:
    """A temporary file in the hooks directory, mode 0o755, then `os.replace`."""
    import tempfile  # install-only dependency; kept off the hook run path

    hooks_dir.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(prefix=".pre-commit.", dir=hooks_dir)
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o755)
            stream.write(source)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, hook_path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        raise


def _write_launcher_target_atomically(directory: Path, path: Path, content: str) -> None:
    """A temporary file in `directory`, mode 0o600, then `os.replace` onto `path`."""
    import tempfile  # pointer-refresh dependency; kept off the hook run path

    descriptor, raw_temporary = tempfile.mkstemp(prefix=".launcher-target.", dir=directory)
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        raise

