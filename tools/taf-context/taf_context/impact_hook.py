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
    run_inspect,
    run_query,
)
from .git_snapshot import SnapshotError, _git
from .native_transport import NativeTransport, NativeTransportError


HOOK_TIME_LIMIT_SECONDS = 3.0  # wall clock the whole run gets before it goes silent
HOOK_MAXIMUM_LINES = 5  # warning lines written before the summary line takes over
HOOK_MAXIMUM_RESULTS = 16  # candidates the one impact query asks the engine for
HOOK_OUTPUT_CHARACTERS = 12000  # output budget of that query
HOOK_DISABLE_VARIABLE = "TAF_HOOK"  # set to exactly "0" to disable the hook entirely
HOOK_LINE_PREFIX = "TAF: "  # every warning the hook writes starts here

HOOK_FILE_NAME = "pre-commit"
CHAINED_HOOK_NAME = "pre-commit.taf-chained"
# The second line of every launcher this module writes; a `pre-commit` file
# containing this exact line is a TAF launcher, anything else is foreign.
LAUNCHER_MARKER = "# TAF commit-time impact warning (managed by: prepare hook install)"

_HOOK_GIT_TIMEOUT_SECONDS = 20  # wall clock for the small git calls hook management makes

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

        run = _HookRun(repository, environment)
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


def untouched_dependents(
    result: Mapping[str, object], staged_paths: Collection[str]
) -> list[dict]:
    """One representative candidate per untouched file the change set depends on.

    A dependent inside the commit is by definition already handled, and an
    inferred edge is a guess a hook must never make, so those candidates are
    dropped first, along with an anchorless one (so `format_warning_line`'s
    `anchors[0]` can never index an empty list). What is left is grouped by
    `path`, keeping each path's first-seen position: a warning names the
    *file*, not every candidate inside it, so a file that turns up as both an
    import and a call site gets one line, not two. Within a path the call
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
        if candidate.get("path") not in touched
        and candidate.get("edge_evidence") == "verified"
        and candidate.get("anchors")
    ]
    by_path: dict[object, list[dict]] = {}
    for candidate in filtered:
        by_path.setdefault(candidate.get("path"), []).append(candidate)
    representatives = [
        next((item for item in group if item.get("relation") == "call"), group[0])
        for group in by_path.values()
    ]
    non_test = [
        item for item in representatives if not _is_test_path(str(item.get("path")))
    ]
    test = [
        item for item in representatives if _is_test_path(str(item.get("path")))
    ]
    return non_test + test


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


def install_hook(
    repository: Path, *, chain: bool, interpreter: Path | None = None
) -> dict[str, object]:
    """Write the pre-commit launcher; idempotent, and the only write this module makes.

    `interpreter` exists so tests can pin it; every production caller leaves
    it as the default, which resolves `sys.executable` exactly the way the
    launcher's own embedded interpreter path must be resolved.
    """
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
    if kind == "foreign":
        if not chain:
            raise PrepareCLIError(
                "a foreign pre-commit hook exists; pass --chain to run it after TAF, "
                "or remove it first"
            )
        if chained_path.exists():
            raise PrepareCLIError(
                f"{CHAINED_HOOK_NAME} already exists; remove it before chaining another hook"
            )
        os.replace(hook_path, chained_path)  # mode is preserved by the rename
        chained_hook_path = chained_path
    else:
        # `kind` is "taf" (a re-install never drops a chained hook, regardless
        # of `--chain`) or "absent" (a `pre-commit.taf-chained` backup can be
        # left behind by hand or by a crash between the foreign->chained
        # rename and the launcher write below; either way, an existing
        # backup is adopted rather than orphaned).
        if chained_path.exists():
            chained_hook_path = chained_path
    source = _render_launcher(
        interpreter=resolved_interpreter, script=script, chained_hook_path=chained_hook_path
    )
    _write_launcher_atomically(hooks_dir, hook_path, source)
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
    # `remove` must not leave it stranded.
    restored = False
    if chained_path.exists():
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
        chained = chained_path.exists()
        hook_path_value = str(hook_path)
        kind = _classify_pre_commit(hook_path)
        if kind == "taf":
            hook_field = "installed"
            expected = _render_launcher(
                interpreter=Path(sys.executable).resolve(),
                script=_entry_point_script(),
                chained_hook_path=chained_path if chained else None,
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
        "readiness": {"next_safe_action": next_action, "error": error},
    }


def _entry_point_script() -> Path:
    """The plugin's stable entry point, located from this package's own path."""
    return (
        Path(__file__).resolve().parents[3]
        / "skills"
        / "prepare-repo-context"
        / "scripts"
        / "prepare_repo_context.py"
    )


def _run_repository_git(repository: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    """A plain `git -C <repo>` call over the caller's real environment.

    Unlike `git_snapshot._git`, this must see the caller's actual effective
    Git configuration - including a real `core.hooksPath` - so it copies
    `os.environ` rather than pinning the `GIT_CONFIG_*` overrides that would
    mask it.
    """
    environment = os.environ.copy()
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
    *, interpreter: Path, script: Path, chained_hook_path: Path | None
) -> str:
    """The POSIX `sh` launcher body; every embedded path is `shlex.quote`d."""
    quoted_script = shlex.quote(str(script))
    quoted_interpreter = shlex.quote(str(interpreter))
    lines = [
        "#!/bin/sh",
        LAUNCHER_MARKER,
        '# Advisory: prints at most a few "TAF:" lines on stderr and never blocks a commit.',
        "# After a TAF plugin update, re-run: prepare hook install --confirm-hook-write",
        'if [ "${TAF_HOOK:-}" != "0" ] && [ -f ' + quoted_script + " ]; then",
        "  " + quoted_interpreter + " " + quoted_script + ' hook run --repo "$PWD" || :',
        "fi",
    ]
    if chained_hook_path is not None:
        lines.append("exec " + shlex.quote(str(chained_hook_path)) + ' "$@"')
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

