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
from .git_snapshot import collect_snapshot
from .level1_models import Level1Result, parse_level1_result
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
    CURRENT_RUNTIME_VERSION,
    apply_plan,
    current_generation_token,
    plan_prune_generations,
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
    request_material = "\0".join(material).encode("utf-8")
    request_identity = "taf.prepare." + hashlib.sha256(request_material).hexdigest()[:24]
    # Schema 2 exists for the one operation that carries a direction; every
    # other request stays byte-identical to the frozen schema-1 envelope.
    schema_version = "2" if direction is not None else "1"
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
    if schema_version == "2":
        envelope["request"]["direction"] = direction
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
                prune_warnings = _prune_generations(state_home, binding_path.parent, protected_generation)
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


def _prune_generations(state_home: Path, entry: Path, protected: str | None = None) -> list[str]:
    """Delete generations no reader can still be using; never raise.

    `protected` names the generation CURRENT pointed at immediately before
    this refresh's `update` call; it is excluded from the plan even if its
    mtime alone would otherwise look aged and unreferenced.
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
        apply_plan(root, plan)
    except (StateError, OSError, ValueError):
        return ["retention-prune-incomplete"]
    return []


def _query_summary(
    result: Level1Result, refresh: dict[str, object] | None = None
) -> dict[str, object]:
    refresh = dict(refresh) if refresh else {"performed": False, "changed_path_count": 0, "duration_ms": 0}
    prune_warnings = refresh.pop("warnings", [])
    warnings = list(result.warnings)
    if prune_warnings:
        warnings = sorted(set(warnings) | set(prune_warnings))
    return {
        "schema_version": "1",
        "mode": "query",
        "operation": result.operation.value,
        "status": result.status.value,
        "freshness": result.freshness.value,
        "index_identity": result.index_identity,
        # The summary's own schema is unchanged; a relationship result simply
        # carries the four extra schema-2 keys on each finding.
        "findings": [item.to_dict(result.schema_version) for item in result.findings],
        "returned_count": result.returned_count,
        "omitted_count": result.omitted_count,
        "truncated": result.truncated,
        "output_characters": result.output_characters,
        "warnings": warnings,
        "required_authorizations": [],
        "next_safe_action": result.next_safe_action,
        "refresh": refresh,
    }


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
        "warnings": sorted(set(() if result is None else result.warnings) | set(prune_warnings)),
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


QUERY_DIRECTIONS = ("callers", "callees", "importers", "imports")
# The query operations that name records instead of searching for them.
IDENTITY_QUERY_OPERATIONS = ("source-snippets", "related-symbols")
# One relationship request stays cheap to resolve, so it carries few anchors.
MAXIMUM_RELATED_ANCHORS = 16


def validate_query_request(
    operation: str,
    query: str | None,
    result_identities: tuple[str, ...],
    direction: str | None = None,
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
        maximum_output_characters=arguments.maximum_output_characters,
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
    return _query_summary(result, refresh_summary)


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
    result = _invoke_native(
        transport_for(binary), "build", repository, state_root, snapshot, index_identity=None
    )
    if (
        result.status.value not in {"ready", "partial"}
        or result.index_identity is None
        or result.next_safe_action != "use-index"
    ):
        raise PrepareCLIError("native context build did not become ready")
    _write_binding(binding_path, snapshot, result.index_identity)
    return _summary(
        mode=mode,
        snapshot=snapshot,
        binary=binary,
        binary_source=binary_source,
        result=result,
        estimate=result,
        authorizations=(),
        state=summarize_state(paths.root),
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
