"""Delegated CLI parsing and execution for passive provider control commands."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import sys
from typing import Callable, Mapping, TextIO

from .bounded_fallback import FallbackPolicy, run_bounded_fallback
from .consent import AuthorizationLedger, ConsentDisposition
from .discovery import discover_providers
from .level1_models import Level1Request
from .models import ContextAction, RepositorySnapshot
from .provider_broker import execute_broker_request
from .provider_execution_models import ExecutionPolicy, parse_adapter_manifest
from .provider_models import (
    BrokerRequest,
    ConsentRequest,
    DiscoverySnapshot,
    HostInventory,
    RoutingDecision,
    parse_host_inventory,
)
from .provider_process import inspect_provider, query_provider
from .provider_state import (
    StateError,
    StatePaths,
    read_consent,
    read_project_registration,
    read_user_registry,
    resolve_state_paths,
    write_consent,
)
from .routing import route_provider


_MAX_CONTROL_BYTES = 256 * 1024


class ProviderCLIError(ValueError):
    """A stable, concise provider-command input error."""


def register_provider_commands(subparsers: argparse._SubParsersAction) -> None:
    """Register provider and consent command groups on *subparsers*."""
    providers = subparsers.add_parser("providers")
    provider_commands = providers.add_subparsers(
        dest="provider_command", required=True
    )

    discover = provider_commands.add_parser("discover")
    discover.add_argument("--repo", required=True)
    discover.add_argument("--snapshot", required=True)
    discover.add_argument("--inventory")

    route = provider_commands.add_parser("route")
    route.add_argument("--discovery", required=True)
    route.add_argument("--request", required=True)

    execute = provider_commands.add_parser("execute")
    execute.add_argument("--repo", required=True)
    execute.add_argument("--snapshot", required=True)
    execute.add_argument("--discovery", required=True)
    execute.add_argument("--request", required=True)
    execute.add_argument("--adapter-manifest", action="append", default=[])
    execute.add_argument("--adapter-root", action="append", default=[])
    execute.add_argument("--timeout-seconds", type=float, default=10.0)
    execute.add_argument("--allow-fallback", action="store_true")

    consent = subparsers.add_parser("consent")
    consent_commands = consent.add_subparsers(dest="consent_command", required=True)

    list_command = consent_commands.add_parser("list")
    list_command.add_argument("--repository-identity")

    request = consent_commands.add_parser("request")
    request.add_argument("--decision", required=True)

    record = consent_commands.add_parser("record")
    record.add_argument("--request", required=True)
    record.add_argument(
        "--decision",
        required=True,
        choices=tuple(item.value for item in ConsentDisposition),
    )

    revoke = consent_commands.add_parser("revoke")
    revoke.add_argument("--repository-identity", required=True)
    revoke.add_argument("--provider", required=True)
    revoke.add_argument("--provider-schema", required=True)
    revoke.add_argument(
        "--action", required=True, choices=tuple(item.value for item in ContextAction)
    )


def run_provider_command(
    args: argparse.Namespace,
    *,
    stdout: TextIO,
    environment: Mapping[str, str],
    utc_clock: Callable[[], datetime],
) -> dict[str, object]:
    """Execute one already-parsed provider command and return JSON-ready output."""
    del stdout  # Kept in the public boundary for delegated CLI stream parity.
    if args.command == "providers" and args.provider_command == "discover":
        return _discover_command(args, environment)
    if args.command == "providers" and args.provider_command == "route":
        return _route_command(args, environment, utc_clock)
    if args.command == "providers" and args.provider_command == "execute":
        return _execute_command(args, environment, utc_clock)
    if args.command == "consent" and args.consent_command == "request":
        return _consent_request_command(args)
    if args.command == "consent" and args.consent_command == "list":
        return _consent_list_command(args, environment)
    if args.command == "consent" and args.consent_command == "record":
        return _consent_record_command(args, environment, utc_clock)
    if args.command == "consent" and args.consent_command == "revoke":
        return _consent_revoke_command(args, environment, utc_clock)
    raise ProviderCLIError("provider command is required")


def _discover_command(
    args: argparse.Namespace, environment: Mapping[str, str]
) -> dict[str, object]:
    snapshot = RepositorySnapshot.from_dict(_read_json_object(Path(args.snapshot)))
    repository = _resolved_directory(Path(args.repo), "repository unavailable")
    snapshot_root = _resolved_directory(
        Path(snapshot.canonical_root), "snapshot repository unavailable"
    )
    if repository != snapshot_root:
        raise ProviderCLIError("repository does not match snapshot root")

    if args.inventory is None:
        inventory = HostInventory("1", (), 0, (), 0, False)
    else:
        inventory_source = None if args.inventory == "-" else Path(args.inventory)
        inventory = parse_host_inventory(_read_control_bytes(inventory_source)).inventory

    paths = _state_paths(environment)
    registry = read_user_registry(paths)
    registration = read_project_registration(
        repository, snapshot.repository_identity
    )
    return discover_providers(snapshot, inventory, registry, registration).to_dict()


def _route_command(
    args: argparse.Namespace,
    environment: Mapping[str, str],
    utc_clock: Callable[[], datetime],
) -> dict[str, object]:
    discovery = DiscoverySnapshot.from_dict(
        _read_json_object(Path(args.discovery))
    )
    request = BrokerRequest.from_dict(_read_json_object(Path(args.request)))
    paths = _state_paths(environment)
    consent_state_usable = True
    try:
        consent = read_consent(paths)
    except StateError:
        consent = AuthorizationLedger()
        consent_state_usable = False
    decision = route_provider(
        discovery,
        request,
        consent,
        utc_now=_rfc3339(utc_clock()),
        consent_state_usable=consent_state_usable,
    )
    return decision.to_dict()


def _execute_command(
    args: argparse.Namespace,
    environment: Mapping[str, str],
    utc_clock: Callable[[], datetime],
) -> dict[str, object]:
    if len(args.adapter_manifest) != len(args.adapter_root):
        raise ProviderCLIError("adapter manifest and root counts must match")
    repository = _resolved_directory(Path(args.repo), "repository unavailable")
    snapshot = RepositorySnapshot.from_dict(
        _read_json_object(Path(args.snapshot))
    )
    if repository != _resolved_directory(
        Path(snapshot.canonical_root), "snapshot repository unavailable"
    ):
        raise ProviderCLIError("repository does not match snapshot root")
    discovery = DiscoverySnapshot.from_dict(
        _read_json_object(Path(args.discovery))
    )
    request = Level1Request.from_dict(_read_json_object(Path(args.request)))
    if (
        discovery.repository_identity != snapshot.repository_identity
        or discovery.worktree_identity != snapshot.worktree_identity
        or request.repository_identity != snapshot.repository_identity
        or request.worktree_identity != snapshot.worktree_identity
        or request.committed_head != snapshot.head_sha
        or request.dirty_overlay_fingerprint != snapshot.dirty_fingerprint
    ):
        raise ProviderCLIError("execution inputs describe different snapshots")

    adapters: dict[str, tuple[object, Path]] = {}
    for manifest_path, root_path in zip(
        args.adapter_manifest, args.adapter_root
    ):
        manifest = parse_adapter_manifest(
            _read_control_bytes(Path(manifest_path))
        )
        adapter_root = _resolved_directory(
            Path(root_path), "adapter root unavailable"
        )
        if manifest.provider_identity in adapters:
            raise ProviderCLIError("duplicate adapter provider identity")
        adapters[manifest.provider_identity] = (manifest, adapter_root)

    try:
        consent = read_consent(_state_paths(environment))
    except StateError:
        consent = AuthorizationLedger()
    try:
        policy = ExecutionPolicy.from_dict(
            {
                "schema_version": "1",
                "timeout_seconds": args.timeout_seconds,
                "maximum_stdout_bytes": 256 * 1024,
                "maximum_stderr_bytes": 64 * 1024,
                "network_allowed": False,
                "fallback_allowed": bool(args.allow_fallback),
                "maximum_inspections": 3,
            }
        )
    except ValueError as error:
        raise ProviderCLIError(str(error)) from error

    def inspect_call(provider):
        manifest, adapter_root = adapters[provider.provider_identity]
        return inspect_provider(
            manifest, adapter_root, snapshot, repository, policy
        )

    def query_call(provider, level1_request):
        manifest, adapter_root = adapters[provider.provider_identity]
        return query_provider(
            manifest, adapter_root, level1_request, repository, policy
        )

    fallback_call = None
    if policy.fallback_allowed:
        candidate_paths = tuple(
            sorted(
                set(snapshot.tracked_paths)
                | set(snapshot.staged_paths)
                | set(snapshot.unstaged_paths)
                | set(snapshot.untracked_paths)
            )
        )

        def fallback_call(level1_request):
            result, _evidence = run_bounded_fallback(
                level1_request,
                repository,
                candidate_paths,
                FallbackPolicy(32, 512 * 1024, 64 * 1024, 256),
            )
            return result

    execution = execute_broker_request(
        discovery,
        request,
        consent,
        snapshot,
        adapters,
        inspect_call=inspect_call,
        query_call=query_call,
        fallback_call=fallback_call,
        utc_now=_rfc3339(utc_clock()),
    )
    return execution.to_dict()


def _consent_request_command(args: argparse.Namespace) -> dict[str, object]:
    decision = RoutingDecision.from_dict(_read_json_object(Path(args.decision)))
    if len(decision.consent_requests) != 1:
        raise ProviderCLIError("routing decision must contain exactly one consent request")
    return decision.consent_requests[0].to_dict()


def _consent_list_command(
    args: argparse.Namespace, environment: Mapping[str, str]
) -> dict[str, object]:
    ledger = read_consent(_state_paths(environment))
    if args.repository_identity is not None:
        ledger = AuthorizationLedger(
            tuple(
                record
                for record in ledger.records
                if record.repository_identity == args.repository_identity
            )
        )
    return ledger.to_dict()


def _consent_record_command(
    args: argparse.Namespace,
    environment: Mapping[str, str],
    utc_clock: Callable[[], datetime],
) -> dict[str, object]:
    request = ConsentRequest.from_dict(_read_json_object(Path(args.request)))
    disposition = ConsentDisposition(args.decision)
    paths = _state_paths(environment)
    ledger = read_consent(paths)
    timestamp = _rfc3339(utc_clock())
    updated = ledger.record(request, disposition, timestamp)
    audit = {
        "timestamp": timestamp,
        "request_digest": f"sha256:{request.digest}",
        "repository_fingerprint": request.repository_identity,
        "provider_identity": request.provider_identity,
        "provider_schema_version": request.provider_schema_version,
        "actions": [item.value for item in request.actions],
        "disposition": disposition.value,
        "operation": "record",
    }
    write_consent(paths, updated, audit)
    return updated.to_dict()


def _consent_revoke_command(
    args: argparse.Namespace,
    environment: Mapping[str, str],
    utc_clock: Callable[[], datetime],
) -> dict[str, object]:
    action = ContextAction(args.action)
    paths = _state_paths(environment)
    ledger = read_consent(paths)
    matches = tuple(
        record
        for record in ledger.records
        if record.action is action
        and record.repository_identity == args.repository_identity
        and record.provider_identity == args.provider
        and record.provider_schema_version == args.provider_schema
    )
    if not matches:
        raise ProviderCLIError("consent scope not found")
    effective = matches[-1]
    timestamp = _rfc3339(utc_clock())
    updated = ledger.revoke(
        action,
        args.repository_identity,
        args.provider,
        args.provider_schema,
    )
    audit = {
        "timestamp": timestamp,
        "request_digest": effective.request_digest,
        "repository_fingerprint": args.repository_identity,
        "provider_identity": args.provider,
        "provider_schema_version": args.provider_schema,
        "actions": [action.value],
        "disposition": effective.disposition.value,
        "operation": "revoke",
    }
    write_consent(paths, updated, audit)
    return updated.to_dict()


def _state_paths(environment: Mapping[str, str]) -> StatePaths:
    home = environment.get("HOME")
    if home is None:
        if (
            "TAF_STATE_HOME" not in environment
            and "XDG_STATE_HOME" not in environment
            and sys.platform != "win32"
        ):
            raise ProviderCLIError("state home unavailable")
        home = "."
    return resolve_state_paths(environment, sys.platform, Path(home))


def _read_json_object(path: Path) -> dict[str, object]:
    value = _load_json(_read_control_bytes(path))
    if type(value) is not dict:
        raise ProviderCLIError("control document must be a JSON object")
    return value


def _read_control_bytes(path: Path | None) -> bytes:
    if path is None:
        stream = sys.stdin
        binary = getattr(stream, "buffer", None)
        if binary is not None:
            data = binary.read(_MAX_CONTROL_BYTES + 1)
        else:
            chunks: list[bytes] = []
            size = 0
            while size <= _MAX_CONTROL_BYTES:
                text = stream.read(min(8192, _MAX_CONTROL_BYTES + 1 - size))
                if not text:
                    break
                encoded = text.encode("utf-8")
                chunks.append(encoded)
                size += len(encoded)
            data = b"".join(chunks)
        if len(data) > _MAX_CONTROL_BYTES:
            raise ProviderCLIError("control document exceeds 256 KiB")
        return data

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(os.fspath(path), flags)
    except OSError as exc:
        raise ProviderCLIError("control document unavailable") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ProviderCLIError("control document must be a regular file")
        if before.st_size > _MAX_CONTROL_BYTES:
            raise ProviderCLIError("control document exceeds 256 KiB")
        chunks = []
        remaining = _MAX_CONTROL_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(8192, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > _MAX_CONTROL_BYTES:
            raise ProviderCLIError("control document exceeds 256 KiB")
        after = os.fstat(descriptor)
        try:
            current = os.lstat(path)
        except OSError as exc:
            raise ProviderCLIError("control document changed during read") from exc
        if _file_identity(before) != _file_identity(after) or _file_identity(
            after
        ) != _file_identity(current):
            raise ProviderCLIError("control document changed during read")
        return data
    finally:
        os.close(descriptor)


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _load_json(raw: bytes) -> object:
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ProviderCLIError("invalid JSON constant")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderCLIError("invalid control document JSON") from exc


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProviderCLIError("duplicate JSON key")
        result[key] = value
    return result


def _resolved_directory(path: Path, message: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ProviderCLIError(message) from exc
    if not resolved.is_dir():
        raise ProviderCLIError(message)
    return resolved


def _rfc3339(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ProviderCLIError("UTC clock returned an invalid time")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="auto")
        .replace("+00:00", "Z")
    )
