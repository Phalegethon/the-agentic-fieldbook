"""Fail-closed process boundary for disposable Level 1 candidates."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Optional

from taf_context.level1_models import (
    CandidateAvailability,
    CandidateManifest,
    Level1Operation,
    Level1Request,
    Level1Result,
    parse_level1_result,
)
from taf_context.level1_render import _render_text, redact_preview


_MAX_STDOUT_BYTES = 256 * 1024
_MAX_STDERR_BYTES = 64 * 1024
_RSS_PATTERN = re.compile(rb"^\s*(\d+)\s+maximum resident set size\s*$", re.MULTILINE)
_SECRET = re.compile(r"(?i)(token|password|secret|api[_-]?key)\s*[:=]\s*[^\s,;]+")
_ABSOLUTE = re.compile(r"(?:/Users|/home|/private|/tmp|/var)/[^\s]+|[A-Za-z]:\\[^\s]+")


class CandidateProcessError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class IsolationCapability:
    offline_enforced: bool
    child_process_audited: bool
    rss_measured: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class ProcessEvidence:
    exit_code: int
    elapsed_ns: int
    peak_rss_bytes: int
    stdout_bytes: int
    stderr_bytes: int
    child_processes: tuple[str, ...]
    escape_counters: Mapping[str, int]

    def to_dict(self) -> dict[str, object]:
        return {
            "exit_code": self.exit_code,
            "elapsed_ns": self.elapsed_ns,
            "peak_rss_bytes": self.peak_rss_bytes,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "child_processes": list(self.child_processes),
            "escape_counters": dict(self.escape_counters),
        }


@dataclass(frozen=True)
class CandidatePreflight:
    availability: CandidateAvailability
    candidate_digest: Optional[str]
    executable_digest: Optional[str]
    dependency_lock_digest: Optional[str]
    license_inventory_digest: Optional[str]
    isolation: IsolationCapability
    reason_codes: tuple[str, ...]


def preflight_candidate(
    manifest: CandidateManifest,
    candidate_root: Path,
    environment: Mapping[str, str],
) -> CandidatePreflight:
    """Hash candidate inputs and prove required local isolation capabilities."""
    isolation = _isolation_capability()
    candidate_digest = _digest_bytes(_canonical_json(manifest.to_dict()))
    if manifest.availability is CandidateAvailability.UNSUPPORTED:
        reasons = tuple(sorted(set(manifest.unsupported_reason_codes + isolation.reason_codes)))
        return CandidatePreflight(
            CandidateAvailability.UNSUPPORTED,
            candidate_digest,
            None,
            None,
            None,
            isolation,
            reasons,
        )

    reasons = list(isolation.reason_codes)
    if not isinstance(candidate_root, Path):
        reasons.append("unsafe-candidate-root")
        root = Path(".").resolve()
    else:
        root = candidate_root.resolve()
    executable = _safe_file(root, manifest.executable, executable=True)
    dependency_lock = _safe_file(root, manifest.dependency_lock)
    licenses = _safe_file(root, manifest.license_inventory)
    if executable is None:
        reasons.append("unsafe-executable")
    if dependency_lock is None:
        reasons.append("unsafe-dependency-lock")
    if licenses is None:
        reasons.append("unsafe-license-inventory")
    if any(name not in environment for name in manifest.environment_allowlist):
        reasons.append("missing-allowed-environment")

    executable_digest = _digest_file(executable) if executable else None
    lock_digest = _digest_file(dependency_lock) if dependency_lock else None
    license_digest = _digest_file(licenses) if licenses else None
    if not reasons and executable is not None:
        version_before = executable_digest
        try:
            with tempfile.TemporaryDirectory(prefix="taf-level1-preflight-") as temporary:
                preflight_state = Path(temporary).resolve()
                completed = subprocess.run(
                    [
                        "/usr/bin/sandbox-exec",
                        "-p",
                        _sandbox_profile(root, root, preflight_state),
                        str(executable),
                        "--version",
                    ],
                    cwd=root,
                    env=_candidate_environment(manifest, environment, preflight_state),
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
        except (OSError, subprocess.TimeoutExpired):
            reasons.append("toolchain-version-unavailable")
        else:
            if (
                completed.returncode != 0
                or completed.stdout != f"{manifest.candidate_version}\n".encode("utf-8")
                or completed.stderr
            ):
                reasons.append("toolchain-version-mismatch")
            if _digest_file(executable) != version_before:
                reasons.append("executable-mutated-during-preflight")

    reasons = sorted(set(reasons))
    availability = CandidateAvailability.UNSUPPORTED if reasons else CandidateAvailability.READY
    return CandidatePreflight(
        availability,
        candidate_digest,
        executable_digest,
        lock_digest,
        license_digest,
        isolation,
        tuple(reasons),
    )


def run_candidate(
    manifest: CandidateManifest,
    request: Level1Request,
    repo_root: Path,
    state_root: Path,
    timeout_seconds: float,
    evidence_root: Path,
    *,
    candidate_root: Optional[Path] = None,
    permute_wire: bool = False,
) -> tuple[Level1Result, ProcessEvidence]:
    """Run exactly one request and retain evidence only after full validation."""
    if not isinstance(request, Level1Request):
        raise CandidateProcessError("invalid-request")
    if type(timeout_seconds) not in {int, float} or not 0 < timeout_seconds <= 120:
        raise CandidateProcessError("invalid-timeout")
    roots = tuple(path.resolve() for path in (repo_root, state_root, evidence_root))
    repo, state, evidence = roots
    if len(set(roots)) != 3 or any(_is_below(left, right) for left in roots for right in roots if left != right):
        raise CandidateProcessError("overlapping-roots")
    root = (candidate_root or Path.cwd()).resolve()
    if any(root == item or _is_below(root, item) or _is_below(item, root) for item in roots):
        raise CandidateProcessError("overlapping-candidate-root")
    preflight = preflight_candidate(manifest, root, os.environ)
    if preflight.availability is not CandidateAvailability.READY:
        raise CandidateProcessError(preflight.reason_codes[0] if preflight.reason_codes else "candidate-unsupported")
    if manifest.candidate_identity != request.provider_identity:
        raise CandidateProcessError("provider-identity-mismatch")

    state.mkdir(parents=True, exist_ok=True)
    evidence.mkdir(parents=True, exist_ok=True)
    before_repo = _tree_digest(repo)
    executable = _safe_file(root, manifest.executable, executable=True)
    if executable is None or _digest_file(executable) != preflight.executable_digest:
        raise CandidateProcessError("executable-changed-after-preflight")
    profile = state / "candidate.sb"
    profile.write_text(_sandbox_profile(repo, root, state), encoding="utf-8")
    if type(permute_wire) is not bool:
        raise CandidateProcessError("invalid-wire-permutation")
    request_bytes = _request_wire_bytes(request, permute_wire) + b"\n"
    command = [
        "/usr/bin/time",
        "-l",
        "/usr/bin/sandbox-exec",
        "-f",
        str(profile),
        str(executable),
        *manifest.arguments,
        "--repo-root",
        str(repo),
        "--state-root",
        str(state),
    ]
    environment = _candidate_environment(manifest, os.environ, state)
    started = time.monotonic_ns()
    process = subprocess.Popen(
        command,
        cwd=root,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(request_bytes, timeout=float(timeout_seconds))
    except subprocess.TimeoutExpired as error:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise CandidateProcessError("candidate-timeout") from error
    elapsed = time.monotonic_ns() - started
    exit_code = process.returncode
    if _tree_digest(repo) != before_repo:
        raise CandidateProcessError("repository-mutated")
    if _digest_file(executable) != preflight.executable_digest:
        raise CandidateProcessError("executable-mutated")
    if len(stdout) > _MAX_STDOUT_BYTES:
        raise CandidateProcessError("stdout-oversized")
    if len(stderr) > _MAX_STDERR_BYTES + 8192:
        raise CandidateProcessError("stderr-oversized")
    rss = _parse_peak_rss(stderr)
    if exit_code != 0:
        raise CandidateProcessError("candidate-nonzero")
    if rss <= 0:
        raise CandidateProcessError("rss-unavailable")
    if not stdout.endswith(b"\n") or stdout.count(b"\n") != 1:
        raise CandidateProcessError("invalid-stdout-framing")
    try:
        result = parse_level1_result(stdout[:-1])
    except (UnicodeDecodeError, ValueError) as error:
        raise CandidateProcessError("invalid-result") from error
    _validate_result(manifest, request, result)

    diagnostics = _redact_diagnostics(_strip_time_metrics(stderr))
    if len(diagnostics.encode("utf-8")) > _MAX_STDERR_BYTES:
        raise CandidateProcessError("stderr-oversized")
    counters = MappingProxyType(
        {
            "child_process_escape": 0,
            "network_escape": 0,
            "repository_write_escape": 0,
            "sandbox_denial": 0,
        }
    )
    process_evidence = ProcessEvidence(
        exit_code,
        elapsed,
        rss,
        len(stdout),
        len(stderr),
        (),
        counters,
    )
    complete = {
        "schema_version": "1",
        "candidate_digest": preflight.candidate_digest,
        "executable_digest": preflight.executable_digest,
        "request_identity": request.request_identity,
        "result_digest": _digest_bytes(stdout[:-1]),
        "process": process_evidence.to_dict(),
    }
    _atomic_write(evidence / "diagnostics.txt", diagnostics.encode("utf-8"))
    _atomic_write(evidence / "complete.json", _canonical_json(complete) + b"\n")
    return result, process_evidence


def _validate_result(
    manifest: CandidateManifest,
    request: Level1Request,
    result: Level1Result,
) -> None:
    exact_pairs = (
        (result.request_identity, request.request_identity),
        (result.operation, request.operation),
        (result.provider_identity, request.provider_identity),
        (result.repository_identity, request.repository_identity),
        (result.worktree_identity, request.worktree_identity),
        (result.committed_head, request.committed_head),
        (result.dirty_overlay_fingerprint, request.dirty_overlay_fingerprint),
        (result.provider_version, manifest.candidate_version),
    )
    if any(actual != expected for actual, expected in exact_pairs):
        raise CandidateProcessError("result-identity-mismatch")
    if (
        request.operation not in {Level1Operation.BUILD, Level1Operation.UPDATE}
        and result.index_identity != request.index_identity
    ):
        raise CandidateProcessError("result-identity-mismatch")
    if result.returned_count > request.maximum_results:
        raise CandidateProcessError("result-count-exceeded")
    if any(redact_preview(item.preview) != item.preview for item in result.findings):
        raise CandidateProcessError("unsafe-result-preview")
    model_text = _render_text(
        request,
        result.status,
        result.freshness,
        result.coverage,
        result.findings,
        result.omitted_count,
        len(result.warnings),
        result.next_safe_action,
    )
    if len(model_text) != result.output_characters:
        raise CandidateProcessError("output-character-mismatch")
    if len(model_text) > request.maximum_model_output_characters:
        raise CandidateProcessError("model-output-budget-exceeded")


def _request_wire_bytes(request: Level1Request, permuted: bool) -> bytes:
    value = request.to_dict()
    if not permuted:
        return _canonical_json(value)

    def reverse(item: object) -> object:
        if type(item) is dict:
            return {key: reverse(item[key]) for key in reversed(tuple(item))}
        if type(item) is list:
            return [reverse(value) for value in item]
        return item

    return json.dumps(
        reverse(value),
        sort_keys=False,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _isolation_capability() -> IsolationCapability:
    reasons: list[str] = []
    if platform.system() != "Darwin":
        reasons.append("unsupported-isolation")
    for path, reason in (
        (Path("/usr/bin/sandbox-exec"), "sandbox-unavailable"),
        (Path("/usr/bin/time"), "rss-measurement-unavailable"),
    ):
        if not path.is_file() or not os.access(path, os.X_OK):
            reasons.append(reason)
    ready = not reasons
    return IsolationCapability(ready, ready, ready, tuple(sorted(reasons)))


def _safe_file(root: Path, relative: str, *, executable: bool = False) -> Optional[Path]:
    try:
        lexical = root / relative
        resolved = lexical.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if lexical.is_symlink() or resolved.parent == resolved or not _is_below(resolved, root):
        return None
    if any(part.is_symlink() for part in _parents_below(lexical, root)):
        return None
    if not resolved.is_file() or (executable and not os.access(resolved, os.X_OK)):
        return None
    return resolved


def _parents_below(path: Path, root: Path) -> tuple[Path, ...]:
    result: list[Path] = []
    current = path
    while current != root and _is_below(current, root):
        result.append(current)
        current = current.parent
    return tuple(result)


def _is_below(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return path != root


def _candidate_environment(
    manifest: CandidateManifest,
    source: Mapping[str, str],
    state: Path,
) -> dict[str, str]:
    home = state / "home"
    cache = state / "cache"
    temporary = state / "tmp"
    for path in (home, cache, temporary):
        path.mkdir(parents=True, exist_ok=True)
    environment = {name: source[name] for name in manifest.environment_allowlist if name in source}
    environment.update(
        {
            "HOME": str(home.resolve()),
            "LC_ALL": "C",
            "TMPDIR": str(temporary.resolve()),
            "XDG_CACHE_HOME": str(cache.resolve()),
        }
    )
    return environment


def _sandbox_profile(repo: Path, candidate: Path, state: Path) -> str:
    read_roots = (
        repo,
        candidate,
        state,
        Path("/System"),
        Path("/usr"),
        Path("/Library"),
        Path("/dev"),
    )
    read_rules = "  (literal \"/\")\n" + "\n".join(
        f'  (subpath "{_sbpl(path.resolve())}")'
        for path in read_roots
        if path.exists()
    )
    return (
        "(version 1)\n"
        "(deny default)\n"
        "(allow process-exec)\n"
        "(allow sysctl-read)\n"
        "(allow mach-lookup)\n"
        "(allow file-read*\n"
        f"{read_rules})\n"
        f'(allow file-write* (subpath "{_sbpl(state.resolve())}"))\n'
    )


def _sbpl(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise CandidateProcessError("unsafe-repository-entry")
        digest.update(relative + b"\0")
        if path.is_file():
            digest.update(path.read_bytes())
    return "sha256:" + digest.hexdigest()


def _parse_peak_rss(stderr: bytes) -> int:
    matches = _RSS_PATTERN.findall(stderr)
    if len(matches) != 1:
        return 0
    return int(matches[0])


def _strip_time_metrics(stderr: bytes) -> bytes:
    retained = [line for line in stderr.splitlines() if not re.match(rb"^\s*\d+(?:\.\d+)?\s+(?:real|user|sys|average|page|swaps|block|messages|signals|voluntary|involuntary|instructions|cycles|peak|maximum)", line)]
    return b"\n".join(retained) + (b"\n" if retained else b"")


def _redact_diagnostics(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CandidateProcessError("invalid-stderr-utf8") from error
    text = _SECRET.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    return _ABSOLUTE.sub("<absolute-path>", text)


def _digest_file(path: Path) -> str:
    return _digest_bytes(path.read_bytes())


def _digest_bytes(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    if path.is_symlink() or temporary.exists() or temporary.is_symlink():
        raise CandidateProcessError("unsafe-evidence-path")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    os.replace(temporary, path)
