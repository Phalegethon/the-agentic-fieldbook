"""High-level, user-facing preparation of bounded repository context."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Mapping
from urllib import error as url_error
from urllib import parse as url_parse
from urllib import request as url_request

from .git_snapshot import collect_snapshot
from .level1_models import Level1Result, parse_level1_result
from .state_lifecycle import (
    CURRENT_RUNTIME_VERSION,
    Candidate,
    apply_plan,
    plan_gc,
    plan_remove,
    summarize_state,
    touch_binding,
)
from .state_paths import StateError, resolve_state_paths


_NATIVE_TIMEOUT_SECONDS = 120
_BINDING_LIMIT = 16 * 1024
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CHECKSUM = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9._-]+)\n\Z")
_NATIVE_ENGINE_VERSION = CURRENT_RUNTIME_VERSION
_TAF_RELEASE_VERSION = "2.1.2"
_NATIVE_RELEASE_BASE_URL = (
    "https://github.com/Phalegethon/the-agentic-fieldbook/releases/download/"
    f"v{_TAF_RELEASE_VERSION}"
)
_MAX_NATIVE_BINARY_BYTES = 64 * 1024 * 1024
_MAX_CHECKSUM_BYTES = 1024
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


def register_prepare_command(subparsers: argparse._SubParsersAction) -> None:
    """Register the high-level preparation command group."""
    prepare = subparsers.add_parser("prepare")
    commands = prepare.add_subparsers(dest="prepare_command", required=True)

    inspect = commands.add_parser("inspect")
    inspect.add_argument("--repo", required=True)

    build = commands.add_parser("build")
    build.add_argument("--repo", required=True)
    build.add_argument("--confirm-state-write", action="store_true")

    activate = commands.add_parser("activate")
    activate.add_argument("--repo", required=True)
    activate.add_argument("--confirm-network", action="store_true")
    activate.add_argument("--confirm-state-write", action="store_true")

    remove = commands.add_parser("remove")
    remove.add_argument("--repo", required=True)
    remove.add_argument("--confirm-state-write", action="store_true")

    gc = commands.add_parser("gc")
    gc.add_argument("--unused-for", type=int, default=30)
    gc.add_argument("--confirm-state-write", action="store_true")

    query = commands.add_parser("query")
    query.add_argument("--repo", required=True)
    query.add_argument(
        "--operation",
        required=True,
        choices=(
            "repository-map",
            "search-symbols",
            "search-docs",
            "source-snippets",
        ),
    )
    query.add_argument("--query")
    query.add_argument("--result-id", action="append", default=[])
    query.add_argument("--path-prefix", action="append", default=[])
    query.add_argument("--language", action="append", default=[])
    query.add_argument("--symbol-kind", action="append", default=[])
    query.add_argument(
        "--source-type",
        action="append",
        default=[],
        type=str.lower,
        choices=("source", "document", "configuration"),
    )
    query.add_argument("--maximum-results", type=int, choices=range(1, 65), default=8)
    query.add_argument(
        "--maximum-output-characters",
        type=int,
        choices=(2000, 4000, 8000, 12000),
        default=4000,
    )
    query.add_argument("--allow-inferred", action="store_true")


def run_prepare_command(
    args: argparse.Namespace,
    *,
    environment: Mapping[str, str],
    utc_clock: object,
) -> dict[str, object]:
    """Execute one already-parsed preparation command."""
    del utc_clock  # Reserved for a future persisted freshness timestamp.
    if args.prepare_command == "activate" and not args.confirm_network:
        raise PrepareCLIError("explicit network confirmation required")
    if args.prepare_command == "activate" and not args.confirm_state_write:
        raise PrepareCLIError("explicit state-write confirmation required")
    if args.prepare_command == "build" and not args.confirm_state_write:
        raise PrepareCLIError("explicit state-write confirmation required")

    if args.prepare_command == "gc":
        paths = _state_paths(environment)
        try:
            candidates = plan_gc(paths.root, unused_for_days=args.unused_for, now=time.time())
        except StateError as exc:
            raise PrepareCLIError(exc.code) from exc
        return _lifecycle_summary("gc", paths.root, candidates, confirmed=args.confirm_state_write)

    repository = Path(args.repo).resolve()
    snapshot = collect_snapshot(repository)
    if snapshot.head_sha is None:
        raise PrepareCLIError("repository must have at least one commit")

    paths = _state_paths(environment)
    state_root, binding_path = _repository_state_paths(
        paths.root, snapshot.repository_identity, snapshot.worktree_identity
    )

    if args.prepare_command == "remove":
        repository_key = snapshot.repository_identity.removeprefix("sha256:")
        worktree_key = snapshot.worktree_identity.removeprefix("sha256:")
        candidates = plan_remove(paths.root, repository_key, worktree_key)
        return _lifecycle_summary("remove", paths.root, candidates, confirmed=args.confirm_state_write)

    binary, binary_source = _resolve_native_binary(environment, paths.root)

    if args.prepare_command == "query":
        if binary is None:
            raise PrepareCLIError("ready context is required; run prepare inspect")
        binding = _read_binding(binding_path, snapshot)
        if binding is None:
            raise PrepareCLIError("ready context is required; run prepare inspect")
        status_result = _invoke_native(
            binary,
            "status",
            repository,
            state_root,
            snapshot,
            index_identity=binding,
        )
        if status_result.next_safe_action != "use-index":
            raise PrepareCLIError("ready context is required; run prepare inspect")
        touch_binding(binding_path)
        query_text, result_identities = _validate_query_arguments(args)
        result = _invoke_native(
            binary,
            args.operation,
            repository,
            state_root,
            snapshot,
            index_identity=binding,
            query=query_text,
            result_identities=result_identities,
            filters={
                "path_prefixes": sorted(set(args.path_prefix)),
                "languages": normalize_filter_values(args.language, "--language", FILTER_LANGUAGES),
                "symbol_kinds": normalize_filter_values(args.symbol_kind, "--symbol-kind", FILTER_SYMBOL_KINDS),
                "source_types": sorted(set(args.source_type)),
            },
            maximum_results=args.maximum_results,
            maximum_output_characters=args.maximum_output_characters,
            allow_inferred=args.allow_inferred,
        )
        return _query_summary(result)

    if args.prepare_command in {"activate", "build"}:
        if binary is None and args.prepare_command == "activate":
            binary = _install_native_engine(environment, paths.root)
            binary_source = "managed"
        if binary is None:
            raise PrepareCLIError("native engine is unavailable")
        result = _invoke_native(
            binary,
            "build",
            repository,
            state_root,
            snapshot,
            index_identity=None,
        )
        if (
            result.status.value not in {"ready", "partial"}
            or result.index_identity is None
            or result.next_safe_action != "use-index"
        ):
            raise PrepareCLIError("native context build did not become ready")
        _write_binding(binding_path, snapshot, result.index_identity)
        return _summary(
            mode=args.prepare_command,
            snapshot=snapshot,
            binary=binary,
            binary_source=binary_source,
            result=result,
            estimate=result,
            authorizations=(),
            state=summarize_state(paths.root),
        )

    binding = _read_binding(binding_path, snapshot)
    status_result: Level1Result | None = None
    estimate_result: Level1Result | None = None
    if binary is not None:
        if binding is not None:
            status_result = _invoke_native(
                binary,
                "status",
                repository,
                state_root,
                snapshot,
                index_identity=binding,
            )
            if status_result.next_safe_action == "use-index":
                touch_binding(binding_path)
        if status_result is None or status_result.next_safe_action != "use-index":
            estimate_result = _invoke_native(
                binary,
                "estimate",
                repository,
                state_root,
                snapshot,
                index_identity=None,
            )

    result = status_result or estimate_result
    if binary is None:
        authorizations = ("network", "state-write")
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
    )


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


def _install_native_engine(
    environment: Mapping[str, str], state_home: Path
) -> Path:
    _system, _machine, asset = _platform_asset()
    del environment
    base = _NATIVE_RELEASE_BASE_URL.rstrip("/")
    parsed = url_parse.urlparse(base)
    if parsed.scheme not in {"https", "file"}:
        raise PrepareCLIError("native engine release URL is unsafe")
    encoded_asset = url_parse.quote(asset)
    checksum_bytes = _download(
        f"{base}/{encoded_asset}.sha256", _MAX_CHECKSUM_BYTES
    )
    try:
        checksum_text = checksum_bytes.decode("ascii")
    except UnicodeDecodeError as exc:
        raise PrepareCLIError("native engine checksum is invalid") from exc
    match = _CHECKSUM.fullmatch(checksum_text)
    if match is None or match.group(2) != asset:
        raise PrepareCLIError("native engine checksum is invalid")
    payload = _download(f"{base}/{encoded_asset}", _MAX_NATIVE_BINARY_BYTES)
    if hashlib.sha256(payload).hexdigest() != match.group(1):
        raise PrepareCLIError("native engine checksum mismatch")

    destination = _managed_binary_path(state_home)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    current = destination.parent
    stop = state_home.resolve(strict=False).parent
    while current != stop and current != current.parent:
        current.chmod(0o700)
        if current == state_home.resolve(strict=False):
            break
        current = current.parent
    descriptor, temporary = tempfile.mkstemp(prefix=".taf-level1-", dir=destination.parent)
    temporary_path = Path(temporary)
    try:
        _set_descriptor_mode(descriptor, 0o700)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, destination)
        destination.chmod(0o700)
        _validate_binary(destination)
    except OSError as exc:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise PrepareCLIError("native engine could not be installed") from exc
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
    return destination.resolve()


def _download(url: str, maximum_bytes: int) -> bytes:
    try:
        with url_request.urlopen(url, timeout=60) as response:
            final_scheme = url_parse.urlparse(response.geturl()).scheme
            if final_scheme not in {"https", "file"}:
                raise PrepareCLIError("native engine download redirect is unsafe")
            value = response.read(maximum_bytes + 1)
    except (OSError, url_error.URLError) as exc:
        raise PrepareCLIError("native engine download failed") from exc
    if not value or len(value) > maximum_bytes:
        raise PrepareCLIError("native engine download exceeded its bound")
    return value


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
    binary: Path,
    operation: str,
    repository: Path,
    state_root: Path,
    snapshot: object,
    *,
    index_identity: str | None,
    query: str | None = None,
    result_identities: tuple[str, ...] = (),
    filters: dict[str, list[str]] | None = None,
    maximum_results: int = 8,
    maximum_output_characters: int = 4000,
    allow_inferred: bool = False,
) -> Level1Result:
    request_filters = filters or {
        "path_prefixes": [],
        "languages": [],
        "symbol_kinds": [],
        "source_types": [],
    }
    request_material = "\0".join(
        (
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
        )
    ).encode("utf-8")
    request_identity = "taf.prepare." + hashlib.sha256(request_material).hexdigest()[:24]
    envelope = {
        "phase": {
            "build": "build",
            "estimate": "estimate",
            "status": "inspect",
        }.get(operation, "query"),
        "repository_root": str(repository),
        "state_root": str(state_root),
        "changed_paths_document": None,
        "request": {
            "schema_version": "1",
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
    wire = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    try:
        completed = subprocess.run(
            [str(binary)],
            input=wire,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=_NATIVE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PrepareCLIError("native engine invocation failed") from exc
    if completed.returncode != 0:
        raise PrepareCLIError("native engine rejected the request")
    try:
        result = parse_level1_result(completed.stdout)
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


def _validate_query_arguments(args: argparse.Namespace) -> tuple[str | None, tuple[str, ...]]:
    query_operations = {"search-symbols", "search-docs"}
    query_text = args.query.strip() if isinstance(args.query, str) else None
    if args.operation in query_operations and not query_text:
        raise PrepareCLIError("selected query operation requires --query")
    if args.operation not in query_operations and query_text is not None:
        raise PrepareCLIError("selected query operation does not accept --query")
    result_identities = tuple(sorted(set(args.result_id)))
    if args.operation == "source-snippets" and not result_identities:
        raise PrepareCLIError("source-snippets requires at least one --result-id")
    if args.operation != "source-snippets" and result_identities:
        raise PrepareCLIError("selected query operation does not accept --result-id")
    if any(_SHA256.fullmatch(item) is None for item in result_identities):
        raise PrepareCLIError("query result identity is invalid")
    return query_text, result_identities


def _query_summary(result: Level1Result) -> dict[str, object]:
    return {
        "schema_version": "1",
        "mode": "query",
        "operation": result.operation.value,
        "status": result.status.value,
        "freshness": result.freshness.value,
        "index_identity": result.index_identity,
        "findings": [item.to_dict() for item in result.findings],
        "returned_count": result.returned_count,
        "omitted_count": result.omitted_count,
        "truncated": result.truncated,
        "output_characters": result.output_characters,
        "warnings": list(result.warnings),
        "required_authorizations": [],
        "next_safe_action": result.next_safe_action,
    }


def _lifecycle_summary(
    mode: str, root: Path, candidates: list[Candidate], *, confirmed: bool
) -> dict[str, object]:
    removed: list[Candidate] = []
    if confirmed and candidates:
        try:
            removed = apply_plan(root, candidates)
        except StateError as exc:
            raise PrepareCLIError(exc.code) from exc
    return {
        "schema_version": "1",
        "mode": mode,
        "dry_run": not confirmed,
        "candidates": [_candidate_dict(item) for item in candidates],
        "candidate_bytes": sum(item.bytes for item in candidates),
        "removed": [_candidate_dict(item) for item in removed],
        "removed_bytes": sum(item.bytes for item in removed),
        "required_authorizations": [] if confirmed or not candidates else ["state-write"],
        "next_safe_action": "none" if confirmed or not candidates else "confirm-state-write",
    }


def _candidate_dict(item: Candidate) -> dict[str, object]:
    return {"category": item.category, "relative_path": item.relative_path, "bytes": item.bytes}


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
) -> dict[str, object]:
    if binary is None:
        next_action = "install-native-engine"
    elif result is None:
        next_action = "build-index"
    else:
        next_action = result.next_safe_action
    context_status = "unavailable" if result is None else result.status.value
    freshness = "unknown" if result is None else result.freshness.value
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
        "warnings": sorted(set(() if result is None else result.warnings)),
    }


def _read_binding(binding_path: Path, snapshot: object) -> str | None:
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
        value = json.loads(binding_path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PrepareCLIError("context binding is invalid") from exc
    expected = {
        "schema_version": "1",
        "repository_identity": snapshot.repository_identity,
        "worktree_identity": snapshot.worktree_identity,
    }
    if type(value) is not dict or any(value.get(key) != item for key, item in expected.items()):
        return None
    index_identity = value.get("index_identity")
    if not isinstance(index_identity, str) or not _SHA256.fullmatch(index_identity):
        raise PrepareCLIError("context binding is invalid")
    return index_identity


def _write_binding(binding_path: Path, snapshot: object, index_identity: str) -> None:
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
    payload = json.dumps(
        {
            "schema_version": "1",
            "repository_identity": snapshot.repository_identity,
            "worktree_identity": snapshot.worktree_identity,
            "index_identity": index_identity,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
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
