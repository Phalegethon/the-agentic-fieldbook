"""Broker operations over the native engine, independent of the command line."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import time
from typing import Callable, Mapping

from . import refresh
from .change_ranges import (
    ChangedPath,
    changed_ranges,
    resolve_change_base,
    staged_change_base,
)
from .git_snapshot import collect_snapshot
from .level1_models import (
    Level1Finding,
    Level1Result,
    max_allowed_output_characters,
    max_collection_items,
    parse_level1_result,
)
from .native_transport import NativeTransport, NativeTransportError
from .refresh import (
    Binding,
    CHANGE_DOCUMENT_NAME,
    MAXIMUM_BINDING_DIRTY_PATHS,
    RefreshLock,
    build_change_document,
    changed_paths_between,
    dirty_paths_of,
    remove_change_document,
    write_change_document,
)
from .state_lifecycle import (
    CURRENT_FILENAME,
    CURRENT_RUNTIME_VERSION,
    GENERATION_PRUNE_GRACE_SECONDS,
    apply_plan,
    current_generation_token,
    incompatible_generation_version,
    plan_prune_generations,
    plan_remove,
    summarize_state,
    touch_binding,
)
from .state_paths import StateError, resolve_state_paths


_BINDING_LIMIT = 1024 * 1024
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_NATIVE_ENGINE_VERSION = CURRENT_RUNTIME_VERSION
FILTER_LANGUAGES = frozenset(
    {"go", "javascript", "json", "markdown", "python", "rust", "toml", "typescript"}
)
FILTER_SYMBOL_KINDS = frozenset(
    {"configuration", "definition", "document-chunk", "entry-point", "heading", "import", "module"}
)


class PrepareCLIError(ValueError):
    """A concise user-facing preparation error."""


def normalize_filter_values(values: list[str], flag: str, valid: frozenset[str]) -> list[str]:
    """Lower-case, validate, and canonicalize repeatable filter values."""
    normalized: set[str] = set()
    for value in values:
        candidate = value.strip().lower()
        if candidate not in valid:
            raise PrepareCLIError(
                f"invalid {flag} value {value!r}; valid values: {', '.join(sorted(valid))}"
            )
        normalized.add(candidate)
    return sorted(normalized)


def _state_paths(environment: Mapping[str, str]):
    home = environment.get("HOME") or environment.get("USERPROFILE")
    state_root_is_explicit = bool(environment.get("TAF_STATE_HOME"))
    windows_state_root_is_available = bool(
        sys.platform == "win32" and environment.get("LOCALAPPDATA")
    )
    if not home and not state_root_is_explicit and not windows_state_root_is_available:
        raise PrepareCLIError("home directory is unavailable")
    try:
        return resolve_state_paths(environment, sys.platform, Path(home or "."))
    except StateError as exc:
        raise PrepareCLIError(exc.code) from exc


def _repository_state_paths(
    root: Path, repository_identity: str, worktree_identity: str
) -> tuple[Path, Path]:
    repository_key = repository_identity.removeprefix("sha256:")
    worktree_key = worktree_identity.removeprefix("sha256:")
    controller_root = root.resolve(strict=False) / "repositories" / repository_key / worktree_key
    return controller_root / "native", controller_root / "binding.json"


def _resolve_native_binary(
    environment: Mapping[str, str], state_home: Path,
) -> tuple[Path | None, str | None]:
    configured = environment.get("TAF_LEVEL1_BINARY")
    if configured:
        candidate = Path(configured)
        _validate_binary(candidate)
        return candidate.resolve(), "environment"

    managed = _managed_binary_path(state_home)
    if managed.exists():
        _validate_binary(managed)
        return managed.resolve(), "managed"

    found = shutil.which("taf-level1", path=environment.get("PATH", ""))
    if found:
        candidate = Path(found)
        _validate_binary(candidate)
        return candidate.resolve(), "path"
    return None, None


def _platform_asset() -> tuple[str, str, str]:
    import platform  # activate-only dependency; kept off the query path

    if sys.platform == "darwin":
        system = "darwin"
    elif sys.platform.startswith("linux"):
        system = "linux"
    elif sys.platform == "win32":
        system = "windows"
    else:
        raise PrepareCLIError("native engine is unsupported on this platform")
    raw_machine = platform.machine().lower()
    if raw_machine in {"amd64", "x86_64"}:
        machine = "amd64"
    elif raw_machine in {"aarch64", "arm64"}:
        machine = "arm64"
    else:
        raise PrepareCLIError("native engine is unsupported on this architecture")
    if (system, machine) not in {
        ("darwin", "amd64"),
        ("darwin", "arm64"),
        ("linux", "amd64"),
        ("linux", "arm64"),
        ("windows", "amd64"),
    }:
        raise PrepareCLIError("native engine is unsupported on this platform")
    suffix = ".exe" if system == "windows" else ""
    asset = f"taf-level1_{_NATIVE_ENGINE_VERSION}_{system}_{machine}{suffix}"
    return system, machine, asset


def _managed_binary_path(state_home: Path) -> Path:
    system, machine, _asset = _platform_asset()
    filename = "taf-level1.exe" if system == "windows" else "taf-level1"
    return (
        state_home.resolve(strict=False)
        / "runtime"
        / _NATIVE_ENGINE_VERSION
        / f"{system}-{machine}"
        / filename
    )


def _validate_binary(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PrepareCLIError("configured native engine is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or not os.access(path, os.X_OK)
    ):
        raise PrepareCLIError("configured native engine is unsafe")


def _invoke_native(
    transport: NativeTransport,
    operation: str,
    repository: Path,
    state_root: Path,
    snapshot: object,
    *,
    index_identity: str | None,
    query: str | None = None,
    result_identities: tuple[str, ...] = (),
    direction: str | None = None,
    filters: dict[str, list[str]] | None = None,
    maximum_results: int = 8,
    maximum_output_characters: int = 4000,
    allow_inferred: bool = False,
    changed_paths_document: str | None = None,
    changed_selector: tuple[ChangedPath, ...] | None = None,
) -> Level1Result:
    request_filters = filters or {
        "path_prefixes": [],
        "languages": [],
        "symbol_kinds": [],
        "source_types": [],
    }
    material = [
        operation,
        snapshot.repository_identity,
        snapshot.worktree_identity,
        snapshot.head_sha,
        snapshot.dirty_fingerprint,
        index_identity or "none",
        query or "none",
        json.dumps(result_identities, separators=(",", ":")),
        json.dumps(request_filters, sort_keys=True, separators=(",", ":")),
        str(maximum_results),
        str(maximum_output_characters),
        str(allow_inferred),
    ]
    if direction is not None:
        # Appended only where it exists, so every schema-1 request identity
        # stays exactly what it was before the relationship operation.
        material.append(direction)
    selector = None if changed_selector is None else _selector_payload(changed_selector)
    if selector is not None:
        material.append(json.dumps(selector, separators=(",", ":")))
    request_material = "\0".join(material).encode("utf-8")
    request_identity = "taf.prepare." + hashlib.sha256(request_material).hexdigest()[:24]
    # Schema 2 exists for the one operation that carries a direction, schema 3
    # for the one that carries a change selector, and schema 4 for the one that
    # answers with a directory table; every other request stays byte-identical
    # to the frozen schema-1 envelope.
    schema_version = (
        "4"
        if operation == OVERVIEW_QUERY_OPERATION
        else "3"
        if selector is not None
        else "2"
        if direction is not None
        else "1"
    )
    envelope = {
        "phase": {
            "build": "build",
            "estimate": "estimate",
            "status": "inspect",
            "update": "update",
        }.get(operation, "query"),
        "repository_root": str(repository),
        "state_root": str(state_root),
        "changed_paths_document": changed_paths_document,
        "request": {
            "schema_version": schema_version,
            "request_identity": request_identity,
            "consumer_identity": "taf.prepare-repo-context",
            "operation": operation,
            "repository_identity": snapshot.repository_identity,
            "worktree_identity": snapshot.worktree_identity,
            "committed_head": snapshot.head_sha,
            "dirty_overlay_fingerprint": snapshot.dirty_fingerprint,
            "provider_identity": "taf-context",
            "index_identity": index_identity,
            "required_capability": operation,
            "minimum_freshness": "exact",
            "query": query,
            "result_identities": list(result_identities),
            "filters": request_filters,
            "maximum_results": maximum_results,
            "maximum_model_output_characters": maximum_output_characters,
            "allow_inferred": allow_inferred,
        },
    }
    if schema_version in {"2", "3", "4"}:
        # Schemas 3 and 4 spell both added keys out, direction included,
        # because the engine requires the whole key set of the schema it is
        # given; schema 4 reuses the schema-3 key set with both keys null.
        envelope["request"]["direction"] = direction
    if schema_version in {"3", "4"}:
        envelope["request"]["changed_ranges"] = selector
    wire = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    try:
        raw = transport.exchange(wire, idempotent=operation not in {"build", "update"})
    except NativeTransportError as exc:
        if exc.reason in {"invocation-failed", "timeout"}:
            raise PrepareCLIError("native engine invocation failed") from exc
        raise PrepareCLIError("native engine rejected the request") from exc
    try:
        result = parse_level1_result(raw)
    except (UnicodeError, ValueError) as exc:
        raise PrepareCLIError("native engine returned invalid output") from exc
    if (
        result.request_identity != request_identity
        or result.operation.value != operation
        or result.repository_identity != snapshot.repository_identity
        or result.worktree_identity != snapshot.worktree_identity
        or result.committed_head != snapshot.head_sha
        or result.dirty_overlay_fingerprint != snapshot.dirty_fingerprint
    ):
        raise PrepareCLIError("native engine returned mismatched output")
    return result


def _refresh_if_stale(
    transport: NativeTransport,
    repository: Path,
    state_root: Path,
    binding_path: Path,
    binding: Binding,
    snapshot: object,
    state_home: Path,
) -> tuple[Binding, dict[str, object]]:
    """Bring the bound index to the current snapshot with one `update` when possible.

    Returns the binding to query with and the summary `refresh` block. The
    binding is standing consent for incremental refresh; a full rebuild stays
    explicit, so an uncomputable delta or a structurally stale index falls
    back to the existing refusal.
    """
    idle = {"performed": False, "changed_path_count": 0, "duration_ms": 0}
    if not binding.has_delta_inputs:
        return binding, idle
    if binding.head_sha == snapshot.head_sha and binding.dirty_fingerprint == snapshot.dirty_fingerprint:
        return binding, idle
    changed = changed_paths_between(binding, snapshot)
    if changed is None:
        return binding, idle  # the operation call reports stale; today's refusal follows
    for attempt in range(2):
        started = time.perf_counter()
        with RefreshLock(state_root):
            # The generation CURRENT names right now, before this attempt's
            # update, is a generation a reader may already be using. The store
            # can reuse an existing generation directory for identical content,
            # so its mtime alone is not a reliable signal once it is superseded;
            # name it explicitly so pruning below never removes it. Read inside
            # the lock so a concurrent refresh cannot publish between the read
            # and this attempt's acquisition.
            protected_generation = current_generation_token(state_root) or None
            try:
                write_change_document(state_root, build_change_document(binding, snapshot, changed))
            except OSError as exc:
                raise PrepareCLIError("context state is unavailable") from exc
            try:
                result = _invoke_native(
                    transport, "update", repository, state_root, snapshot,
                    index_identity=binding.index_identity, changed_paths_document=CHANGE_DOCUMENT_NAME,
                )
            finally:
                remove_change_document(state_root)
            if result.status.value in {"ready", "partial"} and result.index_identity is not None:
                _write_binding(binding_path, snapshot, result.index_identity)
                pruned_count, prune_warnings = _prune_generations(
                    state_home, binding_path.parent, protected_generation
                )
                duration = int((time.perf_counter() - started) * 1000)
                dirty_paths = dirty_paths_of(snapshot)
                refreshed = Binding(
                    result.index_identity,
                    snapshot.head_sha,
                    snapshot.dirty_fingerprint,
                    None if len(dirty_paths) > MAXIMUM_BINDING_DIRTY_PATHS else dirty_paths,
                )
                block: dict[str, object] = {
                    "performed": True,
                    "changed_path_count": len(changed),
                    "duration_ms": duration,
                    "pruned_generation_count": pruned_count,
                }
                if prune_warnings:
                    block["warnings"] = prune_warnings
                return refreshed, block
        if result.status.value == "stale":
            raise PrepareCLIError("ready context is required; run prepare inspect")
        if attempt == 0:
            # Another process may have published a newer generation and rewritten
            # the binding; re-read once and retry with the same snapshot.
            reread = _read_binding(binding_path, snapshot)
            if reread is None:
                break
            binding = reread
            if binding.head_sha == snapshot.head_sha and binding.dirty_fingerprint == snapshot.dirty_fingerprint:
                return binding, idle
            changed = changed_paths_between(binding, snapshot)
            if changed is None:
                break
    raise PrepareCLIError("incremental refresh failed; run prepare build --confirm-state-write")


def _prune_generations(
    state_home: Path, entry: Path, protected: str | None = None
) -> tuple[int, list[str]]:
    """Delete generations no reader can still be using; never raise.

    `protected` names the generation CURRENT pointed at immediately before
    this refresh's `update` call; it is excluded from the plan even if its
    mtime alone would otherwise look aged and unreferenced. Returns the count
    of generations actually removed and any warnings; a failure midway leaves
    whatever had already been removed in place but is reported as zero
    removed here rather than guessed at.
    """
    try:
        # `entry` (binding_path.parent) is built from a symlink-resolved root
        # (see _repository_state_paths); resolve state_home the same way so
        # candidate relative paths stay consistent with it (macOS commonly
        # maps a temp/state path through /var -> /private/var).
        root = state_home.resolve(strict=False)
        plan = plan_prune_generations(root, entry, now=time.time())
        if protected:
            plan = [candidate for candidate in plan if not candidate.relative_path.endswith(protected)]
        removed = apply_plan(root, plan)
    except (StateError, OSError, ValueError):
        return 0, ["retention-prune-incomplete"]
    return len(removed), []


def _run_convergence_prune(state_home: Path, state_root: Path) -> tuple[int, list[str]]:
    """Prune superseded generations once more, protecting whatever CURRENT names now.

    Used after a successful `build`/`activate` (which never call
    `_refresh_if_stale` at all) and after an idle query (one whose refresh
    performed no `update`, so nothing in this process was just superseded).
    Across processes, though, another session on the same repository may
    have published a newer generation moments ago, and the generation it
    superseded is exactly the ordinary aged, unreferenced candidate this
    prune would otherwise remove out from under a reader that is still
    opening it. CURRENT's own mtime marks the moment this entry last
    published, so nothing can have been superseded more recently than that:
    skip the prune entirely while CURRENT is younger than the grace period
    every candidate generation itself gets, and only prune once the
    repository has been quiet for that long. `state_root` is the entry's
    `native` directory; its parent is the entry `_prune_generations` expects.
    """
    try:
        published = (state_root / CURRENT_FILENAME).lstat().st_mtime
    except OSError:
        published = 0.0
    if time.time() - published <= GENERATION_PRUNE_GRACE_SECONDS:
        return 0, []
    protected = current_generation_token(state_root) or None
    return _prune_generations(state_home, state_root.parent, protected)


def _fold_prune_into_refresh(
    refresh: dict[str, object], pruned_count: int, warnings: list[str]
) -> dict[str, object]:
    """Merge one more prune's outcome into a refresh block, before it is used.

    The only caller passes this an idle refresh block, and `_refresh_if_stale`
    sets `pruned_generation_count` only on a performed refresh, so the block
    never already carries the key here; this sets it plainly rather than
    accumulating into a total that would always start at zero. Warnings fold
    in the same way the refresh path's own warnings already are, so the
    caller's existing pop-and-merge into the top-level `warnings` list picks
    both up unchanged.
    """
    merged = dict(refresh)
    merged["pruned_generation_count"] = pruned_count
    if warnings:
        merged["warnings"] = sorted(set(merged.get("warnings", [])) | set(warnings))
    return merged


def _converge_retention_after_query(
    refresh: dict[str, object], state_home: Path, state_root: Path
) -> dict[str, object]:
    """Fold in one more prune after this query's own engine call, when it did not itself refresh.

    A performed refresh already ran `_prune_generations` for this entry
    inside `_refresh_if_stale`, with the one exclusion a
    generation-directory-reuse race needs: the
    generation CURRENT named immediately before that `update`, read while
    still holding the refresh lock. Running a second, independently-protected
    prune right after would undo exactly that exclusion, since a fresh read
    of CURRENT here would no longer name the one that call had to protect. An
    idle refresh performed no `update`, so this process superseded nothing -
    but that says nothing about another session on the same repository,
    which might have published moments ago. What actually keeps this second
    prune safe is `_run_convergence_prune`'s own gate on CURRENT's age,
    together with the grace `plan_prune_generations` already gives every
    unreferenced candidate; with both in place, this is the prune that lets a
    repository which is only ever queried, never rebuilt, still converge once
    it has been quiet for the grace period.
    """
    if refresh.get("performed"):
        return refresh
    pruned_count, prune_warnings = _run_convergence_prune(state_home, state_root)
    return _fold_prune_into_refresh(refresh, pruned_count, prune_warnings)


def _remove_state_entry(state_home: Path, snapshot: object) -> None:
    """Delete this repository's state entry; never raise.

    This is exactly what `remove --confirm-state-write` deletes, and the whole
    entry goes rather than the generation alone: the binding names an index
    this runtime cannot read, and CURRENT would otherwise be left pointing at
    nothing. A removal that fails leaves the build to refuse, and the refusal
    then carries the warning that explains it.
    """
    try:
        root = state_home.resolve(strict=False)
        apply_plan(
            root,
            plan_remove(
                root,
                snapshot.repository_identity.removeprefix("sha256:"),
                snapshot.worktree_identity.removeprefix("sha256:"),
            ),
        )
    except (StateError, OSError, ValueError):
        return


def _query_summary(
    result: Level1Result, refresh: dict[str, object] | None = None
) -> dict[str, object]:
    refresh = dict(refresh) if refresh else {"performed": False, "changed_path_count": 0, "duration_ms": 0}
    prune_warnings = refresh.pop("warnings", [])
    warnings = list(result.warnings)
    if prune_warnings:
        warnings = sorted(set(warnings) | set(prune_warnings))
    # The summary's own schema is unchanged; a relationship result simply
    # carries the four extra schema-2 keys on each finding. The overview names
    # no relationship at all - schema 4 reuses the schema-2 finding shape only
    # because the wire has to name a shape, so its findings are compacted back
    # to the twelve base keys here, matching repository-map and search-*.
    findings = [item.to_dict(result.schema_version) for item in result.findings]
    if result.operation.value == OVERVIEW_QUERY_OPERATION:
        findings = [_compact_finding(item) for item in findings]
    summary: dict[str, object] = {
        "schema_version": "1",
        "mode": "query",
        "operation": result.operation.value,
        "status": result.status.value,
        "freshness": result.freshness.value,
        "index_identity": result.index_identity,
        "findings": findings,
        "returned_count": result.returned_count,
        "omitted_count": result.omitted_count,
        "truncated": result.truncated,
        "output_characters": result.output_characters,
        "warnings": warnings,
        "required_authorizations": [],
        "next_safe_action": result.next_safe_action,
        "refresh": refresh,
    }
    if result.groups is not None and result.overview is not None:
        # Schema 4 adds the directory table. It is carried verbatim here; what
        # the caller's output budget cannot hold is folded into the `*` row
        # afterwards, by `fit_overview_to_budget`.
        summary["groups"] = [item.to_dict() for item in result.groups]
        summary["overview"] = result.overview.to_dict()
    return summary


def _summary(
    *,
    mode: str,
    snapshot: object,
    binary: Path | None,
    binary_source: str | None,
    result: Level1Result | None,
    estimate: Level1Result | None,
    authorizations: tuple[str, ...],
    state: dict[str, int],
    refresh: dict[str, object] | None = None,
    replaced_generation_version: str | None = None,
    extra_warnings: tuple[str, ...] = (),
) -> dict[str, object]:
    if binary is None:
        next_action = "install-native-engine"
    elif result is None:
        next_action = "build-index"
    else:
        next_action = result.next_safe_action
    context_status = "unavailable" if result is None else result.status.value
    freshness = "unknown" if result is None else result.freshness.value
    refresh = dict(refresh) if refresh else {"performed": False, "changed_path_count": 0, "duration_ms": 0}
    prune_warnings = refresh.pop("warnings", [])
    return {
        "schema_version": "1",
        "mode": mode,
        "repository": {
            "branch": snapshot.branch,
            "committed_head": snapshot.head_sha,
            "dirty": bool(
                snapshot.staged_paths
                or snapshot.unstaged_paths
                or snapshot.untracked_paths
            ),
            "tracked_file_count": len(snapshot.tracked_paths),
            "language_counts": dict(snapshot.language_counts),
        },
        "engine": {
            "availability": "available" if binary is not None else "unavailable",
            "source": binary_source,
            # The runtime that wrote the generation this build had to remove
            # before it could write its own; `null` whenever nothing was
            # replaced, which is every inspect and every ordinary build.
            "replaced_generation_version": replaced_generation_version,
        },
        "context": {
            "status": context_status,
            "freshness": freshness,
            "index_identity": None if result is None else result.index_identity,
            "coverage": None if result is None else result.coverage.to_dict(),
        },
        "estimate": {
            "eligible_path_count": (
                len(snapshot.tracked_paths)
                if estimate is None
                else estimate.coverage.indexed_path_count
            ),
            "excluded_path_count": (
                snapshot.ignored_entry_count
                if estimate is None
                else estimate.coverage.excluded_path_count
            ),
        },
        "state": state,
        "required_authorizations": list(authorizations),
        "next_safe_action": next_action,
        "warnings": sorted(
            set(() if result is None else result.warnings)
            | set(prune_warnings)
            | set(extra_warnings)
        ),
        "refresh": refresh,
    }


def _read_binding(binding_path: Path, snapshot: object) -> Binding | None:
    try:
        metadata = binding_path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PrepareCLIError("context binding is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size > _BINDING_LIMIT
        or metadata.st_nlink != 1
    ):
        raise PrepareCLIError("context binding is unsafe")
    try:
        value = json.loads(binding_path.read_bytes().decode("utf-8", "surrogateescape"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PrepareCLIError("context binding is invalid") from exc
    expected = {
        "repository_identity": snapshot.repository_identity,
        "worktree_identity": snapshot.worktree_identity,
    }
    if (
        type(value) is not dict
        or value.get("schema_version") not in {"1", "2"}
        or any(value.get(key) != item for key, item in expected.items())
    ):
        return None
    index_identity = value.get("index_identity")
    if not isinstance(index_identity, str) or not _SHA256.fullmatch(index_identity):
        raise PrepareCLIError("context binding is invalid")
    if value["schema_version"] == "1":
        return Binding(index_identity, None, None, None)
    head_sha = value.get("head_sha")
    dirty_fingerprint = value.get("dirty_fingerprint")
    dirty_paths = value.get("dirty_paths")
    if (
        not isinstance(head_sha, str)
        or not _OBJECT_ID.fullmatch(head_sha)
        or not isinstance(dirty_fingerprint, str)
        or not _SHA256.fullmatch(dirty_fingerprint)
        or not (
            dirty_paths is None
            or (isinstance(dirty_paths, list) and all(isinstance(item, str) for item in dirty_paths))
        )
    ):
        raise PrepareCLIError("context binding is invalid")
    return Binding(
        index_identity, head_sha, dirty_fingerprint, None if dirty_paths is None else tuple(dirty_paths)
    )


def _write_binding(binding_path: Path, snapshot: object, index_identity: str) -> None:
    import tempfile  # deferred so importing the CLI stays free of tempfile (Phase 2 import-path test)

    if not _SHA256.fullmatch(index_identity):
        raise PrepareCLIError("native engine returned invalid index identity")
    binding_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for parent in (
        binding_path.parents[2],
        binding_path.parents[1],
        binding_path.parent,
    ):
        try:
            parent.chmod(0o700)
        except OSError as exc:
            raise PrepareCLIError("context state is unavailable") from exc
    dirty_paths = dirty_paths_of(snapshot)
    payload = json.dumps(
        {
            "schema_version": "2",
            "repository_identity": snapshot.repository_identity,
            "worktree_identity": snapshot.worktree_identity,
            "index_identity": index_identity,
            "head_sha": snapshot.head_sha,
            "dirty_fingerprint": snapshot.dirty_fingerprint,
            "dirty_paths": None if len(dirty_paths) > refresh.MAXIMUM_BINDING_DIRTY_PATHS else list(dirty_paths),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8", "surrogateescape") + b"\n"
    descriptor, temporary = tempfile.mkstemp(prefix=".binding-", dir=binding_path.parent)
    temporary_path = Path(temporary)
    try:
        _set_descriptor_mode(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, binding_path)
        _fsync_directory(binding_path.parent)
    except OSError as exc:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise PrepareCLIError("context binding could not be saved") from exc
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _set_descriptor_mode(descriptor: int, mode: int) -> None:
    if os.name == "posix":
        os.fchmod(descriptor, mode)


def _fsync_directory(path: Path) -> None:
    if os.name != "posix" or not hasattr(os, "O_DIRECTORY"):
        return
    directory_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


TransportFactory = Callable[[Path], NativeTransport]
Installer = Callable[[Mapping[str, str], Path], Path]


@dataclass(frozen=True)
class QueryArguments:
    """One validated read-only query, independent of how it was requested."""

    operation: str
    query: str | None
    result_identities: tuple[str, ...]
    path_prefixes: list[str]
    languages: list[str]
    symbol_kinds: list[str]
    source_types: list[str]
    maximum_results: int
    maximum_output_characters: int
    allow_inferred: bool
    direction: str | None = None
    base: str | None = None
    staged: bool = False


QUERY_DIRECTIONS = ("callers", "callees", "importers", "imports")
# The query operations that name records instead of searching for them.
IDENTITY_QUERY_OPERATIONS = ("source-snippets", "related-symbols")
# One relationship request stays cheap to resolve, so it carries few anchors.
MAXIMUM_RELATED_ANCHORS = 16
# The two operations that answer from a Git difference instead of from a query
# string or a result identity, and the only ones that accept a base or --staged.
CHANGE_QUERY_OPERATIONS = ("changed-symbols", "impact-candidates")
# The operation that answers with a directory table instead of a ranked
# search: it names no query, no anchor, no direction, and no base.
OVERVIEW_QUERY_OPERATION = "repository-overview"
# The row the table's tail folds into. It speaks for several directories at
# once, so `level1_models` refuses it a representative file; the same literal
# is the wire's, and a table carries at most one of it.
OVERVIEW_OTHER_PREFIX = "*"
# The two layers of an overview answer - the directory table and the ranked
# file layer - share the caller's output budget, and neither takes more than
# its half while the other still wants room. The engine hands both far wider
# than a small budget holds, so a rule that fitted one layer first would always
# spend the other one entirely: fitting the table first collapses it to a
# single row, and fitting the file layer first answers 8000 characters with
# four files. Halving is what makes a wider budget buy a wider table and more
# files at the same time.
OVERVIEW_BUDGET_SHARES = 2
# An impact answer has two layers too - the change set in `changed` and the
# candidates in `findings` - and the same reasoning gives the change set a
# guaranteed share of the budget. It is a third rather than a half because the
# candidates are what the operation was asked for and each of them costs far
# more than a changed entry: a third at the default budget carries a compact
# change set of a size a real diff produces, and the candidates keep the rest.
CHANGED_BUDGET_SHARES = 3
# The two fields a changed entry keeps when its layer is over its share: where
# it lives and what it is. The identity is not one of them - it is available
# from `changed-symbols` and from every candidate's `anchors`, and a real
# 71-character `sha256:` identity by itself would halve what the share can
# carry.
CHANGED_COMPACT_KEYS = ("path", "qualified_name")
# The five counters every group row sums when two rows become one.
_OVERVIEW_COUNTERS = (
    "file_count",
    "definition_count",
    "entry_point_count",
    "document_count",
    "configuration_count",
)
# The output budget an operation answers with when the caller names none.
# repository-overview's group table alone is about 3700 characters of
# canonical JSON, so it gets a larger default than the shared 4000 and both
# surfaces resolve it here, which is what keeps them from drifting apart.
DEFAULT_OUTPUT_CHARACTERS = 4000
DEFAULT_OUTPUT_CHARACTERS_BY_OPERATION: dict[str, int] = {
    OVERVIEW_QUERY_OPERATION: 8000,
    # The composed answer carries the change set as well as the candidates, so
    # 4000 characters were spent on context before the first candidate: the
    # operation is the one that answers "what could my change break", and it
    # could not answer it for a change set of any size.
    "impact-candidates": 8000,
}
# The result count an operation answers with when the caller names none.
# repository-overview lists directories as well as files, so an unbudgeted
# request wants more than the shared 8's worth of files to get a feel for a
# whole repository; both surfaces resolve it here, which is what keeps them
# from drifting apart.
DEFAULT_MAXIMUM_RESULTS = 8
DEFAULT_MAXIMUM_RESULTS_BY_OPERATION: dict[str, int] = {
    OVERVIEW_QUERY_OPERATION: 24,
    # The candidates are what this operation was asked for, not context
    # alongside them, and the output budget already bounds what fits; on a
    # small change set the shared 8 bound the answer before the budget's
    # 8000 characters were anywhere near spent. 16 keeps the same budget
    # bounding the answer whenever the change set is wider.
    "impact-candidates": 16,
}
# The overview is fitted entirely by the broker, so it asks the engine for the
# widest answer the wire allows - the largest of the budgets the request
# schema accepts. Handing the engine the caller's own budget instead let the
# engine's own fit - which measures rendered lines and their previews, not the
# object the broker sends - drop findings before the broker ever saw them, and
# no rule here could buy them back: on a repository whose previews are long,
# an 8000-character overview arrived with four of the eight files it asked
# for.
OVERVIEW_ENGINE_OUTPUT_CHARACTERS = max_allowed_output_characters()
# The composed operation asks the engine for the widest change set and the
# widest relationship answer it will give, then trims what it composed to the
# caller's own budget.
IMPACT_CHANGED_MAXIMUM_RESULTS = 64
IMPACT_CHANGED_OUTPUT_CHARACTERS = 12000
# The composed result trims its own context before its answer, and says so.
WARNING_CHANGED_LIST_TRIMMED = "changed-list-trimmed"
WARNING_OUTPUT_BUDGET_EXCEEDED = "output-budget-exceeded"
# The selector guard shrinks the request frame, which is a different loss from
# the change-range limits of `change_ranges`, so it carries its own codes.
WARNING_SELECTOR_COLLAPSED = "changed-selector-collapsed"
WARNING_SELECTOR_LIMIT = "changed-selector-limit"
# Which changed record kinds anchor which relationship question.
_CALLER_ANCHOR_KINDS = ("definition", "entry-point")
_IMPORTER_ANCHOR_KINDS = ("module", "definition")
_EVIDENCE_TIERS = {"verified": 0, "inferred": 1}
# `changed-symbols` never returns any other kind, and decision 1(b) of the
# 2.7.1 anchor-order fix ranks a caller-anchor kind ahead of `module`.
_CHANGED_KIND_RANK = {"definition": 0, "entry-point": 0, "module": 1}
# The three conventional markers of a test file's base name, and the three
# directory segment names that mark a test tree - decision 1 of the 2.7.1
# anchor-order fix.
_TEST_PATH_MARKERS = (".test.", ".spec.", "_test.")
_TEST_PATH_SEGMENTS = ("tests", "test", "__tests__")
# The engine refuses a request frame above 256 KiB (wire.MaximumWireBytes).
MAXIMUM_REQUEST_BYTES = 256 * 1024
# Everything in the frame but the selector: the two absolute roots, the
# identities, the budgets, and the filters. Only the filters can grow, and the
# wire caps them at 64 path prefixes of 512 characters, so 48 KiB covers the
# worst frame the engine would still accept.
_SELECTOR_RESERVE_BYTES = 48 * 1024
MAXIMUM_CHANGED_SELECTOR_BYTES = MAXIMUM_REQUEST_BYTES - _SELECTOR_RESERVE_BYTES


def default_output_characters(operation: str) -> int:
    """The output budget one operation answers with when the caller names none."""
    return DEFAULT_OUTPUT_CHARACTERS_BY_OPERATION.get(operation, DEFAULT_OUTPUT_CHARACTERS)


def default_maximum_results(operation: str) -> int:
    """The result count one operation answers with when the caller names none."""
    return DEFAULT_MAXIMUM_RESULTS_BY_OPERATION.get(operation, DEFAULT_MAXIMUM_RESULTS)


def normalize_change_base(base: str | None) -> str | None:
    """Strip a requested change base and refuse one no Git ref could match.

    The CLI and the MCP server must resolve the same base for the same
    request, so both normalize here instead of each on its own.
    """
    if base is None:
        return None
    normalized = base.strip() if isinstance(base, str) else ""
    if (
        not normalized
        or len(normalized) > 512
        or any(character in normalized for character in "\x00\r\n")
    ):
        raise PrepareCLIError("selected change base is invalid")
    return normalized


def validate_query_request(
    operation: str,
    query: str | None,
    result_identities: tuple[str, ...],
    direction: str | None = None,
    base: str | None = None,
    *,
    symbol_kinds: list[str] | tuple[str, ...] = (),
    source_types: list[str] | tuple[str, ...] = (),
    staged: bool = False,
) -> tuple[str | None, tuple[str, ...]]:
    """Apply the query/result-identity rules shared by the CLI and the MCP server."""
    query_operations = {"search-symbols", "search-docs"}
    identity_operations = set(IDENTITY_QUERY_OPERATIONS)
    query_text = query.strip() if isinstance(query, str) else None
    if operation in query_operations and not query_text:
        raise PrepareCLIError("selected query operation requires --query")
    if operation not in query_operations and query_text is not None:
        raise PrepareCLIError("selected query operation does not accept --query")
    identities = tuple(sorted(set(result_identities)))
    if operation in identity_operations and not identities:
        raise PrepareCLIError(f"{operation} requires at least one --result-id")
    if operation not in identity_operations and identities:
        raise PrepareCLIError("selected query operation does not accept --result-id")
    if any(_SHA256.fullmatch(item) is None for item in identities):
        raise PrepareCLIError("query result identity is invalid")
    if operation == "related-symbols":
        if direction is None:
            raise PrepareCLIError("related-symbols requires --direction")
        if len(identities) > MAXIMUM_RELATED_ANCHORS:
            raise PrepareCLIError(
                f"related-symbols accepts at most {MAXIMUM_RELATED_ANCHORS} --result-id values"
            )
    elif direction is not None:
        raise PrepareCLIError("selected query operation does not accept --direction")
    if direction is not None and direction not in QUERY_DIRECTIONS:
        raise PrepareCLIError("selected query direction is invalid")
    if base is not None:
        if operation not in CHANGE_QUERY_OPERATIONS:
            raise PrepareCLIError("selected query operation does not accept --base")
        normalize_change_base(base)
    if staged:
        if operation not in CHANGE_QUERY_OPERATIONS:
            raise PrepareCLIError("selected query operation does not accept --staged")
        if base is not None:
            raise PrepareCLIError("--staged and --base are mutually exclusive")
    if operation == OVERVIEW_QUERY_OPERATION:
        # The overview describes whole directories, so it narrows by path and
        # by language and never by a symbol-shaped filter. The engine refuses
        # such a request outright; refuse it here with a message that names
        # what to drop.
        if symbol_kinds:
            raise PrepareCLIError("selected query operation does not accept --symbol-kind")
        if source_types:
            raise PrepareCLIError("selected query operation does not accept --source-type")
    return query_text, identities


def _resolve_repository(repository: Path, environment: Mapping[str, str]):
    repository = repository.resolve()
    snapshot = collect_snapshot(repository)
    if snapshot.head_sha is None:
        raise PrepareCLIError("repository must have at least one commit")
    paths = _state_paths(environment)
    state_root, binding_path = _repository_state_paths(
        paths.root, snapshot.repository_identity, snapshot.worktree_identity
    )
    return repository, snapshot, paths, state_root, binding_path


def _selector_payload(changed: tuple[ChangedPath, ...]) -> list[dict[str, object]]:
    """The wire shape of a change selector: sorted paths, ascending spans."""
    return [
        {"path": item.path, "ranges": [[start, end] for start, end in item.ranges]}
        for item in changed
    ]


def _selector_bytes(entries: list[ChangedPath]) -> int:
    return len(
        json.dumps(_selector_payload(tuple(entries)), separators=(",", ":")).encode("utf-8")
    )


def bound_changed_selector(
    changed: tuple[ChangedPath, ...],
    *,
    available_bytes: int = MAXIMUM_CHANGED_SELECTOR_BYTES,
) -> tuple[tuple[ChangedPath, ...], list[str]]:
    """Shrink a change selector until the request frame can carry it.

    The path and span counts are already bounded (200 paths, 64 spans each),
    but a selector of long paths and high line numbers reaches about 340 KB,
    and the engine refuses any frame above 256 KiB as invalid. Collapsing the
    path with the most spans to a whole-file entry costs precision on that one
    path and nothing else, and a whole-file entry is at most 512 bytes of path
    plus its keys, so 200 of them are about 108 KB and the loop always reaches
    the budget. Dropping a path from the tail is the last resort for a budget
    too small for even that.
    """
    entries = list(changed)
    warnings: set[str] = set()
    while entries and _selector_bytes(entries) > available_bytes:
        widest = _widest_selector_entry(entries)
        if widest is None:
            entries.pop()
            warnings.add(WARNING_SELECTOR_LIMIT)
            continue
        entries[widest] = ChangedPath(entries[widest].path, ())
        warnings.add(WARNING_SELECTOR_COLLAPSED)
    return tuple(entries), sorted(warnings)


def _widest_selector_entry(entries: list[ChangedPath]) -> int | None:
    """The entry with the most spans; None when every entry is whole-file."""
    widest: int | None = None
    for index, entry in enumerate(entries):
        if not entry.ranges:
            continue
        if widest is None or (len(entry.ranges), entry.path) > (
            len(entries[widest].ranges),
            entries[widest].path,
        ):
            widest = index
    return widest


# The four keys a relationship edge adds to a finding (schema 2 on); the
# overview reuses that wire shape for a table it never edges, so its own
# findings are compacted back to the base twelve here.
_RELATIONSHIP_FINDING_KEYS = ("relation", "edge_evidence", "reference_line", "reference_count")


def _compact_finding(finding: dict[str, object]) -> dict[str, object]:
    """The twelve base finding keys, without the four relationship extras."""
    return {
        key: value for key, value in finding.items() if key not in _RELATIONSHIP_FINDING_KEYS
    }


def _changed_entry(finding: Level1Finding) -> dict[str, object]:
    """The compact record of one changed symbol."""
    return {
        "result_identity": finding.result_identity,
        "path": finding.path,
        "start_line": finding.start_line,
        "end_line": finding.end_line,
        "record_kind": finding.record_kind.value,
        "qualified_name": finding.qualified_name,
    }


def _anchor_entry(anchor: Level1Finding, edge: Level1Finding) -> dict[str, object]:
    """One changed symbol a candidate depends on, with the edge that reached it."""
    return {
        "result_identity": anchor.result_identity,
        "path": anchor.path,
        "qualified_name": anchor.qualified_name,
        "relation": edge.relation,
        "edge_evidence": None if edge.edge_evidence is None else edge.edge_evidence.value,
        "reference_line": edge.reference_line,
        "reference_count": edge.reference_count,
    }


def _evidence_tier(evidence: object) -> int:
    return _EVIDENCE_TIERS.get(str(evidence), len(_EVIDENCE_TIERS))


def _is_test_path(path: str) -> bool:
    """Whether `path` names a test file - decision 1 of the 2.7.1 anchor-order fix.

    A path is a test file when its base name carries one of the three
    conventional markers, or when one of its directory segments is a `tests`,
    `test`, or `__tests__` folder. Repository paths are always POSIX-relative
    (`level1_models` reads them with `PurePosixPath`), so `/` is the only
    separator to split on.
    """
    segments = path.split("/")
    base = segments[-1]
    if any(marker in base for marker in _TEST_PATH_MARKERS):
        return True
    return any(segment in _TEST_PATH_SEGMENTS for segment in segments[:-1])


def _anchor_counts(findings: list[dict[str, object]]) -> dict[str, int]:
    """How many of `findings` each changed identity anchors.

    Decision 1(a) of the 2.7.1 anchor-order fix ranks a changed entry by this
    count; an identity with no returned candidate is not a key here at all,
    which is what tells group (a) - an anchor of a returned candidate - from
    group (b) - the rest of the changed set.
    """
    counts: dict[str, int] = {}
    for finding in findings:
        for anchor in finding["anchors"]:
            identity = anchor["result_identity"]
            counts[identity] = counts.get(identity, 0) + 1
    return counts


def _changed_sort_key(
    entry: dict[str, object], anchor_counts: dict[str, int]
) -> tuple[object, ...]:
    """The order `changed` is composed in - decision 1 of the 2.7.1 fix.

    Group (a) - an entry that anchors at least one returned candidate - sorts
    ahead of everything else, the most-anchored entries first, then by path,
    then by start line. Group (b) - the rest - sorts non-test paths before
    test paths, a caller-anchor kind (`definition`/`entry-point`) before
    `module`, then by path, then by start line. The two groups' tuples never
    compare past their first element, so they need not be the same shape.
    """
    count = anchor_counts.get(entry["result_identity"], 0)
    if count:
        return (0, -count, entry["path"], entry["start_line"])
    return (
        1,
        _is_test_path(str(entry["path"])),
        _CHANGED_KIND_RANK.get(str(entry["record_kind"]), 1),
        entry["path"],
        entry["start_line"],
    )


def _anchor_identities(findings: list[dict[str, object]]) -> set[str]:
    """Identities of changed symbols that anchor at least one of `findings`.

    Called again after the budget drops a candidate (decision 2 of the 2.7.1
    anchor-order fix), so an entry whose only candidate was just dropped
    stops being reported here and becomes trimmable.
    """
    identities: set[str] = set()
    for finding in findings:
        for anchor in finding["anchors"]:
            identities.add(anchor["result_identity"])
    return identities


def compose_impact_candidates(
    changed: Level1Result,
    related: Callable[[tuple[str, ...], str], Level1Result],
    *,
    allow_inferred: bool,
    maximum_results: int,
) -> dict[str, object]:
    """Compose the one-hop dependents of a change set from relationship calls.

    `related` answers one `related-symbols` question about the anchors it is
    given. Anchors are asked for one at a time, because the engine returns one
    edge per candidate record without naming the anchor it came from and this
    operation exists to name it. Callers answer for changed definitions and
    entry points, importers for changed modules and definitions; a changed
    symbol is never its own candidate, and every candidate keeps the changed
    symbols it depends on with the edge that reached it.
    """
    anchors = {
        "callers": [
            item for item in changed.findings
            if item.record_kind.value in _CALLER_ANCHOR_KINDS
        ],
        "importers": [
            item for item in changed.findings
            if item.record_kind.value in _IMPORTER_ANCHOR_KINDS
        ],
    }
    changed_identities = {item.result_identity for item in changed.findings}
    warnings = set(changed.warnings)
    omitted = changed.omitted_count
    truncated = changed.truncated
    status, next_action = changed.status.value, changed.next_safe_action
    candidates: dict[str, dict[str, object]] = {}
    for direction in ("callers", "importers"):
        for anchor in anchors[direction]:
            answer = related((anchor.result_identity,), direction)
            warnings |= set(answer.warnings)
            omitted += answer.omitted_count
            truncated = truncated or answer.truncated
            if answer.status.value != "ready" and status == "ready":
                status, next_action = "partial", answer.next_safe_action
            for edge in answer.findings:
                if edge.result_identity in changed_identities:
                    continue  # a changed symbol is never its own candidate
                evidence = None if edge.edge_evidence is None else edge.edge_evidence.value
                if evidence is None or (evidence == "inferred" and not allow_inferred):
                    continue  # the broker never widens the engine's evidence
                candidate = candidates.setdefault(
                    edge.result_identity, {"finding": edge, "anchors": {}}
                )
                kept = candidate["anchors"]
                assert isinstance(kept, dict)
                entry = _anchor_entry(anchor, edge)
                previous = kept.get(anchor.result_identity)
                if previous is None or _evidence_tier(entry["edge_evidence"]) < _evidence_tier(
                    previous["edge_evidence"]
                ):
                    # One entry per changed symbol, keeping its strongest edge.
                    kept[anchor.result_identity] = entry
    findings: list[dict[str, object]] = []
    for candidate in candidates.values():
        finding = candidate["finding"]
        assert isinstance(finding, Level1Finding)
        kept = candidate["anchors"]
        assert isinstance(kept, dict)
        attribution = sorted(
            kept.values(),
            key=lambda item: (
                _evidence_tier(item["edge_evidence"]),
                item["path"],
                item["qualified_name"],
                item["result_identity"],
            ),
        )
        strongest = attribution[0]
        entry = finding.to_dict("2")
        entry.update(
            {
                "relation": strongest["relation"],
                "edge_evidence": strongest["edge_evidence"],
                "reference_line": strongest["reference_line"],
                "reference_count": strongest["reference_count"],
                "anchors": attribution,
            }
        )
        findings.append(entry)
    findings.sort(
        key=lambda item: (
            _evidence_tier(item["edge_evidence"]),
            -len(item["anchors"]),
            item["path"],
            item["start_line"],
            item["result_identity"],
        )
    )
    if len(findings) > maximum_results:
        omitted += len(findings) - maximum_results
        truncated = True
        findings = findings[:maximum_results]
    for rank, item in enumerate(findings, start=1):
        item["rank"] = rank
    # Decision 1 of the 2.7.1 anchor-order fix: an anchor of a returned
    # candidate sorts ahead of the rest of the changed set, so a later budget
    # trim never has to look past the compact form to tell one from the
    # other (decision 2 relies on this order).
    anchor_counts = _anchor_counts(findings)
    changed_entries = sorted(
        (_changed_entry(item) for item in changed.findings),
        key=lambda entry: _changed_sort_key(entry, anchor_counts),
    )
    return {
        "schema_version": "1",
        "mode": "query",
        "operation": "impact-candidates",
        "status": status,
        "freshness": changed.freshness.value,
        "index_identity": changed.index_identity,
        "changed": changed_entries,
        "changed_count": len(changed.findings),
        # The changed set's own omissions, so a reader can tell them from the
        # candidates `maximum_results` dropped; `omitted_count` keeps counting
        # both together.
        "changed_omitted_count": changed.omitted_count,
        # What the output budget took off the tail of the changed list, which
        # `trim_to_budget` fills in; a composed answer has trimmed nothing yet.
        "changed_trimmed_count": 0,
        "findings": findings,
        "returned_count": len(findings),
        "omitted_count": omitted,
        "truncated": truncated,
        "output_characters": 0,
        "warnings": sorted(warnings),
        "required_authorizations": [],
        "next_safe_action": next_action,
    }


def _canonical_length(value: object) -> int:
    """The canonical serialized length of a whole result or of one layer."""
    return len(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def _output_characters(value: dict[str, object]) -> int:
    """The serialized length of the object, counting the field that reports it."""
    length = 0
    for _attempt in range(8):
        measured = _canonical_length(dict(value, output_characters=length))
        if measured == length:
            return length
        length = measured
    return length


def _add_warning(trimmed: dict[str, object], warning: str) -> None:
    """Record one trimming warning, keeping the sorted union the result promises."""
    warnings = trimmed.get("warnings", [])
    assert isinstance(warnings, list)
    trimmed["warnings"] = sorted(set(warnings) | {warning})


def _compact_changed_entry(entry: dict[str, object]) -> dict[str, object]:
    """One changed symbol at its cheapest still-usable shape.

    The line range and the record kind are what a reader can ask
    `changed-symbols` for again, and the identity is what `changed-symbols`
    and every candidate's `anchors` already carry, so only the path and the
    qualified name stay - what makes the entry readable at all.
    """
    return {key: entry[key] for key in CHANGED_COMPACT_KEYS}


# One entry the tail trim popped, remembered so give-back can restore it: the
# entry in whatever shape it was dropped (compact once the list was
# compacted), paired with the `result_identity` `identities` carried for it.
_DroppedChangedEntry = tuple[dict[str, object], str]


def _drop_changed_tail(
    result: dict[str, object],
    changed: list[dict[str, object]],
    identities: list[str],
    dropped: list[_DroppedChangedEntry],
) -> None:
    """Drop the last changed entry, counting it and warning about it once.

    The drop is counted in `changed_trimmed_count` rather than in
    `omitted_count`, which counts what the engine left out and the candidates
    the budget dropped; a reader adding `changed_trimmed_count` to the length
    of the list gets back what the engine returned. `identities` is the
    parallel list of `result_identity` values `changed` no longer carries once
    it is compact, kept so a later call can still tell an anchor entry from
    the rest; it is popped in lockstep so the two lists never drift apart.
    `dropped` remembers the popped entry and its identity, in the order they
    are dropped, so give-back can restore them afterwards in the reverse
    order - the one dropped last (closest to the front of the retained order)
    restored first.
    """
    entry = changed.pop()
    identity = identities.pop()
    dropped.append((entry, identity))
    result["changed"] = changed
    result["changed_trimmed_count"] = int(result["changed_trimmed_count"]) + 1
    _add_warning(result, WARNING_CHANGED_LIST_TRIMMED)


def _drop_unprotected_changed_tail(
    result: dict[str, object],
    changed: list[dict[str, object]],
    identities: list[str],
    protected: set[str],
    dropped: list[_DroppedChangedEntry],
) -> bool:
    """Drop the last changed entry unless it anchors a returned candidate.

    Decision 2 of the 2.7.1 anchor-order fix: the tail trim never removes an
    entry that anchors a returned candidate, even when the object stays over
    budget because of it. Decision 1 already sorts every such entry ahead of
    the rest, so once the tail reaches one of them there is nothing further
    to trim here; the caller stops instead of skipping over it. Returns
    whether an entry was actually dropped.
    """
    if not changed or identities[-1] in protected:
        return False
    _drop_changed_tail(result, changed, identities, dropped)
    return True


def _fit_changed_to_share(
    result: dict[str, object],
    identities: list[str],
    share: int,
    dropped: list[_DroppedChangedEntry],
) -> None:
    """Fit the changed layer into its share of the budget, cheapest loss first.

    This only runs once the whole object is already over budget, and the
    whole list takes the compact shape right away - not only when the list
    itself is over its share - because losing entry detail is cheaper than
    losing a candidate: an answer only slightly over budget should not pay
    for it with a dropped candidate when compacting the changed list is
    enough on its own. Only a compact list still over its share loses entries
    from the tail, and never one that anchors a returned candidate (decision
    2 of the 2.7.1 anchor-order fix): if the anchors alone still exceed the
    share, they are kept anyway - they are already compact - and the object
    carries the overage into the later steps instead. This share is only ever
    a ceiling on how much the changed layer loses here; 2.7.2 gives back
    whatever of it the whole object never actually needed once every step is
    settled (`_give_back_changed_budget`).
    """
    changed = result["changed"]
    assert isinstance(changed, list)
    if not changed:
        return
    changed = [_compact_changed_entry(entry) for entry in changed]
    result["changed"] = changed
    protected = _anchor_identities(result["findings"])  # type: ignore[arg-type]
    while changed and _canonical_length(changed) > share:
        if not _drop_unprotected_changed_tail(result, changed, identities, protected, dropped):
            break


def _give_back_changed_budget(
    result: dict[str, object],
    changed: list[dict[str, object]],
    identities: list[str],
    dropped: list[_DroppedChangedEntry],
    maximum_output_characters: int,
) -> None:
    """Restore trimmed `changed` entries while the whole object still fits.

    The two drop phases above trimmed the changed layer to a share of the
    budget, and separately to make room for what the candidates alone could
    not give up - both ceilings on the changed layer in isolation, neither
    aware of whether the object as a whole, once everything else settled,
    actually needed all of it. `dropped` is the stack both phases pushed onto
    as they popped from the tail, in the order they popped; the entry on top
    is always the one dropped last, which was the one closest to the front of
    the retained order and so the next one worth having back. Restoring it -
    appending it back onto `changed` and remeasuring - and continuing while
    the object still fits `maximum_output_characters` rebuilds exactly the
    order the list had before anything was dropped, one entry at a time,
    cheapest give-back first. A restored entry keeps whatever shape it was
    dropped in, so it stays compact when the list was compacted. Candidates
    are never touched here - only what the changed layer itself did not need
    to spend is given back to it - and the loop stops the moment one more
    entry would not fit, leaving the rest counted in `changed_trimmed_count`
    and named by the `changed-list-trimmed` warning.
    """
    while dropped:
        entry, identity = dropped[-1]
        changed.append(entry)
        identities.append(identity)
        result["changed"] = changed
        # `changed_trimmed_count` and the warning it can retire both belong to
        # the tentative restored state, so they are updated before measuring -
        # not after - or the length just measured would not be the length this
        # object actually has once the restore is kept.
        previous_trimmed_count = int(result["changed_trimmed_count"])
        result["changed_trimmed_count"] = previous_trimmed_count - 1
        previous_warnings = result.get("warnings", [])
        assert isinstance(previous_warnings, list)
        retired_warning = False
        if result["changed_trimmed_count"] == 0 and WARNING_CHANGED_LIST_TRIMMED in previous_warnings:
            retired_warning = True
            result["warnings"] = [
                warning for warning in previous_warnings if warning != WARNING_CHANGED_LIST_TRIMMED
            ]
        measured = _output_characters(result)
        if measured > maximum_output_characters:
            changed.pop()
            identities.pop()
            result["changed"] = changed
            result["changed_trimmed_count"] = previous_trimmed_count
            if retired_warning:
                result["warnings"] = previous_warnings
            return
        dropped.pop()
        result["output_characters"] = measured


def trim_to_budget(
    result: dict[str, object], maximum_output_characters: int
) -> dict[str, object]:
    """Fit a composed result into its output budget, both layers sharing it.

    The change set and the candidates are both what this operation is asked
    for, so neither pays for the other, and the cheapest loss goes first: an
    object over budget compacts every changed entry right away, whether or
    not the list itself is over its third of the budget - losing entry detail
    is cheaper than losing a candidate. Only a compact list still over its
    share drops entries from the tail, counted in `changed_trimmed_count`
    with the warning `changed-list-trimmed` - except an entry that anchors a
    returned candidate, which the tail trim never removes (decision 2 of the
    2.7.1 anchor-order fix); if the anchors alone still exceed the share they
    are kept anyway and the object carries the overage forward. Whatever the
    changed layer did not spend is the candidates', and they drop from the
    tail - strongest evidence surviving longest - until the whole object
    fits; the anchors of a kept candidate are never trimmed, so a changed
    symbol trimmed off the list is still named by every candidate that
    depends on it. Dropping a candidate can free the changed entries that
    anchored only it, so the anchor set is recomputed before the changed
    layer is asked to give up its own protected entries too - the only step
    that still runs once every candidate is gone. Only when no candidate and
    no changed entry is left to give does `output-budget-exceeded` report the
    overrun instead of trimming forever. Once every step above has settled,
    2.7.2 gives back whatever of the changed layer's own losses the object
    never actually needed: the entries the two drop phases popped are
    restored one at a time, in the order they were retained, for as long as
    the whole object still fits - a candidate is never dropped to make room
    for this, and the give-back never spends more than what the object was
    already under budget by. `changed_count` keeps counting the changed
    findings the engine returned, and `output_characters` is always the final
    canonical length.
    """
    trimmed = dict(result)
    trimmed["output_characters"] = _output_characters(trimmed)
    if int(trimmed["output_characters"]) <= maximum_output_characters:
        return trimmed
    changed = list(trimmed["changed"])  # type: ignore[arg-type]
    trimmed["changed"] = changed
    changed_identities = [str(entry["result_identity"]) for entry in changed]
    dropped: list[_DroppedChangedEntry] = []
    _fit_changed_to_share(
        trimmed,
        changed_identities,
        maximum_output_characters // CHANGED_BUDGET_SHARES,
        dropped,
    )
    trimmed["output_characters"] = _output_characters(trimmed)
    trimmed = _drop_findings_to_budget(trimmed, maximum_output_characters)
    changed = trimmed["changed"]  # type: ignore[assignment]
    assert isinstance(changed, list)
    # Every candidate the budget was ever going to drop is gone by now, so the
    # anchor set below is the final one this answer will ever have.
    protected = _anchor_identities(trimmed["findings"])  # type: ignore[arg-type]
    while changed and int(trimmed["output_characters"]) > maximum_output_characters:
        # The candidates are gone and the object is still over the budget, so
        # the changed layer gives up its share too rather than overrun.
        if not _drop_unprotected_changed_tail(
            trimmed, changed, changed_identities, protected, dropped
        ):
            break
        trimmed["output_characters"] = _output_characters(trimmed)
    _give_back_changed_budget(
        trimmed, changed, changed_identities, dropped, maximum_output_characters
    )
    if int(trimmed["output_characters"]) > maximum_output_characters:
        # Nothing left to lose: the envelope alone is over the budget, and the
        # reader is told rather than handed a silent overrun.
        _add_warning(trimmed, WARNING_OUTPUT_BUDGET_EXCEEDED)
        trimmed["output_characters"] = _output_characters(trimmed)
    return trimmed


def _drop_findings_to_budget(
    result: dict[str, object], maximum_output_characters: int
) -> dict[str, object]:
    """Drop findings from the tail, cheapest loss first, until the object fits.

    Popping from the tail leaves every surviving finding's rank exactly as it
    was, so ranks stay contiguous from 1 without renumbering.
    `returned_count`/`omitted_count` and `truncated` are updated to match each
    drop, and `output_characters` is remeasured after every one.
    """
    findings = list(result["findings"])  # type: ignore[arg-type]
    while findings and int(result["output_characters"]) > maximum_output_characters:
        findings.pop()
        result["findings"] = findings
        result["returned_count"] = len(findings)
        result["omitted_count"] = int(result["omitted_count"]) + 1
        result["truncated"] = True
        result["output_characters"] = _output_characters(result)
    return result


def _merge_overview_languages(
    *lists: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Sum the language counts of several rows, most files first, ties by name.

    That is the order a single group's list already promises, so the merged
    row reads exactly like every other row of the table. The merged list is
    bounded the way every language list is - a fold joining more languages
    than a result may carry keeps the ones most of its files are written in -
    matching the Go engine's own fold (`mergeOverviewLanguages`,
    `internal/render/render.go`).
    """
    totals: dict[str, int] = {}
    for languages in lists:
        for item in languages:
            name = str(item["language"])
            totals[name] = totals.get(name, 0) + int(item["file_count"])
    merged = [
        {"language": name, "file_count": count}
        for name, count in sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    ]
    return merged[: max_collection_items()]


