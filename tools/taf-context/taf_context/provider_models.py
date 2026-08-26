"""Strict, portable wire records for provider discovery and routing."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Type, TypeVar

from .models import Confidence, ContextAction, Freshness, canonical_json


class ProviderModelError(ValueError):
    """Raised when a provider-control-plane record is not portable wire data."""

    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(f"invalid provider record field: {field}")


class ProviderLocality(str, Enum):
    LOCAL = "local"
    NETWORK_BACKED = "network-backed"


class DiscoverySource(str, Enum):
    NATIVE = "native"
    HOST_INVENTORY = "host-inventory"
    USER_REGISTRY = "user-registry"
    PROJECT_REGISTRATION = "project-registration"
    PROJECT_MARKER = "project-marker"


class Availability(str, Enum):
    CANDIDATE = "candidate"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class Registration(str, Enum):
    NATIVE = "native"
    PROJECT_DECLARED = "project-declared"
    USER_REGISTERED = "user-registered"
    UNREGISTERED = "unregistered"


class StatusEvidence(str, Enum):
    UNINSPECTED = "uninspected"
    MANIFEST_VALIDATED = "manifest-validated"
    PROVIDER_INSPECTED = "provider-inspected"


class RoutingStatus(str, Enum):
    SELECTED = "selected"
    NATIVE_FALLBACK = "native-fallback"
    CONSENT_REQUIRED = "consent-required"
    INSUFFICIENT_CONTEXT = "insufficient-context"


_IDENTITY = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MAX_STRING = 256
_MAX_COLLECTION = 64
_MAX_MARKERS = 16
_MAX_COUNTER = 2**31 - 1
_E = TypeVar("_E", bound=Enum)


@dataclass(frozen=True)
class ProviderDescriptor:
    schema_version: str
    provider_identity: str
    provider_version: str
    provider_schema_version: str
    capabilities: tuple[str, ...]
    locality: ProviderLocality
    discovery_sources: tuple[DiscoverySource, ...]
    availability: Availability
    registration: Registration
    status_evidence: StatusEvidence
    freshness: Freshness
    path_coverage: float | None
    language_coverage: float | None
    latency_ms: float | None
    confidence: Confidence
    supported_actions: tuple[ContextAction, ...]
    required_actions: tuple[ContextAction, ...]
    marker_hints: tuple[str, ...]
    reason_codes: tuple[str, ...]
    warnings: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "ProviderDescriptor":
        _exact(value, cls.__dataclass_fields__, "provider_descriptor")
        return cls(
            schema_version=_schema(value),
            provider_identity=_identity(value, "provider_identity"),
            provider_version=_string(value, "provider_version"),
            provider_schema_version=_string(value, "provider_schema_version"),
            capabilities=_identities(value, "capabilities"),
            locality=_enum(value, "locality", ProviderLocality),
            discovery_sources=_enums(value, "discovery_sources", DiscoverySource),
            availability=_enum(value, "availability", Availability),
            registration=_enum(value, "registration", Registration),
            status_evidence=_enum(value, "status_evidence", StatusEvidence),
            freshness=_enum(value, "freshness", Freshness),
            path_coverage=_optional_number(value, "path_coverage", unit=True),
            language_coverage=_optional_number(value, "language_coverage", unit=True),
            latency_ms=_optional_number(value, "latency_ms"),
            confidence=_enum(value, "confidence", Confidence),
            supported_actions=_enums(value, "supported_actions", ContextAction),
            required_actions=_enums(value, "required_actions", ContextAction),
            marker_hints=_markers(value, "marker_hints"),
            reason_codes=_strings(value, "reason_codes"),
            warnings=_strings(value, "warnings"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version, "provider_identity": self.provider_identity,
            "provider_version": self.provider_version, "provider_schema_version": self.provider_schema_version,
            "capabilities": list(self.capabilities), "locality": self.locality.value,
            "discovery_sources": [item.value for item in self.discovery_sources],
            "availability": self.availability.value, "registration": self.registration.value,
            "status_evidence": self.status_evidence.value, "freshness": self.freshness.value,
            "path_coverage": self.path_coverage, "language_coverage": self.language_coverage,
            "latency_ms": self.latency_ms, "confidence": self.confidence.value,
            "supported_actions": [item.value for item in self.supported_actions],
            "required_actions": [item.value for item in self.required_actions],
            "marker_hints": list(self.marker_hints), "reason_codes": list(self.reason_codes),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class HostInventory:
    schema_version: str
    providers: tuple[ProviderDescriptor, ...]
    rejected_provider_count: int
    rejection_summaries: tuple[str, ...]
    omitted_provider_count: int
    partial: bool

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "HostInventory":
        _exact(value, cls.__dataclass_fields__, "host_inventory")
        return cls(_schema(value), _providers(value, "providers"),
                   _counter(value, "rejected_provider_count"),
                   _strings(value, "rejection_summaries"),
                   _counter(value, "omitted_provider_count"), _boolean(value, "partial"))

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": self.schema_version, "providers": [item.to_dict() for item in self.providers],
                "rejected_provider_count": self.rejected_provider_count,
                "rejection_summaries": list(self.rejection_summaries),
                "omitted_provider_count": self.omitted_provider_count, "partial": self.partial}


@dataclass(frozen=True)
class HostInventoryParseResult:
    inventory: HostInventory
    input_bytes: int


def parse_host_inventory(value: object) -> HostInventoryParseResult:
    """Parse an inventory envelope while safely isolating bad descriptors."""
    if isinstance(value, bytes):
        raw = value
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProviderModelError("inventory") from exc
    elif isinstance(value, str):
        text, raw = value, value.encode("utf-8")
    elif type(value) is dict:
        text = canonical_json(value)
        raw = text.encode("utf-8")
    else:
        raise ProviderModelError("inventory")
    try:
        envelope = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_bad_constant)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError, ProviderModelError) as exc:
        raise ProviderModelError("inventory") from exc
    if type(envelope) is not _ParsedObject or envelope.duplicate:
        raise ProviderModelError("inventory")
    envelope = dict(envelope)
    _exact(envelope, HostInventory.__dataclass_fields__, "host_inventory")
    _schema(envelope)
    entries = envelope["providers"]
    if type(entries) is not list or len(entries) > _MAX_COLLECTION:
        raise ProviderModelError("providers")
    rejected_count = _counter(envelope, "rejected_provider_count")
    summaries = list(_strings(envelope, "rejection_summaries"))
    omitted_count = _counter(envelope, "omitted_provider_count")
    partial = _boolean(envelope, "partial")
    providers: list[ProviderDescriptor] = []
    for index, entry in enumerate(entries):
        try:
            providers.append(ProviderDescriptor.from_dict(entry))
        except (ProviderModelError, ValueError, TypeError):
            rejected_count += 1
            if rejected_count > _MAX_COUNTER or len(summaries) >= _MAX_COLLECTION:
                raise ProviderModelError("rejected_provider_count")
            summaries.append(f"invalid-provider-{index:02d}")
            partial = True
    providers_tuple = _ordered_providers(providers, "providers")
    summaries_tuple = tuple(sorted(set(summaries)))
    if len(summaries_tuple) != len(summaries):
        raise ProviderModelError("rejection_summaries")
    inventory = HostInventory("1", providers_tuple, rejected_count, summaries_tuple, omitted_count, partial)
    return HostInventoryParseResult(inventory, len(raw))


@dataclass(frozen=True)
class ProjectRegistrationEntry:
    provider_identity: str
    provider_schema_version: str
    required_capabilities: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "ProjectRegistrationEntry":
        _exact(value, cls.__dataclass_fields__, "project_registration_entry")
        return cls(_identity(value, "provider_identity"), _string(value, "provider_schema_version"),
                   _identities(value, "required_capabilities"))

    def to_dict(self) -> dict[str, object]:
        return {"provider_identity": self.provider_identity, "provider_schema_version": self.provider_schema_version,
                "required_capabilities": list(self.required_capabilities)}


@dataclass(frozen=True)
class ProjectRegistration:
    schema_version: str
    repository_identity: str
    providers: tuple[ProjectRegistrationEntry, ...]

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "ProjectRegistration":
        _exact(value, cls.__dataclass_fields__, "project_registration")
        entries = value["providers"]
        if type(entries) is not list or len(entries) > _MAX_COLLECTION:
            raise ProviderModelError("providers")
        parsed = [ProjectRegistrationEntry.from_dict(item) for item in entries]
        if tuple(item.provider_identity for item in parsed) != tuple(sorted(item.provider_identity for item in parsed)) or len({item.provider_identity for item in parsed}) != len(parsed):
            raise ProviderModelError("providers")
        return cls(_schema(value), _string(value, "repository_identity"), tuple(parsed))

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": self.schema_version, "repository_identity": self.repository_identity,
                "providers": [item.to_dict() for item in self.providers]}


@dataclass(frozen=True)
class DiscoverySnapshot:
    schema_version: str
    repository_identity: str
    worktree_identity: str
    inventory_fingerprint: str
    providers: tuple[ProviderDescriptor, ...]
    rejected_provider_count: int
    rejection_summaries: tuple[str, ...]
    omitted_provider_count: int
    partial: bool
    warnings: tuple[str, ...]
    input_bytes: int
    output_bytes: int

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "DiscoverySnapshot":
        _exact(value, cls.__dataclass_fields__, "discovery_snapshot")
        return cls(_schema(value), _string(value, "repository_identity"), _string(value, "worktree_identity"),
                   _string(value, "inventory_fingerprint"), _providers(value, "providers"),
                   _counter(value, "rejected_provider_count"), _strings(value, "rejection_summaries"),
                   _counter(value, "omitted_provider_count"), _boolean(value, "partial"),
                   _strings(value, "warnings"), _counter(value, "input_bytes"), _counter(value, "output_bytes"))

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": self.schema_version, "repository_identity": self.repository_identity,
                "worktree_identity": self.worktree_identity, "inventory_fingerprint": self.inventory_fingerprint,
                "providers": [item.to_dict() for item in self.providers], "rejected_provider_count": self.rejected_provider_count,
                "rejection_summaries": list(self.rejection_summaries), "omitted_provider_count": self.omitted_provider_count,
                "partial": self.partial, "warnings": list(self.warnings), "input_bytes": self.input_bytes,
                "output_bytes": self.output_bytes}


@dataclass(frozen=True)
class BrokerRequest:
    schema_version: str
    consumer_identity: str
    repository_identity: str
    worktree_identity: str
    required_capability: str
    minimum_freshness: Freshness
    minimum_path_coverage: float | None
    minimum_language_coverage: float | None
    network_acceptable: bool
    maximum_latency_ms: float | None
    maximum_machine_output_bytes: int
    maximum_model_output_characters: int
    preferred_provider: str | None

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "BrokerRequest":
        _exact(value, cls.__dataclass_fields__, "broker_request")
        return cls(_schema(value), _string(value, "consumer_identity"), _string(value, "repository_identity"),
                   _string(value, "worktree_identity"), _identity(value, "required_capability"),
                   _enum(value, "minimum_freshness", Freshness), _optional_number(value, "minimum_path_coverage", unit=True),
                   _optional_number(value, "minimum_language_coverage", unit=True), _boolean(value, "network_acceptable"),
                   _optional_number(value, "maximum_latency_ms"), _counter(value, "maximum_machine_output_bytes"),
                   _counter(value, "maximum_model_output_characters"), _optional_identity(value, "preferred_provider"))

    def to_dict(self) -> dict[str, object]:
        return {field: (getattr(self, field).value if isinstance(getattr(self, field), Enum) else getattr(self, field)) for field in self.__dataclass_fields__}


@dataclass(frozen=True)
class ConsentRequest:
    schema_version: str
    repository_identity: str
    provider_identity: str
    provider_schema_version: str
    actions: tuple[ContextAction, ...]
    locality: ProviderLocality
    data_surface: str
    fallback: str
    requested_at: str
    digest: str

    @classmethod
    def create(cls, **kwargs: object) -> "ConsentRequest":
        fields = set(cls.__dataclass_fields__) - {"digest"}
        if set(kwargs) != fields:
            raise ProviderModelError("consent_request")
        base = dict(kwargs)
        actions = base.get("actions")
        if isinstance(actions, tuple):
            base["actions"] = [item.value if isinstance(item, ContextAction) else item for item in actions]
        locality = base.get("locality")
        if isinstance(locality, ProviderLocality):
            base["locality"] = locality.value
        base["digest"] = "0" * 64
        candidate = cls.from_dict(base, verify_digest=False)
        digest = _consent_digest(candidate.to_dict())
        return cls(**{**candidate.__dict__, "digest": digest})

    @classmethod
    def from_dict(cls, value: dict[str, object], verify_digest: bool = True) -> "ConsentRequest":
        _exact(value, cls.__dataclass_fields__, "consent_request")
        digest = _string(value, "digest")
        if not _DIGEST.fullmatch(digest):
            raise ProviderModelError("digest")
        result = cls(_schema(value), _string(value, "repository_identity"), _identity(value, "provider_identity"),
                     _string(value, "provider_schema_version"), _enums(value, "actions", ContextAction),
                     _enum(value, "locality", ProviderLocality), _string(value, "data_surface"), _string(value, "fallback"),
                     _string(value, "requested_at"), digest)
        if verify_digest and _consent_digest(result.to_dict()) != digest:
            raise ProviderModelError("digest")
        return result

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": self.schema_version, "repository_identity": self.repository_identity,
                "provider_identity": self.provider_identity, "provider_schema_version": self.provider_schema_version,
                "actions": [item.value for item in self.actions], "locality": self.locality.value,
                "data_surface": self.data_surface, "fallback": self.fallback, "requested_at": self.requested_at,
                "digest": self.digest}


@dataclass(frozen=True)
class RejectedAlternative:
    provider_identity: str
    reason_codes: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "RejectedAlternative":
        _exact(value, cls.__dataclass_fields__, "rejected_alternative")
        return cls(_identity(value, "provider_identity"), _strings(value, "reason_codes"))

    def to_dict(self) -> dict[str, object]:
        return {"provider_identity": self.provider_identity, "reason_codes": list(self.reason_codes)}


@dataclass(frozen=True)
class RoutingDecision:
    schema_version: str
    status: RoutingStatus
    selected_provider: str | None
    selection_reason_codes: tuple[str, ...]
    rejected_alternatives: tuple[RejectedAlternative, ...]
    eligible_count: int
    rejected_count: int
    omitted_count: int
    consent_requests: tuple[ConsentRequest, ...]
    escalation_required: bool
    next_safe_action: str
    model_summary: str
    output_bytes: int
    output_characters: int

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "RoutingDecision":
        _exact(value, cls.__dataclass_fields__, "routing_decision")
        alternatives = _alternatives(value, "rejected_alternatives")
        consents = _consents(value, "consent_requests")
        return cls(_schema(value), _enum(value, "status", RoutingStatus), _optional_identity(value, "selected_provider"),
                   _strings(value, "selection_reason_codes"), alternatives, _counter(value, "eligible_count"),
                   _counter(value, "rejected_count"), _counter(value, "omitted_count"), consents,
                   _boolean(value, "escalation_required"), _identity(value, "next_safe_action"),
                   _string(value, "model_summary", empty=True), _counter(value, "output_bytes"), _counter(value, "output_characters"))

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": self.schema_version, "status": self.status.value, "selected_provider": self.selected_provider,
                "selection_reason_codes": list(self.selection_reason_codes), "rejected_alternatives": [item.to_dict() for item in self.rejected_alternatives],
                "eligible_count": self.eligible_count, "rejected_count": self.rejected_count, "omitted_count": self.omitted_count,
                "consent_requests": [item.to_dict() for item in self.consent_requests], "escalation_required": self.escalation_required,
                "next_safe_action": self.next_safe_action, "model_summary": self.model_summary,
                "output_bytes": self.output_bytes, "output_characters": self.output_characters}


def _exact(value: object, fields: object, field: str) -> None:
    if not isinstance(value, dict) or (isinstance(value, _ParsedObject) and value.duplicate) or set(value) != set(fields):
        raise ProviderModelError(field)


def _schema(value: dict[str, object]) -> str:
    schema = _string(value, "schema_version")
    if schema != "1":
        raise ProviderModelError("schema_version")
    return schema


def _string(value: dict[str, object], field: str, empty: bool = False) -> str:
    item = value[field]
    if not isinstance(item, str) or (not item and not empty) or len(item) > _MAX_STRING:
        raise ProviderModelError(field)
    return item


def _identity(value: dict[str, object], field: str) -> str:
    item = _string(value, field)
    if not _IDENTITY.fullmatch(item):
        raise ProviderModelError(field)
    return item


def _optional_identity(value: dict[str, object], field: str) -> str | None:
    if value[field] is None:
        return None
    return _identity(value, field)


def _boolean(value: dict[str, object], field: str) -> bool:
    if type(value[field]) is not bool:
        raise ProviderModelError(field)
    return value[field]  # type: ignore[return-value]


def _counter(value: dict[str, object], field: str) -> int:
    item = value[field]
    if type(item) is not int or not 0 <= item <= _MAX_COUNTER:
        raise ProviderModelError(field)
    return item


def _optional_number(value: dict[str, object], field: str, unit: bool = False) -> float | None:
    item = value[field]
    if item is None:
        return None
    if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item) or item < 0 or (unit and item > 1):
        raise ProviderModelError(field)
    return float(item)


def _enum(value: dict[str, object], field: str, enum_type: Type[_E]) -> _E:
    item = _string(value, field)
    try:
        return enum_type(item)
    except ValueError as exc:
        raise ProviderModelError(field) from exc


def _enums(value: dict[str, object], field: str, enum_type: Type[_E]) -> tuple[_E, ...]:
    raw = value[field]
    if type(raw) is not list or len(raw) > _MAX_COLLECTION:
        raise ProviderModelError(field)
    parsed = tuple(_enum({field: item}, field, enum_type) for item in raw)
    names = tuple(item.value for item in parsed)
    if names != tuple(sorted(names)) or len(set(names)) != len(names):
        raise ProviderModelError(field)
    return parsed


def _strings(value: dict[str, object], field: str, maximum: int = _MAX_COLLECTION) -> tuple[str, ...]:
    raw = value[field]
    if type(raw) is not list or len(raw) > maximum:
        raise ProviderModelError(field)
    parsed = tuple(_string({field: item}, field) for item in raw)
    if parsed != tuple(sorted(parsed)) or len(set(parsed)) != len(parsed):
        raise ProviderModelError(field)
    return parsed


def _identities(value: dict[str, object], field: str) -> tuple[str, ...]:
    raw = value[field]
    if type(raw) is not list or len(raw) > _MAX_COLLECTION:
        raise ProviderModelError(field)
    parsed = tuple(_identity({field: item}, field) for item in raw)
    if parsed != tuple(sorted(parsed)) or len(set(parsed)) != len(parsed):
        raise ProviderModelError(field)
    return parsed


def _markers(value: dict[str, object], field: str) -> tuple[str, ...]:
    markers = _strings(value, field, _MAX_MARKERS)
    for marker in markers:
        path = PurePosixPath(marker)
        if path.is_absolute() or ".." in path.parts or "\\" in marker:
            raise ProviderModelError(field)
    return markers


def _ordered_providers(items: list[ProviderDescriptor], field: str) -> tuple[ProviderDescriptor, ...]:
    if len(items) > _MAX_COLLECTION:
        raise ProviderModelError(field)
    identities = tuple(item.provider_identity for item in items)
    if identities != tuple(sorted(identities)) or len(set(identities)) != len(identities):
        raise ProviderModelError(field)
    return tuple(items)


def _providers(value: dict[str, object], field: str) -> tuple[ProviderDescriptor, ...]:
    raw = value[field]
    if type(raw) is not list:
        raise ProviderModelError(field)
    return _ordered_providers([ProviderDescriptor.from_dict(item) for item in raw], field)


def _alternatives(value: dict[str, object], field: str) -> tuple[RejectedAlternative, ...]:
    raw = value[field]
    if type(raw) is not list or len(raw) > _MAX_COLLECTION:
        raise ProviderModelError(field)
    parsed = tuple(RejectedAlternative.from_dict(item) for item in raw)
    identities = tuple(item.provider_identity for item in parsed)
    if identities != tuple(sorted(identities)) or len(set(identities)) != len(identities):
        raise ProviderModelError(field)
    return parsed


def _consents(value: dict[str, object], field: str) -> tuple[ConsentRequest, ...]:
    raw = value[field]
    if type(raw) is not list or len(raw) > _MAX_COLLECTION:
        raise ProviderModelError(field)
    parsed = tuple(ConsentRequest.from_dict(item) for item in raw)
    digests = tuple(item.digest for item in parsed)
    if digests != tuple(sorted(digests)) or len(set(digests)) != len(digests):
        raise ProviderModelError(field)
    return parsed


def _consent_digest(value: dict[str, object]) -> str:
    payload = dict(value)
    payload.pop("digest", None)
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class _ParsedObject(dict[str, object]):
    def __init__(self, pairs: list[tuple[str, object]]) -> None:
        super().__init__()
        self.duplicate = False
        for key, item in pairs:
            if key in self:
                self.duplicate = True
            self[key] = item


def _no_duplicates(pairs: list[tuple[str, object]]) -> _ParsedObject:
    result = _ParsedObject(pairs)
    return result


def _bad_constant(_: str) -> None:
    raise ProviderModelError("inventory")
