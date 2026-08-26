"""Safe, bounded persistence for provider metadata and exact consent state."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Mapping

from .consent import AuthorizationLedger, ConsentDisposition
from .models import ContextAction, canonical_json
from .provider_models import ProjectRegistration, ProviderDescriptor


_MAX_CONTROL_BYTES = 256 * 1024
_MAX_AUDIT_BYTES = 1024 * 1024
_MAX_AUDIT_RECORDS = 1024
_MAX_PROVIDERS = 64
_AUDIT_FIELDS = frozenset(
    {
        "timestamp",
        "request_digest",
        "repository_fingerprint",
        "provider_identity",
        "provider_schema_version",
        "actions",
        "disposition",
        "operation",
    }
)
_REQUEST_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class StateError(ValueError):
    """A fail-closed state error identified by a stable reason code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class StatePaths:
    """Resolved user-local paths for all provider-control state."""

    root: Path
    providers: Path
    consent: Path
    audit: Path


def resolve_state_paths(
    environment: Mapping[str, str], platform_name: str, home: Path | str
) -> StatePaths:
    """Resolve state paths solely from the explicitly supplied inputs."""
    override = environment.get("TAF_STATE_HOME")
    if "TAF_STATE_HOME" in environment:
        if not override:
            raise StateError("state-home-unavailable")
        root = Path(override)
    elif platform_name == "darwin":
        root = Path(home) / "Library" / "Application Support" / "TAF" / "context"
    elif platform_name.startswith("linux"):
        xdg = environment.get("XDG_STATE_HOME")
        root = (Path(xdg) if xdg else Path(home) / ".local" / "state") / "taf" / "context"
    elif platform_name == "win32":
        local = environment.get("LOCALAPPDATA")
        if not local:
            raise StateError("state-home-unavailable")
        root = Path(local) / "TAF" / "context"
    else:
        raise StateError("state-home-unavailable")
    return StatePaths(root, root / "providers.json", root / "consent.json", root / "audit.jsonl")


def read_user_registry(paths: StatePaths) -> tuple[ProviderDescriptor, ...]:
    """Read a strict list of user-registered descriptors, or empty state."""
    raw = _read_path(paths.providers, _MAX_CONTROL_BYTES, "unsafe-state-file")
    if raw is None:
        return ()
    try:
        value = _load_json(raw)
        if type(value) is not list or len(value) > _MAX_PROVIDERS:
            raise ValueError
        providers = tuple(ProviderDescriptor.from_dict(item) for item in value)
        identities = tuple(item.provider_identity for item in providers)
        if identities != tuple(sorted(identities)) or len(set(identities)) != len(identities):
            raise ValueError
        return providers
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateError("provider-registry-invalid") from exc


def read_consent(paths: StatePaths) -> AuthorizationLedger:
    """Read strict schema-v2 consent, treating a missing file as empty state."""
    raw = _read_path(paths.consent, _MAX_CONTROL_BYTES, "unsafe-state-file")
    if raw is None:
        return AuthorizationLedger()
    try:
        value = _load_json(raw)
        return AuthorizationLedger.from_dict(value)
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateError("consent-corrupt") from exc


def read_project_registration(
    repo: Path | str, repository_identity: str
) -> ProjectRegistration | None:
    """Read only ``.taf/context/registration.json`` through no-follow dirfds."""
    directory_fds: list[int] = []
    file_fd: int | None = None
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fds.append(os.open(os.fspath(repo), flags))
        for component in (".taf", "context"):
            try:
                directory_fds.append(os.open(component, flags, dir_fd=directory_fds[-1]))
            except FileNotFoundError:
                return None
        file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            file_fd = os.open("registration.json", file_flags, dir_fd=directory_fds[-1])
        except FileNotFoundError:
            return None
        raw = _read_fd(
            file_fd,
            _MAX_CONTROL_BYTES,
            "unsafe-project-registration",
            lambda: os.stat(
                "registration.json", dir_fd=directory_fds[-1], follow_symlinks=False
            ),
        )
    except StateError:
        raise
    except OSError as exc:
        raise StateError("unsafe-project-registration") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)

    try:
        value = _load_json(raw)
        registration = ProjectRegistration.from_dict(value)
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateError("project-registration-invalid") from exc
    if registration.repository_identity != repository_identity:
        return None
    return registration


def write_consent(
    paths: StatePaths, ledger: AuthorizationLedger, audit_record: Mapping[str, object]
) -> None:
    """Install prepared consent first and its prepared audit second."""
    consent_data = _prepare_consent(ledger)
    audit_data = _prepare_audit(paths, audit_record)
    if _path_exists_without_following(paths.consent):
        read_consent(paths)
    _atomic_install(paths.consent, consent_data)
    _atomic_install(paths.audit, audit_data)


def append_audit(paths: StatePaths, record: Mapping[str, object]) -> None:
    """Append one metadata-only record through bounded atomic rotation."""
    _atomic_install(paths.audit, _prepare_audit(paths, record))


def _prepare_consent(ledger: AuthorizationLedger) -> bytes:
    try:
        value = ledger.to_dict()
        verified = AuthorizationLedger.from_dict(value)
        if verified != ledger:
            raise ValueError
        data = canonical_json(value).encode("utf-8")
    except (AttributeError, TypeError, ValueError) as exc:
        raise StateError("consent-invalid") from exc
    if len(data) > _MAX_CONTROL_BYTES:
        raise StateError("control-too-large")
    return data


