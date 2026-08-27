"""Strict host-local bindings for explicitly configured provider tools."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import re
import stat


_MAX_BINDING_BYTES = 64 * 1024
_MAX_COLLECTION = 64
_MAX_STRING = 4096
_IDENTITY = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ENVIRONMENT = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_SECRET_NAME = re.compile(
    r"(?:^|_)(?:API_?KEY|CREDENTIAL|PASSWORD|SECRET|TOKEN)(?:_|$)", re.I
)


class ProviderBindingError(ValueError):
    """Raised when a local provider binding is unsafe or malformed."""

    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(f"invalid provider binding: {field}")


class ProviderTransport(str, Enum):
    CLI_JSON = "cli-json"
    MCP_STDIO = "mcp-stdio"


@dataclass(frozen=True)
class AdapterBinding:
    schema_version: str
    adapter_identity: str
    provider_identity: str
    adapter_root: Path
    provider_executable: Path
    provider_arguments: tuple[str, ...]
    provider_state_roots: tuple[Path, ...]
    environment: tuple[tuple[str, str], ...]
    transport: ProviderTransport
    binding_digest: str

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "AdapterBinding":
        if type(value) is not dict or set(value) != set(cls.__dataclass_fields__):
            raise ProviderBindingError("binding")
        if value["schema_version"] != "1":
            raise ProviderBindingError("schema_version")
        adapter_identity = _identity(value, "adapter_identity")
        provider_identity = _identity(value, "provider_identity")
        adapter_root = _directory(value, "adapter_root")
        provider_executable = _executable(value, "provider_executable")
        arguments = _arguments(value, "provider_arguments")
        state_roots = _directories(value, "provider_state_roots")
        environment = _environment(value, "environment")
        try:
            transport = ProviderTransport(value["transport"])
        except (TypeError, ValueError) as error:
            raise ProviderBindingError("transport") from error
        digest = value["binding_digest"]
        if type(digest) is not str or _DIGEST.fullmatch(digest) is None:
            raise ProviderBindingError("binding_digest")
        _validate_disjoint_roots(adapter_root, state_roots)
        expected = _binding_digest(value)
        if digest != expected:
            raise ProviderBindingError("binding_digest")
        return cls(
            "1",
            adapter_identity,
            provider_identity,
            adapter_root,
            provider_executable,
            arguments,
            state_roots,
            environment,
            transport,
            digest,
        )

    def to_local_dict(self) -> dict[str, object]:
        """Return the host-local control shape; never use as portable output."""
        return {
            "schema_version": self.schema_version,
            "adapter_identity": self.adapter_identity,
            "provider_identity": self.provider_identity,
            "adapter_root": str(self.adapter_root),
            "provider_executable": str(self.provider_executable),
            "provider_arguments": list(self.provider_arguments),
            "provider_state_roots": [
                str(path) for path in self.provider_state_roots
            ],
            "environment": dict(self.environment),
            "transport": self.transport.value,
            "binding_digest": self.binding_digest,
        }


def parse_adapter_binding(raw: bytes) -> AdapterBinding:
    """Parse one bounded canonical-compatible host binding."""
    if not isinstance(raw, bytes) or len(raw) > _MAX_BINDING_BYTES:
        raise ProviderBindingError("binding")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_bad_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ProviderBindingError) as error:
        raise ProviderBindingError("binding") from error
    if type(value) is not _ParsedObject or value.duplicate:
        raise ProviderBindingError("binding")
    if _contains_duplicate(value):
        raise ProviderBindingError("binding")
    return AdapterBinding.from_dict(dict(value))


def read_adapter_binding(path: Path) -> AdapterBinding:
    """Read an owner-only, nonsymlinked binding without replacement races."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(os.fspath(path), flags)
    except OSError as error:
        raise ProviderBindingError("binding_file") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_BINDING_BYTES:
            raise ProviderBindingError("binding_file")
        if hasattr(os, "getuid") and before.st_uid != os.getuid():
            raise ProviderBindingError("binding_owner")
        if before.st_mode & 0o077:
            raise ProviderBindingError("binding_permissions")
        chunks = []
        remaining = _MAX_BINDING_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(8192, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > _MAX_BINDING_BYTES:
            raise ProviderBindingError("binding_file")
        after = os.fstat(descriptor)
        try:
            current = os.lstat(path)
        except OSError as error:
            raise ProviderBindingError("binding_file") from error
        if _file_identity(before) != _file_identity(after) or _file_identity(
            after
        ) != _file_identity(current):
            raise ProviderBindingError("binding_file")
        return parse_adapter_binding(raw)
    finally:
        os.close(descriptor)


def validate_binding_for_repository(
    binding: AdapterBinding, repository_root: Path
) -> None:
    """Reject bindings whose local roots overlap the target repository."""
    repository = _canonical_directory(repository_root, "repository_root")
    local_paths = (
        binding.adapter_root,
        binding.provider_executable,
        *binding.provider_state_roots,
    )
    if any(_overlaps(repository, path) for path in local_paths):
        raise ProviderBindingError("repository_root")


def _binding_digest(value: dict[str, object]) -> str:
    material = {key: item for key, item in value.items() if key != "binding_digest"}
    try:
        wire = json.dumps(
            material,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ProviderBindingError("binding_digest") from error
    return "sha256:" + hashlib.sha256(wire).hexdigest()


def _identity(value: dict[str, object], field: str) -> str:
    item = value[field]
    if type(item) is not str or _IDENTITY.fullmatch(item) is None:
        raise ProviderBindingError(field)
    return item


def _directory(value: dict[str, object], field: str) -> Path:
    item = value[field]
    if type(item) is not str or not 0 < len(item) <= _MAX_STRING:
        raise ProviderBindingError(field)
    return _resolved_directory(Path(item), field)


def _resolved_directory(path: Path, field: str) -> Path:
    if not path.is_absolute():
        raise ProviderBindingError(field)
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ProviderBindingError(field) from error
    if path != resolved or not stat.S_ISDIR(metadata.st_mode):
        raise ProviderBindingError(field)
    return resolved


def _canonical_directory(path: Path, field: str) -> Path:
    if not path.is_absolute():
        raise ProviderBindingError(field)
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ProviderBindingError(field) from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise ProviderBindingError(field)
    return resolved


def _executable(value: dict[str, object], field: str) -> Path:
    item = value[field]
    if type(item) is not str or not 0 < len(item) <= _MAX_STRING:
        raise ProviderBindingError(field)
    path = Path(item)
    if not path.is_absolute():
        raise ProviderBindingError(field)
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ProviderBindingError(field) from error
    if (
        path != resolved
        or not stat.S_ISREG(metadata.st_mode)
        or not os.access(resolved, os.X_OK)
    ):
        raise ProviderBindingError(field)
    return resolved


def _directories(value: dict[str, object], field: str) -> tuple[Path, ...]:
    items = value[field]
    if type(items) is not list or len(items) > _MAX_COLLECTION:
        raise ProviderBindingError(field)
    paths = []
    for item in items:
        if type(item) is not str:
            raise ProviderBindingError(field)
        paths.append(_resolved_directory(Path(item), field))
    result = tuple(paths)
    if tuple(str(path) for path in result) != tuple(
        sorted(str(path) for path in result)
    ):
        raise ProviderBindingError(field)
    if len(set(result)) != len(result):
        raise ProviderBindingError(field)
    return result


def _arguments(value: dict[str, object], field: str) -> tuple[str, ...]:
    items = value[field]
    if type(items) is not list or len(items) > _MAX_COLLECTION:
        raise ProviderBindingError(field)
    result = []
    for item in items:
        if type(item) is not str or len(item) > 512 or "\x00" in item:
            raise ProviderBindingError(field)
        result.append(item)
    return tuple(result)


def _environment(
    value: dict[str, object], field: str
) -> tuple[tuple[str, str], ...]:
    item = value[field]
    if type(item) not in {dict, _ParsedObject} or len(item) > _MAX_COLLECTION:
        raise ProviderBindingError(field)
    if list(item) != sorted(item):
        raise ProviderBindingError(field)
    result = []
    for name, content in item.items():
        if (
            _ENVIRONMENT.fullmatch(name) is None
            or _SECRET_NAME.search(name)
            or type(content) is not str
            or len(content) > 512
            or "\x00" in content
        ):
            raise ProviderBindingError(field)
        result.append((name, content))
    return tuple(result)


def _validate_disjoint_roots(
    adapter_root: Path, state_roots: tuple[Path, ...]
) -> None:
    roots = (adapter_root, *state_roots)
    if any(
        _overlaps(left, right)
        for index, left in enumerate(roots)
        for right in roots[index + 1 :]
    ):
        raise ProviderBindingError("provider_state_roots")


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


class _ParsedObject(dict[str, object]):
    duplicate: bool


def _unique_object(pairs: list[tuple[str, object]]) -> _ParsedObject:
    result = _ParsedObject()
    result.duplicate = False
    for key, value in pairs:
        if key in result:
            result.duplicate = True
        result[key] = value
    return result


def _contains_duplicate(value: object) -> bool:
    if type(value) is _ParsedObject:
        if value.duplicate:
            return True
        return any(_contains_duplicate(item) for item in value.values())
    if type(value) is list:
        return any(_contains_duplicate(item) for item in value)
    return False


def _bad_constant(_value: str) -> object:
    raise ProviderBindingError("binding")


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )
