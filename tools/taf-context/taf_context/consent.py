"""Immutable, exact-scope allow and deny records for context operations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from .models import ContextAction
from .provider_models import ConsentRequest, ProviderModelError


class ConsentError(ValueError):
    """Raised when a consent ledger does not match its strict wire schema."""

    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(f"invalid consent field: {field}")


class ConsentDisposition(str, Enum):
    """The explicit outcome of a consent request."""

    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True)
class ConsentRecord:
    """One digest-bound decision at an exact action and provider scope."""

    action: ContextAction
    repository_identity: str
    provider_identity: str
    provider_schema_version: str
    disposition: ConsentDisposition
    decided_at: str
    request_digest: str


_RECORD_FIELDS = frozenset(ConsentRecord.__dataclass_fields__)
_LEDGER_FIELDS = frozenset({"schema_version", "records"})
_REQUEST_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_RFC3339 = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z"
)


@dataclass(frozen=True)
class AuthorizationLedger:
    """A canonical, immutable history of exact allow and deny decisions."""

    records: tuple[ConsentRecord, ...] = ()

    def __post_init__(self) -> None:
        if type(self.records) is not tuple:
            raise ConsentError("records")

        normalized: dict[tuple[ContextAction, str, str, str, datetime], ConsentRecord] = {}
        for index, record in enumerate(self.records):
            parsed = _parse_record(record, f"records[{index}]")
            key = _record_time_scope(parsed)
            existing = normalized.get(key)
            if existing is not None and existing != parsed:
                raise ConsentError(f"records[{index}]")
            normalized[key] = parsed
        object.__setattr__(self, "records", tuple(sorted(normalized.values(), key=_record_sort_key)))

    def record(
        self,
        request: ConsentRequest,
        disposition: ConsentDisposition,
        decided_at: str,
    ) -> "AuthorizationLedger":
        """Return a ledger containing one verified decision per requested action."""
        verified_request = _verify_request(request)
        normalized_disposition = _parse_disposition(disposition, "disposition")
        normalized_timestamp = _require_timestamp(decided_at, "decided_at")
        digest = f"sha256:{verified_request.digest}"
        additions = tuple(
            ConsentRecord(
                action=action,
                repository_identity=verified_request.repository_identity,
                provider_identity=verified_request.provider_identity,
                provider_schema_version=verified_request.provider_schema_version,
                disposition=normalized_disposition,
                decided_at=normalized_timestamp,
                request_digest=digest,
            )
            for action in verified_request.actions
        )
        return AuthorizationLedger(self.records + additions)

    def decision_for(
        self,
        action: ContextAction,
        repository_identity: str,
        provider_identity: str,
        provider_schema_version: str,
    ) -> ConsentDisposition | None:
        """Return the latest decision for exactly this action and provider scope."""
        scope = _parse_scope(
            action,
            repository_identity,
            provider_identity,
            provider_schema_version,
        )
        matches = [record for record in self.records if _record_scope(record) == scope]
        if not matches:
            return None
        return max(matches, key=lambda record: _timestamp_value(record.decided_at)).disposition

    def is_authorized(
        self,
        action: ContextAction,
        repository_identity: str,
        provider_identity: str,
        provider_schema_version: str,
    ) -> bool:
        """Return whether the latest exact decision is an allow."""
        return (
            self.decision_for(
                action, repository_identity, provider_identity, provider_schema_version
            )
            is ConsentDisposition.ALLOW
        )

    def is_denied(
        self,
        action: ContextAction,
        repository_identity: str,
        provider_identity: str,
        provider_schema_version: str,
    ) -> bool:
        """Return whether the latest exact decision is a deny."""
        return (
            self.decision_for(
                action, repository_identity, provider_identity, provider_schema_version
            )
            is ConsentDisposition.DENY
        )

    def revoke(
        self,
        action: ContextAction,
        repository_identity: str,
        provider_identity: str,
        provider_schema_version: str,
    ) -> "AuthorizationLedger":
        """Return a ledger without any decision for exactly this scope."""
        scope = _parse_scope(
            action,
            repository_identity,
            provider_identity,
            provider_schema_version,
        )
        return AuthorizationLedger(
            tuple(record for record in self.records if _record_scope(record) != scope)
        )

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "AuthorizationLedger":
        """Build a ledger from the exact immutable schema-version-2 wire shape."""
        if type(value) is not dict or set(value) != _LEDGER_FIELDS:
            raise ConsentError("ledger")
        if value["schema_version"] != "2":
            raise ConsentError("schema_version")
        entries = value["records"]
        if type(entries) is not list:
            raise ConsentError("records")
        return cls(tuple(_record_from_dict(entry, f"records[{index}]") for index, entry in enumerate(entries)))

    def to_dict(self) -> dict[str, object]:
        """Return canonical JSON-ready records sorted by scope and decision time."""
        return {
            "schema_version": "2",
            "records": [
                {
                    "action": record.action.value,
                    "repository_identity": record.repository_identity,
                    "provider_identity": record.provider_identity,
                    "provider_schema_version": record.provider_schema_version,
                    "disposition": record.disposition.value,
                    "decided_at": record.decided_at,
                    "request_digest": record.request_digest,
                }
                for record in self.records
            ],
        }


def _record_from_dict(value: object, field: str) -> ConsentRecord:
    if type(value) is not dict or set(value) != _RECORD_FIELDS:
        raise ConsentError(field)
    return _parse_record(
        ConsentRecord(
            action=value["action"],  # type: ignore[arg-type]
            repository_identity=value["repository_identity"],  # type: ignore[arg-type]
            provider_identity=value["provider_identity"],  # type: ignore[arg-type]
            provider_schema_version=value["provider_schema_version"],  # type: ignore[arg-type]
            disposition=value["disposition"],  # type: ignore[arg-type]
            decided_at=value["decided_at"],  # type: ignore[arg-type]
            request_digest=value["request_digest"],  # type: ignore[arg-type]
        ),
        field,
    )


def _parse_record(value: object, field: str) -> ConsentRecord:
    if type(value) is not ConsentRecord:
        raise ConsentError(field)
    action, repository_identity, provider_identity, provider_schema_version = _parse_scope(
        value.action,
        value.repository_identity,
        value.provider_identity,
        value.provider_schema_version,
        field,
    )
    request_digest = _require_request_digest(value.request_digest, f"{field}.request_digest")
    return ConsentRecord(
        action=action,
        repository_identity=repository_identity,
        provider_identity=provider_identity,
        provider_schema_version=provider_schema_version,
        disposition=_parse_disposition(value.disposition, f"{field}.disposition"),
        decided_at=_require_timestamp(value.decided_at, f"{field}.decided_at"),
        request_digest=request_digest,
    )


def _parse_scope(
    action: object,
    repository_identity: object,
    provider_identity: object,
    provider_schema_version: object,
    field: str = "scope",
) -> tuple[ContextAction, str, str, str]:
    return (
        _parse_action(action, f"{field}.action"),
        _require_scope(repository_identity, f"{field}.repository_identity"),
        _require_scope(provider_identity, f"{field}.provider_identity"),
        _require_scope(provider_schema_version, f"{field}.provider_schema_version"),
    )


def _parse_action(value: object, field: str) -> ContextAction:
    if isinstance(value, ContextAction):
        return value
    if isinstance(value, str):
        try:
            return ContextAction(value)
        except ValueError as exc:
            raise ConsentError(field) from exc
    raise ConsentError(field)


def _parse_disposition(value: object, field: str) -> ConsentDisposition:
    if isinstance(value, ConsentDisposition):
        return value
    if isinstance(value, str):
        try:
            return ConsentDisposition(value)
        except ValueError as exc:
            raise ConsentError(field) from exc
    raise ConsentError(field)


def _require_scope(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConsentError(field)
    return value


def _require_request_digest(value: object, field: str) -> str:
    if not isinstance(value, str) or not _REQUEST_DIGEST.fullmatch(value):
        raise ConsentError(field)
    return value


def _require_timestamp(value: object, field: str) -> str:
    if not isinstance(value, str) or not _RFC3339.fullmatch(value):
        raise ConsentError(field)
    _timestamp_value(value, field)
    return value


def _timestamp_value(value: str, field: str = "decided_at") -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConsentError(field) from exc
    if parsed.tzinfo is None:
        raise ConsentError(field)
    return parsed.astimezone(timezone.utc)


def _verify_request(value: object) -> ConsentRequest:
    if not isinstance(value, ConsentRequest):
        raise ConsentError("request")
    try:
        return ConsentRequest.from_dict(value.to_dict())
    except (ProviderModelError, TypeError, ValueError, KeyError) as exc:
        raise ConsentError("request_digest") from exc


def _record_scope(record: ConsentRecord) -> tuple[ContextAction, str, str, str]:
    return (
        record.action,
        record.repository_identity,
        record.provider_identity,
        record.provider_schema_version,
    )


def _record_time_scope(record: ConsentRecord) -> tuple[ContextAction, str, str, str, datetime]:
    return (*_record_scope(record), _timestamp_value(record.decided_at))


def _record_sort_key(record: ConsentRecord) -> tuple[str, str, str, str, datetime, str, str]:
    return (
        record.action.value,
        record.repository_identity,
        record.provider_identity,
        record.provider_schema_version,
        _timestamp_value(record.decided_at),
        record.disposition.value,
        record.request_digest,
    )
