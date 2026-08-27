"""Strict portable records for active provider execution."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Type, TypeVar


_MAX_WIRE_BYTES = 256 * 1024
_MAX_STDOUT_BYTES = 256 * 1024
_MAX_STDERR_BYTES = 64 * 1024
_MAX_COLLECTION = 64
_MAX_STRING = 256
_MAX_COUNTER = 2**63 - 1
_IDENTITY = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_E = TypeVar("_E", bound=Enum)


class ProviderExecutionModelError(ValueError):
    """Raised when active-provider wire data is invalid."""

    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(f"invalid provider execution field: {field}")


class AdapterPhase(str, Enum):
    CANCEL = "cancel"
    DESCRIBE = "describe"
    ESTIMATE = "estimate"
    INSPECT = "inspect"
    METRICS = "metrics"
    QUERY = "query"
    UPDATE = "update"
    BUILD = "build"


class AdapterLocality(str, Enum):
    LOCAL = "local"
    NETWORK_BACKED = "network-backed"


class Readiness(str, Enum):
    READY = "ready"
    PARTIAL = "partial"
    CORRUPT = "corrupt"
    UNAVAILABLE = "unavailable"


class AttemptStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class AdapterManifest:
    schema_version: str
    adapter_identity: str
    adapter_version: str
    provider_identity: str
    provider_version: str
    executable: str
    arguments: tuple[str, ...]
    capabilities: tuple[str, ...]
    supported_phases: tuple[AdapterPhase, ...]
    environment_allowlist: tuple[str, ...]
    locality: AdapterLocality
    network_required: bool

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "AdapterManifest":
        _exact(value, cls.__dataclass_fields__, "adapter_manifest")
        return cls(
            _schema(value),
            _identity(value, "adapter_identity"),
            _text(value, "adapter_version"),
            _identity(value, "provider_identity"),
            _text(value, "provider_version"),
            _relative_path(value, "executable"),
            _texts(value, "arguments", sorted_values=False),
            _identities(value, "capabilities"),
            _enums(value, "supported_phases", AdapterPhase),
            _identities(value, "environment_allowlist", upper=True),
            _enum(value, "locality", AdapterLocality),
            _boolean(value, "network_required"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "adapter_identity": self.adapter_identity,
            "adapter_version": self.adapter_version,
            "provider_identity": self.provider_identity,
            "provider_version": self.provider_version,
            "executable": self.executable,
            "arguments": list(self.arguments),
            "capabilities": list(self.capabilities),
            "supported_phases": [item.value for item in self.supported_phases],
            "environment_allowlist": list(self.environment_allowlist),
            "locality": self.locality.value,
            "network_required": self.network_required,
        }


@dataclass(frozen=True)
class InspectionRecord:
    schema_version: str
    adapter_identity: str
    provider_identity: str
    provider_version: str
    repository_identity: str
    worktree_identity: str
    committed_head: str
    dirty_overlay_fingerprint: str
    index_identity: str
    readiness: Readiness
    capabilities: tuple[str, ...]
    path_coverage: float
    language_coverage: float
    storage_bytes: int
    reason_codes: tuple[str, ...]
    warnings: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "InspectionRecord":
        _exact(value, cls.__dataclass_fields__, "inspection_record")
        readiness = _enum(value, "readiness", Readiness)
        reasons = _texts(value, "reason_codes")
        if readiness in {Readiness.CORRUPT, Readiness.UNAVAILABLE} and not reasons:
            raise ProviderExecutionModelError("reason_codes")
        return cls(
            _schema(value),
            _identity(value, "adapter_identity"),
            _identity(value, "provider_identity"),
            _text(value, "provider_version"),
            _digest(value, "repository_identity"),
            _digest(value, "worktree_identity"),
            _object_id(value, "committed_head"),
            _digest(value, "dirty_overlay_fingerprint"),
            _digest(value, "index_identity"),
            readiness,
            _identities(value, "capabilities"),
            _unit_number(value, "path_coverage"),
            _unit_number(value, "language_coverage"),
            _counter(value, "storage_bytes"),
            reasons,
            _texts(value, "warnings"),
        )

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {}
        for field in self.__dataclass_fields__:
            item = getattr(self, field)
            if isinstance(item, Enum):
                result[field] = item.value
            elif isinstance(item, tuple):
                result[field] = list(item)
            else:
                result[field] = item
        return result


@dataclass(frozen=True)
class ExecutionPolicy:
    schema_version: str
    timeout_seconds: float
    maximum_stdout_bytes: int
    maximum_stderr_bytes: int
    network_allowed: bool
    fallback_allowed: bool
    maximum_inspections: int

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "ExecutionPolicy":
        _exact(value, cls.__dataclass_fields__, "execution_policy")
        timeout = _number(value, "timeout_seconds")
        stdout = _counter(value, "maximum_stdout_bytes")
        stderr = _counter(value, "maximum_stderr_bytes")
        inspections = _counter(value, "maximum_inspections")
        if not 0 < timeout <= 120:
            raise ProviderExecutionModelError("timeout_seconds")
        if not 0 < stdout <= _MAX_STDOUT_BYTES:
            raise ProviderExecutionModelError("maximum_stdout_bytes")
        if not 0 <= stderr <= _MAX_STDERR_BYTES:
            raise ProviderExecutionModelError("maximum_stderr_bytes")
        if not 0 <= inspections <= 3:
            raise ProviderExecutionModelError("maximum_inspections")
        return cls(
            _schema(value), timeout, stdout, stderr,
            _boolean(value, "network_allowed"),
            _boolean(value, "fallback_allowed"), inspections,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "timeout_seconds": self.timeout_seconds,
            "maximum_stdout_bytes": self.maximum_stdout_bytes,
            "maximum_stderr_bytes": self.maximum_stderr_bytes,
            "network_allowed": self.network_allowed,
            "fallback_allowed": self.fallback_allowed,
            "maximum_inspections": self.maximum_inspections,
        }


@dataclass(frozen=True)
class AttemptRecord:
    schema_version: str
    provider_identity: str
    phase: AdapterPhase
    status: AttemptStatus
    reason_codes: tuple[str, ...]
    elapsed_ns: int
    stdout_bytes: int
    stderr_bytes: int

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "AttemptRecord":
        _exact(value, cls.__dataclass_fields__, "attempt_record")
        return cls(
            _schema(value),
            _identity(value, "provider_identity"),
            _enum(value, "phase", AdapterPhase),
            _enum(value, "status", AttemptStatus),
            _texts(value, "reason_codes"),
            _counter(value, "elapsed_ns"),
            _counter(value, "stdout_bytes"),
            _counter(value, "stderr_bytes"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "provider_identity": self.provider_identity,
            "phase": self.phase.value,
            "status": self.status.value,
            "reason_codes": list(self.reason_codes),
            "elapsed_ns": self.elapsed_ns,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
        }


def parse_adapter_manifest(value: object) -> AdapterManifest:
    return AdapterManifest.from_dict(_parse_object(value, "adapter_manifest"))


def parse_inspection_record(value: object) -> InspectionRecord:
    return InspectionRecord.from_dict(_parse_object(value, "inspection_record"))


class _ParsedObject(dict[str, object]):
    duplicate: bool


def _no_duplicates(pairs: list[tuple[str, object]]) -> _ParsedObject:
    result = _ParsedObject()
    result.duplicate = False
    for key, value in pairs:
        if key in result:
            result.duplicate = True
        result[key] = value
    return result


def _bad_constant(_value: str) -> object:
    raise ProviderExecutionModelError("json")


def _parse_object(value: object, field: str) -> dict[str, object]:
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = value.encode("utf-8")
    else:
        raise ProviderExecutionModelError(field)
    if len(raw) > _MAX_WIRE_BYTES:
        raise ProviderExecutionModelError(field)
    try:
        parsed = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_no_duplicates,
            parse_constant=_bad_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ProviderExecutionModelError) as error:
        raise ProviderExecutionModelError(field) from error
    if type(parsed) is not _ParsedObject or parsed.duplicate:
        raise ProviderExecutionModelError(field)
    return dict(parsed)


def _exact(value: object, fields: object, field: str) -> None:
    if type(value) is not dict or set(value) != set(fields):
        raise ProviderExecutionModelError(field)


def _schema(value: dict[str, object]) -> str:
    if value.get("schema_version") != "1":
        raise ProviderExecutionModelError("schema_version")
    return "1"


def _text(value: dict[str, object], field: str) -> str:
    item = value.get(field)
    if type(item) is not str or not item or len(item) > _MAX_STRING or any(character in item for character in "\n\r\x00"):
        raise ProviderExecutionModelError(field)
    return item


def _identity(value: dict[str, object], field: str) -> str:
    item = _text(value, field)
    if not _IDENTITY.fullmatch(item):
        raise ProviderExecutionModelError(field)
    return item


def _relative_path(value: dict[str, object], field: str) -> str:
    item = _text(value, field)
    path = PurePosixPath(item)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ProviderExecutionModelError(field)
    return item


def _texts(value: dict[str, object], field: str, *, sorted_values: bool = True) -> tuple[str, ...]:
    items = value.get(field)
    if type(items) is not list or len(items) > _MAX_COLLECTION:
        raise ProviderExecutionModelError(field)
    parsed = tuple(_text({field: item}, field) for item in items)
    if len(set(parsed)) != len(parsed) or (sorted_values and parsed != tuple(sorted(parsed))):
        raise ProviderExecutionModelError(field)
    return parsed


def _identities(value: dict[str, object], field: str, *, upper: bool = False) -> tuple[str, ...]:
    items = _texts(value, field)
    pattern = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z") if upper else _IDENTITY
    if any(not pattern.fullmatch(item) for item in items):
        raise ProviderExecutionModelError(field)
    return items


def _enum(value: dict[str, object], field: str, enum_type: Type[_E]) -> _E:
    item = value.get(field)
    if type(item) is not str:
        raise ProviderExecutionModelError(field)
    try:
        return enum_type(item)
    except ValueError as error:
        raise ProviderExecutionModelError(field) from error


def _enums(value: dict[str, object], field: str, enum_type: Type[_E]) -> tuple[_E, ...]:
    items = value.get(field)
    if type(items) is not list or len(items) > _MAX_COLLECTION:
        raise ProviderExecutionModelError(field)
    parsed = tuple(_enum({field: item}, field, enum_type) for item in items)
    if len(set(parsed)) != len(parsed) or parsed != tuple(sorted(parsed, key=lambda item: item.value)):
        raise ProviderExecutionModelError(field)
    return parsed


def _boolean(value: dict[str, object], field: str) -> bool:
    item = value.get(field)
    if type(item) is not bool:
        raise ProviderExecutionModelError(field)
    return item


def _number(value: dict[str, object], field: str) -> float:
    item = value.get(field)
    if type(item) not in {int, float} or not math.isfinite(item):
        raise ProviderExecutionModelError(field)
    return float(item)


def _unit_number(value: dict[str, object], field: str) -> float:
    item = _number(value, field)
    if not 0 <= item <= 1:
        raise ProviderExecutionModelError(field)
    return item


def _counter(value: dict[str, object], field: str) -> int:
    item = value.get(field)
    if type(item) is not int or not 0 <= item <= _MAX_COUNTER:
        raise ProviderExecutionModelError(field)
    return item


def _digest(value: dict[str, object], field: str) -> str:
    item = _text(value, field)
    if not _DIGEST.fullmatch(item):
        raise ProviderExecutionModelError(field)
    return item


def _object_id(value: dict[str, object], field: str) -> str:
    item = _text(value, field)
    if not _OBJECT_ID.fullmatch(item):
        raise ProviderExecutionModelError(field)
    return item
