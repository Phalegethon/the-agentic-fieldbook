"""Command-line entry points for native Level 0 context artifacts."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys
import time
from typing import Callable, Mapping, Sequence, TextIO

from .dossier import build_dossier
from .freshness import (
    FreshnessAssessment,
    FreshnessExpectation,
    HeadRelation,
    assess_freshness,
)
from .git_snapshot import (
    SnapshotError,
    _git_environment,
    collect_snapshot,
    manifest_from_snapshot,
)
from .models import (
    BackgroundState,
    ContextManifest,
    ManifestError,
    RepositorySnapshot,
    canonical_json,
)
from .impact_hook import run_hook
from .prepare_cli import register_prepare_command, run_prepare_command
from .recovery import RecoveryError
from .recovery_cli import register_recovery_command, run_recovery_command


_DEFAULT_MAX_OUTPUT_CHARS = 12000
_DEFAULT_MAX_DIRTY_FILE_BYTES = 8 * 1024 * 1024
_MAX_INCREMENTAL_CHANGED_PATHS = 1000
_GIT_TIMEOUT_SECONDS = 20
_OBJECT_ID = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")


class CLIError(RuntimeError):
    """A concise, user-facing command error."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CLIError(message)


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    utc_clock: Callable[[], datetime] | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    """Run one command and return a process-style exit code."""
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    utc_clock = _utc_now if utc_clock is None else utc_clock
    environment = os.environ if environment is None else environment
    try:
        args = _parser().parse_args(argv)
        if args.command == "snapshot":
            result = _snapshot_command(
                repo=Path(args.repo),
                output_dir=Path(args.output_dir),
                max_output_chars=args.max_output_chars,
                max_dirty_file_bytes=args.max_dirty_file_bytes,
                utc_clock=utc_clock,
            )
        elif args.command == "status":
            result = _status_command(
                repo=Path(args.repo), manifest_path=Path(args.manifest)
            )
        elif args.command == "prepare":
            if args.prepare_command == "hook" and args.hook_command == "run":
                # The hook is advisory: it writes its own lines to stderr,
                # never answers with JSON, and always exits 0, so it returns
                # before the stdout writer below.
                return run_hook(
                    Path(args.repo),
                    environment=environment,
                    stderr=stderr,
                    verbose=args.verbose,
                )
            result = run_prepare_command(
                args,
                environment=environment,
                utc_clock=utc_clock,
            )
        elif args.command == "recover":
            stdout.write(run_recovery_command(args))
            return 0
        else:  # pragma: no cover - argparse requires a command.
            raise CLIError("a command is required")
        stdout.write(canonical_json(result))
        return 0
    except json.JSONDecodeError:
        _write_error(stderr, "invalid manifest JSON")
    except UnicodeError:
        _write_error(stderr, "invalid UTF-8 data")
    except (CLIError, SnapshotError, RecoveryError, ManifestError, ValueError) as exc:
        _write_error(stderr, str(exc))
    except OSError:
        _write_error(stderr, "artifact operation failed")
    return 2


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="taf_context", add_help=True)
    commands = parser.add_subparsers(dest="command", required=True)

    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--repo", required=True)
    snapshot.add_argument("--output-dir", required=True)
    snapshot.add_argument(
        "--max-output-chars", type=int, default=_DEFAULT_MAX_OUTPUT_CHARS
    )
    snapshot.add_argument(
        "--max-dirty-file-bytes",
        type=int,
        default=_DEFAULT_MAX_DIRTY_FILE_BYTES,
    )

    status = commands.add_parser("status")
    status.add_argument("--repo", required=True)
    status.add_argument("--manifest", required=True)
    register_recovery_command(commands)
    register_prepare_command(commands)
    return parser


def _snapshot_command(
    *,
    repo: Path,
    output_dir: Path,
    max_output_chars: int,
    max_dirty_file_bytes: int,
    utc_clock: Callable[[], datetime],
) -> dict[str, object]:
    if not 1024 <= max_output_chars <= 12000:
        raise CLIError("output character budget must be between 1024 and 12000")
    if max_dirty_file_bytes < 0:
        raise CLIError("dirty file byte ceiling must be non-negative")

    snapshot = collect_snapshot(repo, max_dirty_file_bytes=max_dirty_file_bytes)
    output = _validate_output_dir(output_dir, Path(snapshot.canonical_root))
    created_at = _rfc3339(utc_clock())
    initial_manifest = manifest_from_snapshot(snapshot, created_at)
    initial_assessment = _new_snapshot_assessment(initial_manifest, snapshot)
    dossier = build_dossier(
        snapshot, initial_assessment, max_chars=max_output_chars
    )
    snapshot_text = canonical_json(asdict(snapshot))
    storage_bytes = len(snapshot_text.encode("utf-8")) + len(
        dossier.markdown.encode("utf-8")
    )
    manifest = manifest_from_snapshot(snapshot, created_at, storage_bytes)
    manifest_text = canonical_json(manifest.to_dict())

    _install_artifacts(
        output,
        (
            ("snapshot.json", snapshot_text.encode("utf-8")),
            ("dossier.md", dossier.markdown.encode("utf-8")),
            ("manifest.json", manifest_text.encode("utf-8")),
        ),
    )
    return {
        "artifacts": {
            "dossier": str(output / "dossier.md"),
            "manifest": str(output / "manifest.json"),
            "snapshot": str(output / "snapshot.json"),
        },
        "freshness": initial_assessment.freshness.value,
        "path_coverage": manifest.path_coverage,
        "storage_bytes": storage_bytes,
        "dossier_characters": dossier.characters_used,
    }


def _status_command(*, repo: Path, manifest_path: Path) -> dict[str, object]:
    manifest = _read_manifest(manifest_path)
    current = collect_snapshot(repo)
    expected = manifest_from_snapshot(current, manifest.updated_at)
    relation, changed_path_count = _head_relation(
        Path(current.canonical_root), manifest.head_sha, current.head_sha
    )
    provider_compatible = (
        manifest.provider_name == expected.provider_name
        and manifest.provider_version == expected.provider_version
        and manifest.index_levels == expected.index_levels
        and manifest.capabilities == expected.capabilities
        and manifest.background_state is BackgroundState.READY
    )
    expectation = FreshnessExpectation(
        repository_authorized=True,
        provider_compatible=provider_compatible,
        provider_schema_version=expected.provider_schema_version,
        include_rules_hash=expected.include_rules_hash,
        exclude_rules_hash=expected.exclude_rules_hash,
        required_path_coverage=1.0,
        head_relation=relation,
        changed_path_count=changed_path_count,
        maximum_changed_path_count=_MAX_INCREMENTAL_CHANGED_PATHS,
        dirty_state_proven=True,
        manifest_is_corrupt=False,
    )
    assessment = assess_freshness(manifest, current, expectation)
    return _assessment_dict(assessment)


def _new_snapshot_assessment(
    manifest: ContextManifest, snapshot: RepositorySnapshot
) -> FreshnessAssessment:
    expectation = FreshnessExpectation(
        repository_authorized=True,
        provider_compatible=True,
        provider_schema_version=manifest.provider_schema_version,
        include_rules_hash=manifest.include_rules_hash,
        exclude_rules_hash=manifest.exclude_rules_hash,
        required_path_coverage=1.0,
        head_relation=HeadRelation.MATCHES,
        changed_path_count=None,
        maximum_changed_path_count=_MAX_INCREMENTAL_CHANGED_PATHS,
        dirty_state_proven=True,
        manifest_is_corrupt=False,
    )
    return assess_freshness(manifest, snapshot, expectation)


def _assessment_dict(assessment: FreshnessAssessment) -> dict[str, object]:
    return {
        "freshness": assessment.freshness.value,
        "reasons": list(assessment.reason_codes),
        "can_incrementally_update": assessment.can_incrementally_update,
        "requires_rebuild": assessment.requires_rebuild,
    }


def _validate_output_dir(output_dir: Path, repository_root: Path) -> Path:
    output = output_dir.resolve(strict=False)
    repository = repository_root.resolve()
    try:
        output.relative_to(repository)
    except ValueError:
        pass
    else:
        raise CLIError("output directory must be outside the repository")
    if output.exists():
        if not output.is_dir():
            raise CLIError("output path must be a directory")
        if any(output.iterdir()):
            raise CLIError("output directory must be empty")
    return output


def _install_artifacts(
    output: Path, artifacts: tuple[tuple[str, bytes], ...]
) -> None:
    import tempfile  # Level 0 artifact installation only

    if not artifacts:
        raise CLIError("no artifacts to install")
    output.mkdir(parents=True, exist_ok=True)
    temporary_paths: list[Path] = []
    ready_marker = output / artifacts[-1][0]
    ready_marker_installed = False
    ready_marker_durable = False
    try:
        for index, (name, content) in enumerate(artifacts):
            descriptor, raw_path = tempfile.mkstemp(
                prefix=f".{name}.", suffix=".tmp", dir=output
            )
            temporary = Path(raw_path)
            temporary_paths.append(temporary)
            with os.fdopen(descriptor, "wb") as target:
                target.write(content)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, output / name)
            if index == len(artifacts) - 2:
                _fsync_directory(output)
            if index == len(artifacts) - 1:
                ready_marker_installed = True
                _fsync_directory(output)
                ready_marker_durable = True
    finally:
        if ready_marker_installed and not ready_marker_durable:
            try:
                ready_marker.unlink()
            except FileNotFoundError:
                pass
            else:
                try:
                    _fsync_directory(output)
                except OSError:
                    pass
        for temporary in temporary_paths:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_manifest(path: Path) -> ContextManifest:
    raw = path.read_text(encoding="utf-8")
    value = json.loads(raw, object_pairs_hook=_strict_object)
    if type(value) is not dict:
        raise ManifestError("manifest")
    return ContextManifest.from_dict(value)


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError(key)
        result[key] = value
    return result


def _head_relation(
    repo: Path, manifest_head: str | None, current_head: str | None
) -> tuple[HeadRelation, int | None]:
    if manifest_head == current_head:
        return HeadRelation.MATCHES, None
    if (
        manifest_head is None
        or current_head is None
        or not _OBJECT_ID.fullmatch(manifest_head)
        or not _OBJECT_ID.fullmatch(current_head)
    ):
        return HeadRelation.UNKNOWN, None
    result = _local_git(repo, "merge-base", "--is-ancestor", manifest_head, current_head)
    if result.returncode == 0:
        count = _bounded_changed_path_count(
            repo,
            manifest_head,
            current_head,
            maximum=_MAX_INCREMENTAL_CHANGED_PATHS,
        )
        if count is None:
            return HeadRelation.UNKNOWN, None
        return HeadRelation.FORWARD, count
    if result.returncode == 1:
        return HeadRelation.DIVERGED, None
    return HeadRelation.UNKNOWN, None


def _local_git(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            env=_git_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CLIError("local Git command failed") from exc


def _bounded_changed_path_count(
    repo: Path,
    manifest_head: str,
    current_head: str,
    *,
    maximum: int,
) -> int | None:
    command = [
        "git",
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--name-only",
        "-z",
        manifest_head,
        current_head,
        "--",
    ]
    try:
        process = subprocess.Popen(
            command,
            cwd=repo,
            env=_git_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.stdout is None:  # pragma: no cover - PIPE guarantees this.
            raise CLIError("local Git command failed")
        count = 0
        pending = b""
        bounded = False
        malformed = False
        deadline = time.monotonic() + _GIT_TIMEOUT_SECONDS

        def consume(chunk: bytes) -> bool:
            nonlocal count, malformed, pending
            pending += chunk
            fields = pending.split(b"\x00")
            pending = fields.pop()
            for field in fields:
                if not field:
                    malformed = True
                    return False
                field.decode("utf-8", "surrogateescape")
                count += 1
                if count > maximum:
                    return False
            return True

        try:
            stdout_descriptor = process.stdout.fileno()
        except (AttributeError, OSError):  # Test doubles have no OS descriptor.
            while True:
                chunk = process.stdout.read(8192)
                if not chunk:
                    break
                if not consume(chunk):
                    bounded = count > maximum
                    break
        else:
            stderr = process.stderr
            stderr_descriptor = None if stderr is None else stderr.fileno()
            os.set_blocking(stdout_descriptor, False)
            if stderr_descriptor is not None:
                os.set_blocking(stderr_descriptor, False)
            with selectors.DefaultSelector() as selector:
                selector.register(stdout_descriptor, selectors.EVENT_READ, "stdout")
                if stderr_descriptor is not None:
                    selector.register(stderr_descriptor, selectors.EVENT_READ, "stderr")
                stdout_open = True
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(command, _GIT_TIMEOUT_SECONDS)
                    events = selector.select(remaining)
                    if not events:
                        raise subprocess.TimeoutExpired(command, _GIT_TIMEOUT_SECONDS)
                    for key, _mask in events:
                        chunk = os.read(key.fd, 8192)
                        if not chunk:
                            selector.unregister(key.fd)
                            if key.data == "stdout":
                                stdout_open = False
                            continue
                        if key.data == "stdout" and not consume(chunk):
                            bounded = count > maximum
                            break
                    if bounded or malformed:
                        break
        if malformed:
            _terminate_process(process)
            return None
        if bounded:
            _terminate_process(process)
            return maximum + 1
        remaining = max(0.0, deadline - time.monotonic())
        _stdout, _stderr = process.communicate(timeout=remaining)
        if process.returncode != 0 or pending:
            return None
        return count
    except subprocess.TimeoutExpired as exc:
        if "process" in locals():
            _terminate_process(process)
        raise CLIError("local Git command failed") from exc
    except OSError as exc:
        raise CLIError("local Git command failed") from exc


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        process.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()


def _rfc3339(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CLIError("UTC clock returned an invalid time")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _write_error(stderr: TextIO, message: str) -> None:
    concise = " ".join(message.splitlines()).strip() or "command failed"
    stderr.write(f"error: {concise}\n")