def _overview_real_rows(groups: list[dict[str, object]]) -> int:
    """How many rows still name a directory, so all of them but `*`."""
    if groups and groups[-1]["path_prefix"] == OVERVIEW_OTHER_PREFIX:
        return len(groups) - 1
    return len(groups)


def _overview_rows_folded_to(
    groups: list[dict[str, object]], keep: int
) -> tuple[list[dict[str, object]], int] | None:
    """The table with `keep` directory rows left, and how many rows it lost.

    The folded row speaks for every directory the answer had no room for, so
    there is exactly one of it and it is always last: an engine that folded
    rows of its own is merged into, never doubled, and the merged row keeps
    the nine keys the wire names - depth 0 and no representative file, because
    it stands for whole directories rather than one of them. Folding several
    rows at once sums and merges exactly what folding them one at a time would,
    which is what lets the search below skip the intermediate tables. One
    directory row is the floor - a table of nothing but `*` would describe no
    repository at all - and `None` reports a table that is already at or below
    `keep`. The rows handed in are read, never written: the fold builds new
    ones.
    """
    rows = list(groups)
    folded = (
        rows.pop()
        if rows and rows[-1]["path_prefix"] == OVERVIEW_OTHER_PREFIX
        else None
    )
    if keep < 1 or len(rows) <= keep:
        return None
    tail = rows[keep:]
    merged: dict[str, object] = {
        "path_prefix": OVERVIEW_OTHER_PREFIX,
        "depth": 0,
        "languages": _merge_overview_languages(
            *(list(row["languages"]) for row in tail),
            list(folded["languages"]) if folded else [],
        ),
        "representative_identity": None,
    }
    for counter in _OVERVIEW_COUNTERS:
        merged[counter] = sum(int(row[counter]) for row in tail) + (
            int(folded[counter]) if folded else 0
        )
    return rows[:keep] + [merged], len(tail)