def _prepare_audit(paths: StatePaths, record: Mapping[str, object]) -> bytes:
    new_record = _validate_audit_record(record)
    new_line = canonical_json(new_record).encode("utf-8")
    if len(new_line) > _MAX_AUDIT_BYTES:
        raise StateError("audit-too-large")
    previous = _read_path(paths.audit, _MAX_AUDIT_BYTES, "unsafe-state-file")
    records = _parse_audit(previous or b"")
    lines = [canonical_json(item).encode("utf-8") for item in records]
    lines.append(new_line)
    lines = lines[-_MAX_AUDIT_RECORDS:]
    total = sum(len(line) for line in lines)
    while lines and total > _MAX_AUDIT_BYTES:
        total -= len(lines.pop(0))
    if not lines:
        raise StateError("audit-too-large")
    return b"".join(lines)


def _parse_audit(data: bytes) -> list[dict[str, object]]:
    if not data:
        return []
    complete = data if data.endswith(b"\n") else data[: data.rfind(b"\n") + 1]
    records: list[dict[str, object]] = []
    try:
        for line in complete.splitlines():
            if not line:
                raise ValueError
            value = _load_json(line)
            records.append(_validate_audit_record(value))
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateError("audit-corrupt") from exc
    return records


def _validate_audit_record(record: object) -> dict[str, object]:
    try:
        if type(record) is not dict or set(record) != _AUDIT_FIELDS:
            raise ValueError
        timestamp = _timestamp(record["timestamp"])
        digest = record["request_digest"]
        repository = record["repository_fingerprint"]
        provider = record["provider_identity"]
        provider_schema = record["provider_schema_version"]
        actions = record["actions"]
        if not isinstance(digest, str) or not _REQUEST_DIGEST.fullmatch(digest):
            raise ValueError
        if any(not isinstance(item, str) or not item for item in (repository, provider, provider_schema)):
            raise ValueError
        if type(actions) is not list or not actions:
            raise ValueError
        normalized_actions = [ContextAction(item).value for item in actions]
        if normalized_actions != sorted(set(normalized_actions)):
            raise ValueError
        disposition = ConsentDisposition(record["disposition"]).value
        operation = record["operation"]
        if operation not in {"record", "revoke"}:
            raise ValueError
        return {
            "timestamp": timestamp,
            "request_digest": digest,
            "repository_fingerprint": repository,
            "provider_identity": provider,
            "provider_schema_version": provider_schema,
            "actions": normalized_actions,
            "disposition": disposition,
            "operation": operation,
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise StateError("audit-record-invalid") from exc


def _timestamp(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(candidate)
    if parsed.tzinfo is None:
        raise ValueError
    return value


def _load_json(data: bytes) -> object:
    return json.loads(
        data.decode("utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _read_path(path: Path, maximum: int, unsafe_code: str) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(os.fspath(path), flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StateError(unsafe_code) from exc
    try:
        return _read_fd(fd, maximum, unsafe_code, lambda: os.lstat(path))
    finally:
        os.close(fd)


def _read_fd(
    fd: int, maximum: int, unsafe_code: str, final_stat: Callable[[], os.stat_result]
) -> bytes:
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode):
        raise StateError(unsafe_code)
    if before.st_size > maximum:
        raise StateError("control-too-large")
    chunks: list[bytes] = []
    remaining = maximum + 1
    while remaining:
        chunk = os.read(fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) > maximum:
        raise StateError("control-too-large")
    after = os.fstat(fd)
    try:
        current = final_stat()
    except OSError as exc:
        raise StateError(unsafe_code) from exc
    identity = lambda item: (
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )
    if identity(before) != identity(after) or identity(after) != identity(current):
        raise StateError(unsafe_code)
    return data


def _path_exists_without_following(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True


def _ensure_state_directory(path: Path) -> int:
    missing: list[Path] = []
    cursor = path
    while True:
        try:
            metadata = os.lstat(cursor)
            if not stat.S_ISDIR(metadata.st_mode):
                raise StateError("unsafe-state-directory")
            break
        except FileNotFoundError:
            missing.append(cursor)
            parent = cursor.parent
            if parent == cursor:
                raise StateError("state-write-failed")
            cursor = parent
    try:
        for directory in reversed(missing):
            os.mkdir(directory, 0o700)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        directory_fd = os.open(os.fspath(path), flags)
        if os.name == "posix":
            os.fchmod(directory_fd, 0o700)
        return directory_fd
    except StateError:
        raise
    except OSError as exc:
        raise StateError("state-write-failed") from exc


def _atomic_install(path: Path, data: bytes) -> None:
    directory_fd: int | None = None
    temporary_fd: int | None = None
    temporary_path: str | None = None
    try:
        directory_fd = _ensure_state_directory(path.parent)
        temporary_fd, temporary_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        if os.name == "posix":
            os.fchmod(temporary_fd, 0o600)
        view = memoryview(data)
        while view:
            written = os.write(temporary_fd, view)
            if written <= 0:
                raise OSError("short state write")
            view = view[written:]
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = None
        os.replace(temporary_path, path)
        temporary_path = None
        os.fsync(directory_fd)
    except StateError:
        raise
    except (OSError, ValueError) as exc:
        raise StateError("state-write-failed") from exc
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
        if directory_fd is not None:
            os.close(directory_fd)
