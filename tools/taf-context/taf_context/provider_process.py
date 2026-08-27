"""Fail-closed stdio boundary for active local context providers."""

from __future__ import annotations

import hashlib
import json
import os
import resource
import signal
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from .level1_models import Level1Request, Level1Result, parse_level1_result
from .models import RepositorySnapshot
from .provider_execution_models import (
    AdapterManifest,
    AdapterPhase,
    AttemptRecord,
    AttemptStatus,
    ExecutionPolicy,
    InspectionRecord,
    parse_inspection_record,
)


class ProviderProcessError(RuntimeError):
    """One stable reason code for a rejected provider attempt."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def inspect_provider(
    manifest: AdapterManifest,
    adapter_root: Path,
    snapshot: RepositorySnapshot,
    repository_root: Path,
    policy: ExecutionPolicy,
) -> tuple[InspectionRecord, AttemptRecord]:
    """Inspect one explicitly configured provider without trusting its claims."""
    if AdapterPhase.INSPECT not in manifest.supported_phases:
        raise ProviderProcessError("inspection-unsupported")
    envelope = {
        "phase": "inspect",
        "repository_root": str(repository_root.resolve()),
        "snapshot": snapshot.to_dict(),
    }
    wire, attempt = _execute(
        manifest, adapter_root, repository_root, policy,
        AdapterPhase.INSPECT, envelope,
    )
    try:
        record = parse_inspection_record(wire)
    except ValueError as error:
        raise ProviderProcessError("invalid-inspection-output") from error
    if (
        record.adapter_identity != manifest.adapter_identity
        or record.provider_identity != manifest.provider_identity
        or record.provider_version != manifest.provider_version
    ):
        raise ProviderProcessError("inspection-identity-mismatch")
    return record, attempt


def query_provider(
    manifest: AdapterManifest,
    adapter_root: Path,
    request: Level1Request,
    repository_root: Path,
    policy: ExecutionPolicy,
) -> tuple[Level1Result, AttemptRecord]:
    """Execute one bounded Level 1 query through an explicit adapter."""
    if AdapterPhase.QUERY not in manifest.supported_phases:
        raise ProviderProcessError("query-unsupported")
    if request.provider_identity != manifest.provider_identity:
        raise ProviderProcessError("request-provider-mismatch")
    envelope = {
        "phase": "query",
        "repository_root": str(repository_root.resolve()),
        "request": request.to_dict(),
    }
    wire, attempt = _execute(
        manifest, adapter_root, repository_root, policy,
        AdapterPhase.QUERY, envelope,
    )
    try:
        result = parse_level1_result(wire)
    except ValueError as error:
        raise ProviderProcessError("invalid-query-output") from error
    exact = (
        (result.request_identity, request.request_identity),
        (result.operation, request.operation),
        (result.provider_identity, request.provider_identity),
        (result.provider_version, manifest.provider_version),
        (result.repository_identity, request.repository_identity),
        (result.worktree_identity, request.worktree_identity),
        (result.committed_head, request.committed_head),
        (result.dirty_overlay_fingerprint, request.dirty_overlay_fingerprint),
    )
    if any(actual != expected for actual, expected in exact):
        raise ProviderProcessError("query-identity-mismatch")
    if result.index_identity != request.index_identity:
        raise ProviderProcessError("query-index-mismatch")
    if result.returned_count > request.maximum_results:
        raise ProviderProcessError("query-result-count-exceeded")
    if result.output_characters > request.maximum_model_output_characters:
        raise ProviderProcessError("query-model-budget-exceeded")
    _validate_citations(result, repository_root)
    return result, attempt


def _execute(
    manifest: AdapterManifest,
    adapter_root: Path,
    repository_root: Path,
    policy: ExecutionPolicy,
    phase: AdapterPhase,
    envelope: dict[str, object],
) -> tuple[bytes, AttemptRecord]:
    root = adapter_root.resolve()
    repo = repository_root.resolve()
    executable = _safe_executable(root, manifest.executable)
    if executable is None:
        raise ProviderProcessError("unsafe-adapter-executable")
    if manifest.network_required and not policy.network_allowed:
        raise ProviderProcessError("provider-network-denied")
    executable_digest = _digest_file(executable)
    repository_state = _repository_state(repo)
    environment = {
        name: os.environ[name]
        for name in manifest.environment_allowlist
        if name in os.environ
    }
    payload = (
        json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    command = [str(executable), *manifest.arguments]
    started = time.monotonic_ns()
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        process = subprocess.Popen(
            command,
            cwd=root,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=True,
            preexec_fn=_limit_output_files,
        )
        try:
            process.communicate(payload, timeout=policy.timeout_seconds)
        except subprocess.TimeoutExpired as error:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise ProviderProcessError("provider-timeout") from error
        elapsed = time.monotonic_ns() - started
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read(policy.maximum_stdout_bytes + 1)
        stderr = stderr_file.read(policy.maximum_stderr_bytes + 1)
    if _repository_state(repo) != repository_state:
        raise ProviderProcessError("repository-mutated")
    if not executable.exists() or _digest_file(executable) != executable_digest:
        raise ProviderProcessError("adapter-executable-mutated")
    if len(stdout) > policy.maximum_stdout_bytes:
        raise ProviderProcessError("stdout-oversized")
    if len(stderr) > policy.maximum_stderr_bytes:
        raise ProviderProcessError("stderr-oversized")
    if process.returncode != 0:
        raise ProviderProcessError("provider-nonzero")
    if not stdout.endswith(b"\n") or stdout.count(b"\n") != 1:
        raise ProviderProcessError("invalid-stdout-framing")
    attempt = AttemptRecord(
        "1", manifest.provider_identity, phase, AttemptStatus.SUCCEEDED, (),
        elapsed, len(stdout), len(stderr),
    )
    return stdout[:-1], attempt


def _limit_output_files() -> None:
    resource.setrlimit(resource.RLIMIT_FSIZE, (300 * 1024, 300 * 1024))


def _safe_executable(root: Path, relative: str) -> Path | None:
    candidate = root.joinpath(*relative.split("/"))
    try:
        resolved = candidate.resolve(strict=True)
        metadata = candidate.lstat()
    except (OSError, RuntimeError):
        return None
    if resolved != candidate.absolute() or root not in resolved.parents:
        return None
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        return None
    return resolved


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _repository_state(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        metadata = path.lstat()
        descriptor = (
            relative.as_posix(), metadata.st_mode, metadata.st_size,
            metadata.st_mtime_ns,
            os.readlink(path) if path.is_symlink() else "",
        )
        digest.update(repr(descriptor).encode("utf-8", "surrogateescape"))
    return digest.hexdigest()


def _validate_citations(result: Level1Result, repository_root: Path) -> None:
    root = repository_root.resolve()
    for finding in result.findings:
        candidate = root.joinpath(*finding.path.split("/"))
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ProviderProcessError("query-citation-invalid") from error
        if root not in resolved.parents or not resolved.is_file():
            raise ProviderProcessError("query-citation-invalid")
        with resolved.open("rb") as handle:
            line_count = sum(1 for _ in handle)
        if finding.end_line > line_count:
            raise ProviderProcessError("query-citation-invalid")