def _apply_overview_fold(
    result: dict[str, object], folded: tuple[list[dict[str, object]], int] | None
) -> None:
    """Write a fold back, counting every directory row the table gave up.

    `other_group_count` counts them all, so a reader can still add the whole
    repository up. The answer handed in is left alone; the fold rebuilds what
    it changes, and a fold of `None` changes nothing.
    """
    overview = result.get("overview")
    if folded is None or not isinstance(overview, dict):
        return
    rows, lost = folded
    result["groups"] = rows
    result["overview"] = dict(
        overview, other_group_count=int(overview["other_group_count"]) + lost
    )


def _fold_overview_table_to_share(
    result: dict[str, object], table_budget: int
) -> None:
    """Fold the table's tail until the table layer fits its share of the budget.

    The widest table that fits is found by binary search over the serialized
    length of the folded table - about `log2(rows)` serializations rather than
    one per row given up, which is what keeps a table at the wire's four
    thousand row bound from costing a minute of the caller's query. The length
    grows with the row count, so the search lands on the same table folding one
    row at a time would have left; the two single-step walks afterwards are
    what makes that true rather than assumed, and on a real table they take no
    steps at all. Only `groups` and `overview` are measured: the file layer has
    its own share and must not push the table below what the table was given.
    """
    groups = list(result.get("groups") or [])
    overview = result.get("overview")
    widest = _overview_real_rows(groups)
    if not isinstance(overview, dict) or widest < 2:
        return
    measured: dict[int, int] = {}

    def length(keep: int) -> int:
        if keep not in measured:
            folded = _overview_rows_folded_to(groups, keep)
            rows, lost = (groups, 0) if folded is None else folded
            measured[keep] = _canonical_length(
                {
                    "groups": rows,
                    "overview": dict(
                        overview,
                        other_group_count=int(overview["other_group_count"]) + lost,
                    ),
                }
            )
        return measured[keep]

    low, high, keep = 1, widest, 1
    while low <= high:
        middle = (low + high) // 2
        if length(middle) <= table_budget:
            keep, low = middle, middle + 1
        else:
            high = middle - 1
    while keep < widest and length(keep + 1) <= table_budget:
        keep += 1
    while keep > 1 and length(keep) > table_budget:
        keep -= 1
    _apply_overview_fold(result, _overview_rows_folded_to(groups, keep))


