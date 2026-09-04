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
)
from .git_snapshot import collect_snapshot
from .level1_models import Level1Finding, Level1Result, parse_level1_result
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
    # Schema 2 exists for the one operation that carries a direction and
    # schema 3 for the one that carries a change selector; every other request
    # stays byte-identical to the frozen schema-1 envelope.
    schema_version = "3" if selector is not None else "2" if direction is not None else "1"
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
    if schema_version in {"2", "3"}:
        # Schema 3 spells both added keys out, direction included, because the
        # engine requires the whole key set of the schema it is given.
        envelope["request"]["direction"] = direction
    if schema_version == "3":
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
    base: str | None = None


QUERY_DIRECTIONS = ("callers", "callees", "importers", "imports")
# The query operations that name records instead of searching for them.
IDENTITY_QUERY_OPERATIONS = ("source-snippets", "related-symbols")
# One relationship request stays cheap to resolve, so it carries few anchors.
MAXIMUM_RELATED_ANCHORS = 16
# The two operations that answer from a Git difference instead of from a query
# string or a result identity, and the only ones that accept a base.
CHANGE_QUERY_OPERATIONS = ("changed-symbols", "impact-candidates")
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
# The engine refuses a request frame above 256 KiB (wire.MaximumWireBytes).
MAXIMUM_REQUEST_BYTES = 256 * 1024
# Everything in the frame but the selector: the two absolute roots, the
# identities, the budgets, and the filters. Only the filters can grow, and the
# wire caps them at 64 path prefixes of 512 characters, so 48 KiB covers the
# worst frame the engine would still accept.
_SELECTOR_RESERVE_BYTES = 48 * 1024
MAXIMUM_CHANGED_SELECTOR_BYTES = MAXIMUM_REQUEST_BYTES - _SELECTOR_RESERVE_BYTES


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
    return {
        "schema_version": "1",
        "mode": "query",
        "operation": "impact-candidates",
        "status": status,
        "freshness": changed.freshness.value,
        "index_identity": changed.index_identity,
        "changed": [_changed_entry(item) for item in changed.findings],
        "changed_count": len(changed.findings),
        # The changed set's own omissions, so a reader can tell them from the
        # candidates `maximum_results` dropped; `omitted_count` keeps counting
        # both together.
        "changed_omitted_count": changed.omitted_count,
        "findings": findings,
        "returned_count": len(findings),
        "omitted_count": omitted,
        "truncated": truncated,
        "output_characters": 0,
        "warnings": sorted(warnings),
        "required_authorizations": [],
        "next_safe_action": next_action,
    }


def _canonical_length(value: dict[str, object]) -> int:
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


def trim_to_budget(
    result: dict[str, object], maximum_output_characters: int
) -> dict[str, object]:
    """Fit a composed result into its output budget, cheapest loss first.

    The candidates are the answer, and each one already carries its anchors'
    path and qualified name, so the changed list is the context that shrinks
    first: it becomes identity-only, then loses entries from the tail with
    `changed-list-trimmed`, and only then do candidates go from the tail so
    that the strongest evidence survives longest. `changed_count` keeps
    counting the changed findings the engine returned, and the anchors of a
    kept candidate are never trimmed. A budget that not even an empty answer
    fits in is reported with `output-budget-exceeded` instead of trimming
    forever.
    """
    trimmed = dict(result)
    trimmed["output_characters"] = _output_characters(trimmed)
    if int(trimmed["output_characters"]) <= maximum_output_characters:
        return trimmed
    changed = trimmed["changed"]
    assert isinstance(changed, list)
    identities = [{"result_identity": item["result_identity"]} for item in changed]
    trimmed["changed"] = identities
    trimmed["output_characters"] = _output_characters(trimmed)
    dropped_changed = False
    while identities and int(trimmed["output_characters"]) > maximum_output_characters:
        identities.pop()
        if not dropped_changed:
            dropped_changed = True
            _add_warning(trimmed, WARNING_CHANGED_LIST_TRIMMED)
        trimmed["output_characters"] = _output_characters(trimmed)
    findings = list(trimmed["findings"])  # type: ignore[arg-type]
    while findings and int(trimmed["output_characters"]) > maximum_output_characters:
        findings.pop()
        trimmed["findings"] = findings
        trimmed["returned_count"] = len(findings)
        trimmed["omitted_count"] = int(trimmed["omitted_count"]) + 1
        trimmed["truncated"] = True
        trimmed["output_characters"] = _output_characters(trimmed)
    if int(trimmed["output_characters"]) > maximum_output_characters:
        # Nothing left to lose: the envelope alone is over the budget, and the
        # reader is told rather than handed a silent overrun.
        _add_warning(trimmed, WARNING_OUTPUT_BUDGET_EXCEEDED)
        trimmed["output_characters"] = _output_characters(trimmed)
    return trimmed


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
) -> dict[str, object]:
    """Answer `changed-symbols` or `impact-candidates` for one resolved base."""
    # `collect_snapshot` (inside `_resolve_repository`) already ran
    # `git rev-parse --show-toplevel` once to produce `canonical_root`; reuse
    # it here instead of resolving the same root twice more (H2).
    root = Path(snapshot.canonical_root)
    base = _resolve_change_base(repository, arguments.base, root=root)
    changed, warnings = changed_ranges(repository, base, snapshot, root=root)
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
        summary = _query_summary(result, refresh_summary)
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
    refresh_block = dict(refresh_summary)
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
            transport, repository, state_root, binding, snapshot, arguments, refresh_summary
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
