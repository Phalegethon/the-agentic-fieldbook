"""High-level, user-facing preparation of bounded repository context."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import time
from typing import Mapping

from .context_operations import (  # noqa: F401 - re-exported for callers and tests
    CHANGE_QUERY_OPERATIONS,
    FILTER_LANGUAGES,
    FILTER_SYMBOL_KINDS,
    PrepareCLIError,
    QUERY_DIRECTIONS,
    QueryArguments,
    _invoke_native,
    _managed_binary_path,
    _platform_asset,
    _read_binding,
    _repository_state_paths,
    _resolve_native_binary,
    _set_descriptor_mode,
    _state_paths,
    _validate_binary,
    default_maximum_results,
    default_output_characters,
    normalize_change_base,
    normalize_filter_values,
    run_build,
    run_inspect,
    run_query,
    validate_query_request,
)
from .git_snapshot import collect_snapshot
from .native_transport import OneShotTransport
from .state_lifecycle import Candidate, apply_plan, plan_gc, plan_remove
from .state_paths import StateError


_CHECKSUM = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9._-]+)\n\Z")
_TAF_RELEASE_VERSION = "2.7.2"
_NATIVE_RELEASE_BASE_URL = (
    "https://github.com/Phalegethon/the-agentic-fieldbook/releases/download/"
    f"v{_TAF_RELEASE_VERSION}"
)
_MAX_NATIVE_BINARY_BYTES = 64 * 1024 * 1024
_MAX_CHECKSUM_BYTES = 1024


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
            "related-symbols",
            "changed-symbols",
            "impact-candidates",
            "repository-overview",
        ),
    )
    query.add_argument("--query")
    query.add_argument("--result-id", action="append", default=[])
    query.add_argument("--direction", choices=QUERY_DIRECTIONS)
    query.add_argument("--base")
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
    # Both defaults are resolved per operation once the operation is known
    # (`_validate_query_arguments`), so repository-overview answers with more
    # files and more room for its file layer while every other operation
    # keeps the shared 8 and 4000.
    query.add_argument("--maximum-results", type=int, choices=range(1, 65), default=None)
    query.add_argument(
        "--maximum-output-characters",
        type=int,
        choices=(2000, 4000, 8000, 12000),
        default=None,
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

    repository = Path(args.repo)
    if args.prepare_command == "remove":
        snapshot = collect_snapshot(repository.resolve())
        if snapshot.head_sha is None:
            raise PrepareCLIError("repository must have at least one commit")
        paths = _state_paths(environment)
        repository_key = snapshot.repository_identity.removeprefix("sha256:")
        worktree_key = snapshot.worktree_identity.removeprefix("sha256:")
        candidates = plan_remove(paths.root, repository_key, worktree_key)
        return _lifecycle_summary("remove", paths.root, candidates, confirmed=args.confirm_state_write)
    if args.prepare_command == "query":
        query_text, result_identities, base, maximum_results, output_characters = (
            _validate_query_arguments(args)
        )
        arguments = QueryArguments(
            operation=args.operation,
            query=query_text,
            result_identities=result_identities,
            direction=args.direction,
            base=base,
            path_prefixes=sorted(set(args.path_prefix)),
            languages=normalize_filter_values(args.language, "--language", FILTER_LANGUAGES),
            symbol_kinds=normalize_filter_values(args.symbol_kind, "--symbol-kind", FILTER_SYMBOL_KINDS),
            source_types=sorted(set(args.source_type)),
            maximum_results=maximum_results,
            maximum_output_characters=output_characters,
            allow_inferred=args.allow_inferred,
        )
        if args.operation in CHANGE_QUERY_OPERATIONS:
            return _run_change_query_over_a_session(
                repository, arguments, environment=environment
            )
        return run_query(repository, arguments, environment=environment, transport_for=OneShotTransport)
    if args.prepare_command in {"activate", "build"}:
        return run_build(
            repository,
            environment=environment,
            transport_for=OneShotTransport,
            mode=args.prepare_command,
            installer=_install_native_engine if args.prepare_command == "activate" else None,
        )
    return run_inspect(repository, environment=environment, transport_for=OneShotTransport)


def _run_change_query_over_a_session(
    repository: Path, arguments: QueryArguments, *, environment: Mapping[str, str]
) -> dict[str, object]:
    """Answer one change query over a single reused engine child.

    A composed change query makes one engine call per changed symbol, and a
    fresh process per call reloads the index every time. One session pays that
    once, so the recovery command the skill runs stays interactive.
    """
    from .engine_session import Level1Session, SessionTransport  # change-query only

    sessions: list[Level1Session] = []

    def transport_for(binary: Path) -> SessionTransport:
        session = Level1Session(binary)
        sessions.append(session)
        return SessionTransport(session)

    try:
        return run_query(
            repository, arguments, environment=environment, transport_for=transport_for
        )
    finally:
        for session in sessions:
            session.close()


def _install_native_engine(
    environment: Mapping[str, str], state_home: Path
) -> Path:
    import tempfile  # activate-only dependency; kept off the query path
    from urllib import parse as url_parse

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
    from urllib import error as url_error  # activate-only dependency
    from urllib import parse as url_parse
    from urllib import request as url_request

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


def _validate_query_arguments(
    args: argparse.Namespace,
) -> tuple[str | None, tuple[str, ...], str | None, int, int]:
    """The validated query, anchors, change base, result count, and output budget.

    Both the result count and the budget are the flag when it was given and
    the operation's own default when it was not, resolved from the same
    tables the MCP server reads, so the two surfaces cannot drift apart on
    what an unbudgeted request means.
    """
    query_text, result_identities = validate_query_request(
        args.operation,
        args.query,
        tuple(args.result_id),
        args.direction,
        args.base,
        symbol_kinds=args.symbol_kind,
        source_types=args.source_type,
    )
    maximum_results = args.maximum_results
    if maximum_results is None:
        maximum_results = default_maximum_results(args.operation)
    output_characters = args.maximum_output_characters
    if output_characters is None:
        output_characters = default_output_characters(args.operation)
    return (
        query_text,
        result_identities,
        normalize_change_base(args.base),
        maximum_results,
        output_characters,
    )


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
