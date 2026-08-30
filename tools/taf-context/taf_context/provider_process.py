"""Fail-closed stdio boundary for active local context providers."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .level1_models import Level1Request, Level1Result, parse_level1_result
from .models import RepositorySnapshot
from .provider_binding import AdapterBinding, validate_binding_for_repository
from .provider_execution_models import (
    AdapterManifest,
    AdapterPhase,
    AttemptRecord,
    AttemptStatus,
    ExecutionPolicy,
    InspectionRecord,
    parse_inspection_record,
)

try:
    import resource as _resource
except ModuleNotFoundError:  # Not provided by Python on Windows.
    _resource = None


_PROVIDER_REASON_CODES = frozenset({
    "adapter-binding-mismatch",
    "adapter-binding-overlap",
    "adapter-executable-mutated",
    "inspection-identity-mismatch",
    "inspection-unsupported",
    "invalid-inspection-output",
    "invalid-query-output",
    "invalid-stdout-framing",
    "provider-executable-digest-mismatch",
    "provider-executable-mutated",
    "provider-isolation-unavailable",
    "provider-network-denied",
    "provider-nonzero",
    "provider-timeout",
    "query-citation-invalid",
    "query-identity-mismatch",
    "query-index-mismatch",
    "query-model-budget-exceeded",
    "query-result-count-exceeded",
    "query-unsupported",
    "repository-mutated",
    "request-provider-mismatch",
    "stderr-oversized",
    "stdout-oversized",
    "unsafe-adapter-executable",
    "unsafe-adapter-interpreter",
})


class ProviderProcessError(RuntimeError):
    """One stable reason code for a rejected provider attempt."""

    def __init__(self, reason_code: str) -> None:
        if reason_code not in _PROVIDER_REASON_CODES:
            raise ValueError("unregistered-provider-reason-code")
        self.reason_code = reason_code
        super().__init__(reason_code)


def inspect_provider(
    manifest: AdapterManifest,
    adapter_root: Path,
    snapshot: RepositorySnapshot,
    repository_root: Path,
    policy: ExecutionPolicy,
    *,
    binding: AdapterBinding | None = None,
) -> tuple[InspectionRecord, AttemptRecord]:
    """Inspect one explicitly configured provider without trusting its claims."""
    if AdapterPhase.INSPECT not in manifest.supported_phases:
        raise ProviderProcessError("inspection-unsupported")
    envelope = {
        "phase": "inspect",
        "repository_root": str(repository_root.resolve()),
        "snapshot": snapshot.to_dict(),
    }
    _attach_binding(envelope, manifest, adapter_root, repository_root, binding)
    wire, attempt = _execute(
        manifest, adapter_root, repository_root, policy,
        AdapterPhase.INSPECT, envelope, binding,
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
    *,
    binding: AdapterBinding | None = None,
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
    _attach_binding(envelope, manifest, adapter_root, repository_root, binding)
    wire, attempt = _execute(
        manifest, adapter_root, repository_root, policy,
        AdapterPhase.QUERY, envelope, binding,
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
    binding: AdapterBinding | None,
) -> tuple[bytes, AttemptRecord]:
    root = adapter_root.resolve()
    repo = repository_root.resolve()
    executable = _safe_executable(root, manifest.executable)
    if executable is None:
        raise ProviderProcessError("unsafe-adapter-executable")
    if manifest.network_required and not policy.network_allowed:
        raise ProviderProcessError("provider-network-denied")
    executable_digest = _digest_file(executable)
    child_digest = None
    if binding is not None:
        child_digest = "sha256:" + _digest_file(binding.provider_executable)
        if child_digest != binding.provider_executable_digest:
            raise ProviderProcessError("provider-executable-digest-mismatch")
    repository_state = _repository_state(repo)
    environment = {
        name: os.environ[name]
        for name in manifest.environment_allowlist
        if name in os.environ
    }
    payload = (
        json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    started = time.monotonic_ns()
    with tempfile.TemporaryDirectory(prefix="taf-provider-state-") as state_name, tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        command = _isolated_command(
            executable,
            manifest.arguments,
            root,
            repo,
            Path(state_name),
            binding,
        )
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
    if (
        binding is not None
        and (
            not binding.provider_executable.exists()
            or "sha256:" + _digest_file(binding.provider_executable) != child_digest
        )
    ):
        raise ProviderProcessError("provider-executable-mutated")
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
    if _resource is None:
        raise ProviderProcessError("provider-isolation-unavailable")
    _resource.setrlimit(_resource.RLIMIT_FSIZE, (300 * 1024, 300 * 1024))


def _isolated_command(
    executable: Path,
    arguments: tuple[str, ...],
    adapter_root: Path,
    repository_root: Path,
    state_root: Path,
    binding: AdapterBinding | None = None,
) -> list[str]:
    sandbox = Path("/usr/bin/sandbox-exec")
    if sys.platform != "darwin" or not sandbox.is_file():
        raise ProviderProcessError("provider-isolation-unavailable")
    runtime, runtime_arguments = _runtime_command(executable)
    return [
        str(sandbox),
        "-p",
        _sandbox_profile(
            runtime, adapter_root, repository_root, state_root, binding
        ),
        str(runtime),
        *runtime_arguments,
        *arguments,
    ]


def _runtime_command(executable: Path) -> tuple[Path, tuple[str, ...]]:
    try:
        with executable.open("rb") as handle:
            first_line = handle.readline(128).rstrip(b"\r\n")
    except OSError as error:
        raise ProviderProcessError("unsafe-adapter-executable") from error
    if first_line != b"#!/usr/bin/python3":
        return executable, ()
    interpreter = Path(sys.executable).resolve()
    trusted_root = Path("/Library/Developer/CommandLineTools").resolve()
    if interpreter != trusted_root and trusted_root not in interpreter.parents:
        raise ProviderProcessError("unsafe-adapter-interpreter")
    python_app = (
        interpreter.parent.parent
        / "Resources/Python.app/Contents/MacOS/Python"
    )
    if not python_app.is_file() or not os.access(python_app, os.X_OK):
        raise ProviderProcessError("unsafe-adapter-interpreter")
    return python_app.resolve(), (str(executable),)


def _sandbox_profile(
    executable: Path,
    adapter_root: Path,
    repository_root: Path,
    state_root: Path,
    binding: AdapterBinding | None,
) -> str:
    read_roots = tuple(
        path.resolve()
        for path in (
            adapter_root,
            repository_root,
            state_root,
            Path("/System"),
            Path("/usr"),
            Path("/Library"),
            Path("/dev"),
            *(binding.provider_state_roots if binding is not None else ()),
        )
        if path.exists()
    )
    ancestors = tuple(
        sorted(
            {
                ancestor
                for root in read_roots
                for ancestor in (root, *root.parents)
                if ancestor != Path("/")
            },
            key=str,
        )
    )
    literal_reads = "\n".join(
        f'  (literal "{_sandbox_escape(path)}")' for path in ancestors
    )
    subtree_reads = "\n".join(
        f'  (subpath "{_sandbox_escape(path)}")' for path in read_roots
    )
    allowed_executables = {executable.resolve()}
    if binding is not None:
        allowed_executables.add(binding.provider_executable)
    process_rules = "\n".join(
        f'  (literal "{_sandbox_escape(path)}")'
        for path in sorted(allowed_executables, key=str)
    )
    state = state_root.resolve()
    process_fork_rule = (
        "(allow process-fork)\n" if binding is not None else ""
    )
    return (
        "(version 1)\n"
        "(deny default)\n"
        f"{process_fork_rule}"
        "(allow sysctl-read)\n"
        "(allow mach-lookup)\n"
        "(allow ipc-posix-shm)\n"
        "(allow process-exec\n"
        f"{process_rules})\n"
        "(allow file-read*\n"
        "  (literal \"/\")\n"
        f"{literal_reads}\n"
        f"{subtree_reads})\n"
        "(allow file-write*\n"
        f'  (literal "{_sandbox_escape(state)}")\n'
        f'  (subpath "{_sandbox_escape(state)}"))\n'
    )


def _sandbox_escape(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def _attach_binding(
    envelope: dict[str, object],
    manifest: AdapterManifest,
    adapter_root: Path,
    repository_root: Path,
    binding: AdapterBinding | None,
) -> None:
    if binding is None:
        return
    if (
        binding.adapter_identity != manifest.adapter_identity
        or binding.provider_identity != manifest.provider_identity
        or binding.adapter_root != adapter_root.resolve()
    ):
        raise ProviderProcessError("adapter-binding-mismatch")
    try:
        validate_binding_for_repository(binding, repository_root)
    except ValueError as error:
        raise ProviderProcessError("adapter-binding-overlap") from error
    envelope["provider_command"] = {
        "executable": str(binding.provider_executable),
        "executable_digest": binding.provider_executable_digest,
        "arguments": list(binding.provider_arguments),
        "state_roots": [str(path) for path in binding.provider_state_roots],
        "environment": dict(binding.environment),
        "binding_digest": binding.binding_digest,
        "transport": binding.transport.value,
    }


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