def fit_overview_to_budget(
    summary: dict[str, object], maximum_output_characters: int
) -> dict[str, object]:
    """Fit an overview answer into its output budget, half of it to each layer.

    The directory table and the ranked file layer are both what this operation
    is asked for, so neither pays for the other: the table's share is half of
    the budget, and it folds its tail into `*` only for as long as it is over
    that half - the counts stay in the table, only the detail behind them goes
    - down to a single directory row. Everything the table did not spend is the
    file layer's, and the file layer drops from its tail until the whole answer
    fits. A table already inside its half is therefore never folded and the
    file layer gets all the rest, while a table wider than its half yields
    exactly what it is over. That is what makes a wider budget buy a wider
    table and more files at once. If a one-row table with no findings still
    does not fit, `output-budget-exceeded` says so instead of a silent
    overrun - and it can say nothing else, because an envelope small enough to
    leave a two-row table inside half the budget is small enough for that table
    to fit the whole of it. `output_characters` is always the final canonical
    length.
    """
    fitted = dict(summary)
    fitted["output_characters"] = _output_characters(fitted)
    if int(fitted["output_characters"]) <= maximum_output_characters:
        return fitted
    _fold_overview_table_to_share(
        fitted, maximum_output_characters // OVERVIEW_BUDGET_SHARES
    )
    fitted["output_characters"] = _output_characters(fitted)
    fitted = _drop_findings_to_budget(fitted, maximum_output_characters)
    if int(fitted["output_characters"]) > maximum_output_characters:
        _add_warning(fitted, WARNING_OUTPUT_BUDGET_EXCEEDED)
        fitted["output_characters"] = _output_characters(fitted)
    return fitted


def _resolve_change_base(repository: Path, requested: str | None, *, root: Path):
    try:
        return resolve_change_base(repository, requested, root=root)
    except ValueError as exc:
        raise PrepareCLIError("selected change base could not be resolved") from exc


def _run_change_query(
    transport: NativeTransport,
    repository: Path,
    state_root: Path,
    binding: Binding,
    snapshot: object,
    arguments: QueryArguments,
    refresh_summary: dict[str, object],
    state_home: Path,
) -> dict[str, object]:
    """Answer `changed-symbols` or `impact-candidates` for one resolved base."""
    # `collect_snapshot` (inside `_resolve_repository`) already ran
    # `git rev-parse --show-toplevel` once to produce `canonical_root`; reuse
    # it here instead of resolving the same root twice more (H2).
    root = Path(snapshot.canonical_root)
    if arguments.staged:
        # `_resolve_repository` already refused a repository with no commit
        # before any query runs, so the staged base never needs the
        # work-recovery priority: it is always HEAD itself.
        base = staged_change_base(repository, root=root)
    else:
        base = _resolve_change_base(repository, arguments.base, root=root)
    changed, warnings = changed_ranges(
        repository, base, snapshot, root=root, staged=arguments.staged
    )
    selector, guard_warnings = bound_changed_selector(changed)
    change_warnings = set(warnings) | set(guard_warnings)
    base_block = {
        "requested": base.requested,
        "ref": base.ref,
        "sha": base.sha,
        "source": base.source,
        "warning": base.warning,
    }
    filters = {
        "path_prefixes": arguments.path_prefixes,
        "languages": arguments.languages,
        "symbol_kinds": arguments.symbol_kinds,
        "source_types": arguments.source_types,
    }
    composing = arguments.operation == "impact-candidates"

    def changed_symbols(maximum_results: int, budget: int) -> Level1Result:
        return _invoke_native(
            transport,
            "changed-symbols",
            repository,
            state_root,
            snapshot,
            index_identity=binding.index_identity,
            filters=filters,
            maximum_results=maximum_results,
            maximum_output_characters=budget,
            allow_inferred=arguments.allow_inferred,
            changed_selector=selector,
        )

    if not composing:
        result = changed_symbols(
            arguments.maximum_results, arguments.maximum_output_characters
        )
        _require_usable(result)
        summary = _query_summary(
            result,
            _converge_retention_after_query(refresh_summary, state_home, state_root),
        )
        summary["base"] = base_block
        summary["warnings"] = sorted(set(summary["warnings"]) | change_warnings)
        return summary

    changed_result = changed_symbols(
        IMPACT_CHANGED_MAXIMUM_RESULTS, IMPACT_CHANGED_OUTPUT_CHARACTERS
    )
    _require_usable(changed_result)

    def related(anchors: tuple[str, ...], direction: str) -> Level1Result:
        return _invoke_native(
            transport,
            "related-symbols",
            repository,
            state_root,
            snapshot,
            index_identity=binding.index_identity,
            result_identities=anchors,
            direction=direction,
            maximum_results=IMPACT_CHANGED_MAXIMUM_RESULTS,
            maximum_output_characters=IMPACT_CHANGED_OUTPUT_CHARACTERS,
            allow_inferred=arguments.allow_inferred,
        )

    composed = compose_impact_candidates(
        changed_result,
        related,
        allow_inferred=arguments.allow_inferred,
        maximum_results=arguments.maximum_results,
    )
    # Converge retention before the budget fit below: `trim_to_budget`
    # remeasures `output_characters` over this whole object, so
    # `pruned_generation_count` has to already be in `refresh` for that count
    # to be honest.
    refresh_block = dict(
        _converge_retention_after_query(refresh_summary, state_home, state_root)
    )
    prune_warnings = refresh_block.pop("warnings", [])
    composed["base"] = base_block
    composed["refresh"] = refresh_block
    composed["warnings"] = sorted(
        set(composed["warnings"]) | change_warnings | set(prune_warnings)
    )
    return trim_to_budget(composed, arguments.maximum_output_characters)


def _require_usable(result: Level1Result) -> None:
    """Refuse a change query the index cannot answer, as a direct query does.

    A change query names no result identity, so the identity refusal of
    `run_query` cannot apply; a stale index is the only refusal left.
    """
    if result.status.value not in {"ready", "partial"}:
        raise PrepareCLIError("ready context is required; run prepare inspect")


def run_query(
    repository: Path,
    arguments: QueryArguments,
    *,
    environment: Mapping[str, str],
    transport_for: TransportFactory,
) -> dict[str, object]:
    """Answer one read-only query, refreshing a stale bound index first."""
    repository, snapshot, paths, state_root, binding_path = _resolve_repository(
        repository, environment
    )
    binary, _source = _resolve_native_binary(environment, paths.root)
    if binary is None:
        raise PrepareCLIError("ready context is required; run prepare inspect")
    binding = _read_binding(binding_path, snapshot)
    if binding is None:
        raise PrepareCLIError("ready context is required; run prepare inspect")
    transport = transport_for(binary)
    binding, refresh_summary = _refresh_if_stale(
        transport, repository, state_root, binding_path, binding, snapshot, paths.root
    )
    if arguments.operation in CHANGE_QUERY_OPERATIONS:
        # A change query resolves its own base and sends the changed line
        # ranges; the composed one then follows every changed symbol's
        # relationships. Both refuse an unusable index exactly as the direct
        # operations below do.
        summary = _run_change_query(
            transport, repository, state_root, binding, snapshot, arguments, refresh_summary,
            paths.root,
        )
        touch_binding(binding_path)
        return summary
    # The engine evaluates the binding's freshness on every query, so the
    # query result carries exactly what a separate status call would have
    # reported. One native call per query, plus the one `update` call the
    # refresh above already made when the binding was stale.
    result = _invoke_native(
        transport,
        arguments.operation,
        repository,
        state_root,
        snapshot,
        index_identity=binding.index_identity,
        query=arguments.query,
        result_identities=arguments.result_identities,
        direction=arguments.direction,
        filters={
            "path_prefixes": arguments.path_prefixes,
            "languages": arguments.languages,
            "symbol_kinds": arguments.symbol_kinds,
            "source_types": arguments.source_types,
        },
        maximum_results=arguments.maximum_results,
        maximum_output_characters=(
            OVERVIEW_ENGINE_OUTPUT_CHARACTERS
            if arguments.operation == OVERVIEW_QUERY_OPERATION
            else arguments.maximum_output_characters
        ),
        allow_inferred=arguments.allow_inferred,
    )
    if result.status.value not in {"ready", "partial"}:
        # Two different refusals arrive as `stale`: the index is genuinely
        # stale, or an otherwise exact index cannot use the requested result
        # identities - source-snippets cannot verify them (unknown id, or a
        # record whose evidence class is not Verified), and related-symbols
        # cannot anchor a relationship on them. `next_safe_action` tells them
        # apart: only the identity case answers `update-index`, while every
        # genuinely stale query path answers `rebuild-index`. Name the
        # identity case honestly, and leave a stale index pointing at
        # `prepare inspect`, which is the step that actually helps.
        if (
            arguments.operation in IDENTITY_QUERY_OPERATIONS
            and result.status.value == "stale"
            and result.next_safe_action == "update-index"
        ):
            raise PrepareCLIError(
                "result identities could not be verified against the current index; re-run the search query"
            )
        raise PrepareCLIError("ready context is required; run prepare inspect")
    touch_binding(binding_path)
    # Converge retention after this query's own engine call too, when it did
    # not itself refresh: a repository that is only ever queried still
    # reaches one referenced generation once the grace period passes. This
    # runs before `_query_summary` so an overview's later budget fit measures
    # `pruned_generation_count` as part of the object from the start.
    summary = _query_summary(
        result, _converge_retention_after_query(refresh_summary, paths.root, state_root)
    )
    if arguments.operation == OVERVIEW_QUERY_OPERATION:
        # The engine returns the whole ordered table and the widest file layer
        # it was asked for; the caller's own output budget is what sizes both.
        # The broker folds the table's tail into the `*` row until the table
        # is inside its half of that budget, drops findings until the whole
        # answer fits, and reports the canonical length of what it sends.
        return fit_overview_to_budget(summary, arguments.maximum_output_characters)
    return summary


def run_build(
    repository: Path,
    *,
    environment: Mapping[str, str],
    transport_for: TransportFactory,
    mode: str = "build",
    installer: Installer | None = None,
) -> dict[str, object]:
    """Build a fresh index; ``installer`` (CLI ``activate`` only) may install the engine first."""
    repository, snapshot, paths, state_root, binding_path = _resolve_repository(
        repository, environment
    )
    binary, binary_source = _resolve_native_binary(environment, paths.root)
    if binary is None and installer is not None:
        binary = installer(environment, paths.root)
        binary_source = "managed"
    if binary is None:
        raise PrepareCLIError("native engine is unavailable")
    # A generation this runtime cannot read is removed before the build, under
    # the state-write authorization the build itself already required: the
    # engine can neither answer from such a generation nor publish over it, and
    # it refuses without a warning of its own. The version it was written by is
    # kept for the summary, and for the refusal below when the build still
    # fails.
    replaced = incompatible_generation_version(state_root)
    warnings: tuple[str, ...] = ()
    if replaced is not None:
        warnings = ("incompatible-generation",)
        _remove_state_entry(paths.root, snapshot)
    result = _invoke_native(
        transport_for(binary), "build", repository, state_root, snapshot, index_identity=None
    )
    if (
        result.status.value not in {"ready", "partial"}
        or result.index_identity is None
        or result.next_safe_action != "use-index"
    ):
        if replaced is not None:
            # The state explains the refusal, so answer it instead of hiding
            # it behind an error string: the removed generation is named, and
            # `rebuild-index` is the step that now has a clean state to run in.
            return _summary(
                mode=mode,
                snapshot=snapshot,
                binary=binary,
                binary_source=binary_source,
                result=result,
                estimate=result,
                authorizations=("state-write",),
                state=summarize_state(paths.root),
                replaced_generation_version=replaced,
                extra_warnings=warnings,
            )
        raise PrepareCLIError(
            "native context build did not become ready "
            f"(engine status: {result.status.value})"
        )
    _write_binding(binding_path, snapshot, result.index_identity)
    # Converge retention here too: a successful build (or activate, which
    # shares this function) is the other moment - besides a refresh - a
    # repository's superseded generations can be cleared once they have aged
    # past the grace period.
    pruned_count, prune_warnings = _run_convergence_prune(paths.root, state_root)
    refresh_block: dict[str, object] = {
        "performed": False,
        "changed_path_count": 0,
        "duration_ms": 0,
        "pruned_generation_count": pruned_count,
    }
    if prune_warnings:
        refresh_block["warnings"] = prune_warnings
    return _summary(
        mode=mode,
        snapshot=snapshot,
        binary=binary,
        binary_source=binary_source,
        result=result,
        estimate=result,
        authorizations=(),
        state=summarize_state(paths.root),
        replaced_generation_version=replaced,
        extra_warnings=warnings,
        refresh=refresh_block,
    )


def run_inspect(
    repository: Path,
    *,
    environment: Mapping[str, str],
    transport_for: TransportFactory,
) -> dict[str, object]:
    """Report engine availability, freshness, and the next safe action; read-only except a refresh."""
    repository, snapshot, paths, state_root, binding_path = _resolve_repository(
        repository, environment
    )
    binary, binary_source = _resolve_native_binary(environment, paths.root)
    binding = _read_binding(binding_path, snapshot)
    status_result: Level1Result | None = None
    estimate_result: Level1Result | None = None
    refresh_summary: dict[str, object] | None = None
    if binary is not None:
        transport = transport_for(binary)
        if binding is not None:
            try:
                binding, refresh_summary = _refresh_if_stale(
                    transport, repository, state_root, binding_path, binding, snapshot, paths.root
                )
            except PrepareCLIError:
                # Inspect never fails because of a refresh refusal; it falls
                # back to today's status/estimate reporting (rebuild-index).
                refresh_summary = None
            status_result = _invoke_native(
                transport,
                "status",
                repository,
                state_root,
                snapshot,
                index_identity=binding.index_identity,
            )
            if status_result.next_safe_action == "use-index":
                touch_binding(binding_path)
        if status_result is None or status_result.next_safe_action != "use-index":
            estimate_result = _invoke_native(
                transport, "estimate", repository, state_root, snapshot, index_identity=None
            )
    result = status_result or estimate_result
    if binary is None:
        authorizations: tuple[str, ...] = ("network", "state-write")
    elif result is not None and result.next_safe_action == "use-index":
        authorizations = ()
    else:
        authorizations = ("state-write",)
    return _summary(
        mode="inspect",
        snapshot=snapshot,
        binary=binary,
        binary_source=binary_source,
        result=result,
        estimate=estimate_result or status_result,
        authorizations=authorizations,
        state=summarize_state(paths.root),
        refresh=refresh_summary,
    )
