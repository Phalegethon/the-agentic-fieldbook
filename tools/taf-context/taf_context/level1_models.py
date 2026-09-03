"""Strict portable records for the bounded Level 1 query contract."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Type, TypeVar

from .models import Confidence, Freshness


class Level1ModelError(ValueError):
    """Raised when Level 1 wire data violates the portable contract."""

    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(f"invalid Level 1 field: {field}")


class Level1Operation(str, Enum):
    ESTIMATE = "estimate"
    BUILD = "build"
    UPDATE = "update"
    STATUS = "status"
    METRICS = "metrics"
    REPOSITORY_MAP = "repository-map"
    SEARCH_SYMBOLS = "search-symbols"
    SEARCH_DOCS = "search-docs"
    SOURCE_SNIPPETS = "source-snippets"
    RELATED_SYMBOLS = "related-symbols"


class Level1RecordKind(str, Enum):
    MODULE = "module"
    DEFINITION = "definition"
    IMPORT = "import"
    ENTRY_POINT = "entry-point"
    CONFIGURATION = "configuration"
    HEADING = "heading"
    DOCUMENT_CHUNK = "document-chunk"


class Level1SourceType(str, Enum):
    SOURCE = "source"
    DOCUMENT = "document"
    CONFIGURATION = "configuration"


class Level1ResultStatus(str, Enum):
    READY = "ready"
    PARTIAL = "partial"
    STALE = "stale"
    UNSUPPORTED = "unsupported"
    ERROR = "error"


class CandidateAvailability(str, Enum):
    READY = "ready"
    UNSUPPORTED = "unsupported"


_E = TypeVar("_E", bound=Enum)
_CANONICAL_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_ENVIRONMENT_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_SHA256_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_MAX_WIRE_BYTES = 256 * 1024
_MAX_STRING = 512
_MAX_PREVIEW = 12000
_MAX_COLLECTION = 64
_MAX_COUNTER = 2**31 - 1
_READ_OPERATIONS = {
    Level1Operation.REPOSITORY_MAP,
    Level1Operation.SEARCH_SYMBOLS,
    Level1Operation.SEARCH_DOCS,
    Level1Operation.SOURCE_SNIPPETS,
    Level1Operation.RELATED_SYMBOLS,
}
_CONTROL_OPERATIONS = set(Level1Operation) - _READ_OPERATIONS
_QUERY_OPERATIONS = {
    Level1Operation.SEARCH_SYMBOLS,
    Level1Operation.SEARCH_DOCS,
}
# The two operations that name records instead of searching for them.
_IDENTITY_OPERATIONS = {
    Level1Operation.SOURCE_SNIPPETS,
    Level1Operation.RELATED_SYMBOLS,
}
_ALLOWED_BUDGETS = {2000, 4000, 8000, 12000}
_WIRE_SCHEMAS = ("1", "2")
_DIRECTIONS = ("callers", "callees", "importers", "imports")
# One relationship request stays cheap to resolve, so it carries few anchors.
_MAXIMUM_RELATED_ANCHORS = 16
_RELATIONS = ("call", "import")
_EDGE_EVIDENCE = (Confidence.VERIFIED.value, Confidence.INFERRED.value)
# Keys that exist only in wire schema 2.
_SCHEMA_TWO_REQUEST_FIELDS = frozenset({"direction"})
_SCHEMA_TWO_FINDING_FIELDS = frozenset(
    {"relation", "edge_evidence", "reference_line", "reference_count"}
)


@dataclass(frozen=True)
class Level1Filters:
    path_prefixes: tuple[str, ...]
    languages: tuple[str, ...]
    symbol_kinds: tuple[str, ...]
    source_types: tuple[Level1SourceType, ...]

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "Level1Filters":
        _exact(value, cls.__dataclass_fields__, "filters")
        return cls(
            _relative_paths(value, "path_prefixes"),
            _sorted_texts(value, "languages"),
            _sorted_ids(value, "symbol_kinds"),
            _sorted_enums(value, "source_types", Level1SourceType),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "path_prefixes": list(self.path_prefixes),
            "languages": list(self.languages),
            "symbol_kinds": list(self.symbol_kinds),
            "source_types": [item.value for item in self.source_types],
        }

    def is_empty(self) -> bool:
        return not (
            self.path_prefixes
            or self.languages
            or self.symbol_kinds
            or self.source_types
        )


@dataclass(frozen=True)
class Level1Request:
    schema_version: str
    request_identity: str
    consumer_identity: str
    operation: Level1Operation
    repository_identity: str
    worktree_identity: str
    committed_head: str
    dirty_overlay_fingerprint: str
    provider_identity: str
    index_identity: str | None
    required_capability: str
    minimum_freshness: Freshness
    query: str | None
    result_identities: tuple[str, ...]
    filters: Level1Filters
    maximum_results: int
    maximum_model_output_characters: int
    allow_inferred: bool
    # Schema 2 only: the relationship direction, non-null exactly for
    # `related-symbols`. A schema-1 request carries no direction key at all.
    direction: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "Level1Request":
        # The key set depends on the schema version, so read it first.
        schema = _wire_schema(value, "request")
        _exact(
            value,
            _schema_fields(
                cls.__dataclass_fields__, schema, _SCHEMA_TWO_REQUEST_FIELDS
            ),
            "request",
        )
        operation = _enum(value, "operation", Level1Operation)
        direction = _direction(value, schema, operation)
        index_identity = _optional_sha256(value, "index_identity")
        query = _optional_text(value, "query")
        result_identities = _sorted_sha256s(value, "result_identities")
        filters = Level1Filters.from_dict(_object(value, "filters"))
        maximum_results = _counter(value, "maximum_results")
        maximum_characters = _counter(
            value, "maximum_model_output_characters"
        )
        required_capability = _canonical_id(value, "required_capability")

        if required_capability != operation.value:
            raise Level1ModelError("required_capability")
        if operation in {Level1Operation.ESTIMATE, Level1Operation.BUILD}:
            if index_identity is not None:
                raise Level1ModelError("index_identity")
        elif index_identity is None:
            raise Level1ModelError("index_identity")
        if operation in _QUERY_OPERATIONS:
            if query is None:
                raise Level1ModelError("query")
        elif query is not None:
            raise Level1ModelError("query")
        if operation in _IDENTITY_OPERATIONS:
            if not result_identities:
                raise Level1ModelError("result_identities")
        elif result_identities:
            raise Level1ModelError("result_identities")
        if (
            operation is Level1Operation.RELATED_SYMBOLS
            and len(result_identities) > _MAXIMUM_RELATED_ANCHORS
        ):
            raise Level1ModelError("result_identities")
        if operation in _CONTROL_OPERATIONS and not filters.is_empty():
            raise Level1ModelError("filters")
        if not 1 <= maximum_results <= _MAX_COLLECTION:
            raise Level1ModelError("maximum_results")
        if maximum_characters not in _ALLOWED_BUDGETS:
            raise Level1ModelError("maximum_model_output_characters")

        return cls(
            schema,
            _canonical_id(value, "request_identity"),
            _canonical_id(value, "consumer_identity"),
            operation,
            _sha256(value, "repository_identity"),
            _sha256(value, "worktree_identity"),
            _object_id(value, "committed_head"),
            _sha256(value, "dirty_overlay_fingerprint"),
            _canonical_id(value, "provider_identity"),
            index_identity,
            required_capability,
            _enum(value, "minimum_freshness", Freshness),
            query,
            result_identities,
            filters,
            maximum_results,
            maximum_characters,
            _boolean(value, "allow_inferred"),
            direction,
        )

    def to_dict(self) -> dict[str, object]:
        wire: dict[str, object] = {
            "schema_version": self.schema_version,
            "request_identity": self.request_identity,
            "consumer_identity": self.consumer_identity,
            "operation": self.operation.value,
            "repository_identity": self.repository_identity,
            "worktree_identity": self.worktree_identity,
            "committed_head": self.committed_head,
            "dirty_overlay_fingerprint": self.dirty_overlay_fingerprint,
            "provider_identity": self.provider_identity,
            "index_identity": self.index_identity,
            "required_capability": self.required_capability,
            "minimum_freshness": self.minimum_freshness.value,
            "query": self.query,
            "result_identities": list(self.result_identities),
            "filters": self.filters.to_dict(),
            "maximum_results": self.maximum_results,
            "maximum_model_output_characters": (
                self.maximum_model_output_characters
            ),
            "allow_inferred": self.allow_inferred,
        }
        if self.schema_version == "2":
            wire["direction"] = self.direction
        return wire


@dataclass(frozen=True)
class Level1Finding:
    rank: int
    result_identity: str
    path: str
    start_line: int
    end_line: int
    language: str
    record_kind: Level1RecordKind
    source_type: Level1SourceType
    qualified_name: str
    extraction_method: str
    evidence_class: Confidence
    preview: str
    # Schema 2 only: the edge that reached this record. `relation` and
    # `edge_evidence` are None together when the finding is not a relationship
    # result, and then both counters are zero.
    relation: str | None = None
    edge_evidence: Confidence | None = None
    reference_line: int = 0
    reference_count: int = 0

    @classmethod
    def from_dict(
        cls, value: dict[str, object], schema: str = "1"
    ) -> "Level1Finding":
        _exact(
            value,
            _schema_fields(
                cls.__dataclass_fields__, schema, _SCHEMA_TWO_FINDING_FIELDS
            ),
            "finding",
        )
        rank = _counter(value, "rank")
        start_line = _counter(value, "start_line")
        end_line = _counter(value, "end_line")
        if not 1 <= rank <= _MAX_COLLECTION:
            raise Level1ModelError("rank")
        if start_line < 1 or end_line < start_line:
            raise Level1ModelError("line_range")
        return cls(
            rank,
            _sha256(value, "result_identity"),
            _relative_path(value, "path"),
            start_line,
            end_line,
            _text(value, "language"),
            _enum(value, "record_kind", Level1RecordKind),
            _enum(value, "source_type", Level1SourceType),
            _text(value, "qualified_name", empty=True),
            _text(value, "extraction_method"),
            _enum(value, "evidence_class", Confidence),
            _preview(value, "preview"),
            *_edge(value, schema),
        )

    def to_dict(self, schema: str = "1") -> dict[str, object]:
        wire: dict[str, object] = {
            "rank": self.rank,
            "result_identity": self.result_identity,
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "language": self.language,
            "record_kind": self.record_kind.value,
            "source_type": self.source_type.value,
            "qualified_name": self.qualified_name,
            "extraction_method": self.extraction_method,
            "evidence_class": self.evidence_class.value,
            "preview": self.preview,
        }
        if schema == "2":
            wire["relation"] = self.relation
            wire["edge_evidence"] = (
                None if self.edge_evidence is None else self.edge_evidence.value
            )
            wire["reference_line"] = self.reference_line
            wire["reference_count"] = self.reference_count
        return wire


@dataclass(frozen=True)
class Level1Coverage:
    path_coverage: float
    language_coverage: float
    indexed_path_count: int
    excluded_path_count: int
    unsupported_language_count: int
    parse_failure_count: int
    exclusion_reason_counts: tuple[tuple[str, int], ...]

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "Level1Coverage":
        _exact(value, cls.__dataclass_fields__, "coverage")
        return cls(
            _unit_number(value, "path_coverage"),
            _unit_number(value, "language_coverage"),
            _counter(value, "indexed_path_count"),
            _counter(value, "excluded_path_count"),
            _counter(value, "unsupported_language_count"),
            _counter(value, "parse_failure_count"),
            _reason_counts(value, "exclusion_reason_counts"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "path_coverage": self.path_coverage,
            "language_coverage": self.language_coverage,
            "indexed_path_count": self.indexed_path_count,
            "excluded_path_count": self.excluded_path_count,
            "unsupported_language_count": self.unsupported_language_count,
            "parse_failure_count": self.parse_failure_count,
            "exclusion_reason_counts": dict(self.exclusion_reason_counts),
        }


@dataclass(frozen=True)
class Level1Result:
    schema_version: str
    request_identity: str
    operation: Level1Operation
    status: Level1ResultStatus
    provider_identity: str
    provider_version: str
    index_identity: str | None
    repository_identity: str
    worktree_identity: str
    committed_head: str
    dirty_overlay_fingerprint: str
    freshness: Freshness
    parser_versions: tuple[tuple[str, str], ...]
    coverage: Level1Coverage
    findings: tuple[Level1Finding, ...]
    returned_count: int
    omitted_count: int
    truncated: bool
    output_characters: int
    warnings: tuple[str, ...]
    next_safe_action: str

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "Level1Result":
        schema = _wire_schema(value, "result")
        _exact(value, cls.__dataclass_fields__, "result")
        operation = _enum(value, "operation", Level1Operation)
        status = _enum(value, "status", Level1ResultStatus)
        index_identity = _optional_sha256(value, "index_identity")
        findings = _findings(value, "findings", schema)
        returned_count = _counter(value, "returned_count")
        omitted_count = _counter(value, "omitted_count")
        truncated = _boolean(value, "truncated")
        freshness = _enum(value, "freshness", Freshness)
        output_characters = _counter(value, "output_characters")

        if returned_count != len(findings):
            raise Level1ModelError("returned_count")
        if tuple(item.rank for item in findings) != tuple(
            range(1, len(findings) + 1)
        ):
            raise Level1ModelError("rank")
        identities = tuple(item.result_identity for item in findings)
        if len(set(identities)) != len(identities):
            raise Level1ModelError("findings")
        if omitted_count > 0 and not truncated:
            raise Level1ModelError("truncated")
        if output_characters > 12000:
            raise Level1ModelError("output_characters")
        if status in {
            Level1ResultStatus.STALE,
            Level1ResultStatus.UNSUPPORTED,
            Level1ResultStatus.ERROR,
        } and findings:
            raise Level1ModelError("findings")
        if freshness is not Freshness.EXACT and any(
            item.evidence_class is Confidence.VERIFIED for item in findings
        ):
            raise Level1ModelError("evidence_class")
        # Only the operation that resolves relationships carries edge data.
        if operation is not Level1Operation.RELATED_SYMBOLS and any(
            item.relation is not None for item in findings
        ):
            raise Level1ModelError("relation")
        if index_identity is None and not (
            operation is Level1Operation.ESTIMATE
            or (
                operation is Level1Operation.BUILD
                and status is not Level1ResultStatus.READY
            )
        ):
            raise Level1ModelError("index_identity")

        return cls(
            schema,
            _canonical_id(value, "request_identity"),
            operation,
            status,
            _canonical_id(value, "provider_identity"),
            _text(value, "provider_version"),
            index_identity,
            _sha256(value, "repository_identity"),
            _sha256(value, "worktree_identity"),
            _object_id(value, "committed_head"),
            _sha256(value, "dirty_overlay_fingerprint"),
            freshness,
            _text_map(value, "parser_versions"),
            Level1Coverage.from_dict(_object(value, "coverage")),
            findings,
            returned_count,
            omitted_count,
            truncated,
            output_characters,
            _sorted_texts(value, "warnings"),
            _canonical_id(value, "next_safe_action"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "request_identity": self.request_identity,
            "operation": self.operation.value,
            "status": self.status.value,
            "provider_identity": self.provider_identity,
            "provider_version": self.provider_version,
            "index_identity": self.index_identity,
            "repository_identity": self.repository_identity,
            "worktree_identity": self.worktree_identity,
            "committed_head": self.committed_head,
            "dirty_overlay_fingerprint": self.dirty_overlay_fingerprint,
            "freshness": self.freshness.value,
            "parser_versions": dict(self.parser_versions),
            "coverage": self.coverage.to_dict(),
            "findings": [
                item.to_dict(self.schema_version) for item in self.findings
            ],
            "returned_count": self.returned_count,
            "omitted_count": self.omitted_count,
            "truncated": self.truncated,
            "output_characters": self.output_characters,
            "warnings": list(self.warnings),
            "next_safe_action": self.next_safe_action,
        }


@dataclass(frozen=True)
class CandidateManifest:
    schema_version: str
    candidate_identity: str
    candidate_version: str
    language: str
    protocol_version: str
    availability: CandidateAvailability
    unsupported_reason_codes: tuple[str, ...]
    executable: str
    arguments: tuple[str, ...]
    environment_allowlist: tuple[str, ...]
    declared_child_processes: tuple[str, ...]
    dependency_lock: str
    license_inventory: str

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "CandidateManifest":
        _exact(value, cls.__dataclass_fields__, "candidate_manifest")
        availability = _enum(value, "availability", CandidateAvailability)
        reasons = _sorted_ids(value, "unsupported_reason_codes")
        executable = _text(value, "executable", empty=True)
        arguments = _texts(value, "arguments")
        environment = _environment_names(value, "environment_allowlist")
        children = _sorted_ids(value, "declared_child_processes")
        dependency_lock = _text(value, "dependency_lock", empty=True)
        licenses = _text(value, "license_inventory", empty=True)

        if children:
            raise Level1ModelError("declared_child_processes")
        if availability is CandidateAvailability.READY:
            if reasons:
                raise Level1ModelError("unsupported_reason_codes")
            for field, item in (
                ("executable", executable),
                ("dependency_lock", dependency_lock),
                ("license_inventory", licenses),
            ):
                _validate_relative_path(item, field)
        else:
            if not reasons:
                raise Level1ModelError("unsupported_reason_codes")
            if executable or arguments or environment or dependency_lock or licenses:
                raise Level1ModelError("availability")

        return cls(
            _schema(value),
            _canonical_id(value, "candidate_identity"),
            _text(value, "candidate_version"),
            _text(value, "language"),
            _text(value, "protocol_version"),
            availability,
            reasons,
            executable,
            arguments,
            environment,
            children,
            dependency_lock,
            licenses,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "candidate_identity": self.candidate_identity,
            "candidate_version": self.candidate_version,
            "language": self.language,
            "protocol_version": self.protocol_version,
            "availability": self.availability.value,
            "unsupported_reason_codes": list(self.unsupported_reason_codes),
            "executable": self.executable,
            "arguments": list(self.arguments),
            "environment_allowlist": list(self.environment_allowlist),
            "declared_child_processes": list(self.declared_child_processes),
            "dependency_lock": self.dependency_lock,
            "license_inventory": self.license_inventory,
        }


def parse_level1_request(raw: bytes) -> Level1Request:
    return Level1Request.from_dict(_load_wire(raw, "request"))


def parse_level1_result(raw: bytes) -> Level1Result:
    return Level1Result.from_dict(_load_wire(raw, "result"))


def _load_wire(raw: bytes, field: str) -> dict[str, object]:
    if type(raw) is not bytes or len(raw) > _MAX_WIRE_BYTES:
        raise Level1ModelError(f"{field}_bytes")
    try:
        text = raw.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Level1ModelError(field) from exc
    if type(value) is not _ParsedObject:
        raise Level1ModelError(field)
    return value


class _ParsedObject(dict[str, object]):
    def __init__(self, pairs: list[tuple[str, object]]) -> None:
        super().__init__()
        self.duplicate = False
        for key, item in pairs:
            if key in self:
                self.duplicate = True
            self[key] = item


def _unique_object(pairs: list[tuple[str, object]]) -> _ParsedObject:
    return _ParsedObject(pairs)


def _reject_constant(_: str) -> None:
    raise Level1ModelError("number")


def _exact(value: object, fields: object, field: str) -> None:
    if (
        not isinstance(value, dict)
        or (isinstance(value, _ParsedObject) and value.duplicate)
        or set(value) != set(fields)
    ):
        raise Level1ModelError(field)


def _object(value: dict[str, object], field: str) -> dict[str, object]:
    item = value[field]
    if not isinstance(item, dict):
        raise Level1ModelError(field)
    return item


def _schema(value: dict[str, object]) -> str:
    if _text(value, "schema_version") != "1":
        raise Level1ModelError("schema_version")
    return "1"


def _wire_schema(value: object, field: str) -> str:
    """Read the wire schema version before the key set, which depends on it."""
    if (
        not isinstance(value, dict)
        or (isinstance(value, _ParsedObject) and value.duplicate)
        or "schema_version" not in value
    ):
        raise Level1ModelError(field)
    version = _text(value, "schema_version")
    if version not in _WIRE_SCHEMAS:
        raise Level1ModelError("schema_version")
    return version


def _schema_fields(
    fields: object, schema: str, schema_two_only: frozenset[str]
) -> set[str]:
    """The exact key set a wire object must carry under ``schema``."""
    names = set(fields)
    if schema == "1":
        names -= schema_two_only
    return names


def _direction(
    value: dict[str, object], schema: str, operation: Level1Operation
) -> str | None:
    if schema == "1":
        # Schema 1 has no direction key at all, so it cannot name the one
        # operation that needs one.
        if operation is Level1Operation.RELATED_SYMBOLS:
            raise Level1ModelError("operation")
        return None
    direction = _optional_text(value, "direction")
    if (direction is not None) != (
        operation is Level1Operation.RELATED_SYMBOLS
    ):
        raise Level1ModelError("direction")
    if direction is not None and direction not in _DIRECTIONS:
        raise Level1ModelError("direction")
    return direction


def _edge_label(
    value: dict[str, object], field: str, allowed: tuple[str, ...]
) -> str | None:
    """Read one schema-2 edge label; null and "" both mean "no edge"."""
    if value[field] is None:
        return None
    label = _text(value, field, empty=True)
    if not label:
        return None
    if label not in allowed:
        raise Level1ModelError(field)
    return label


def _edge(
    value: dict[str, object], schema: str
) -> tuple[str | None, Confidence | None, int, int]:
    if schema != "2":
        return None, None, 0, 0
    relation = _edge_label(value, "relation", _RELATIONS)
    evidence = _edge_label(value, "edge_evidence", _EDGE_EVIDENCE)
    reference_line = _counter(value, "reference_line")
    reference_count = _counter(value, "reference_count")
    if (relation is None) != (evidence is None):
        raise Level1ModelError("relation")
    if relation is None:
        if reference_line:
            raise Level1ModelError("reference_line")
        if reference_count:
            raise Level1ModelError("reference_count")
    return (
        relation,
        None if evidence is None else Confidence(evidence),
        reference_line,
        reference_count,
    )


def _valid_unicode(item: str) -> bool:
    try:
        item.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _text(
    value: dict[str, object],
    field: str,
    *,
    empty: bool = False,
) -> str:
    item = value[field]
    if (
        not isinstance(item, str)
        or (not item and not empty)
        or len(item) > _MAX_STRING
        or not _valid_unicode(item)
        or "\x00" in item
        or "\n" in item
        or "\r" in item
    ):
        raise Level1ModelError(field)
    return item


def _preview(value: dict[str, object], field: str) -> str:
    item = value[field]
    if (
        not isinstance(item, str)
        or len(item) > _MAX_PREVIEW
        or not _valid_unicode(item)
        or "\x00" in item
        or "\r" in item
    ):
        raise Level1ModelError(field)
    return item


def _optional_text(value: dict[str, object], field: str) -> str | None:
    if value[field] is None:
        return None
    return _text(value, field)


def _canonical_id(value: dict[str, object], field: str) -> str:
    item = _text(value, field)
    if not _CANONICAL_ID.fullmatch(item):
        raise Level1ModelError(field)
    return item


def _sha256(value: dict[str, object], field: str) -> str:
    item = _text(value, field)
    if not _SHA256_ID.fullmatch(item):
        raise Level1ModelError(field)
    return item


def _optional_sha256(value: dict[str, object], field: str) -> str | None:
    if value[field] is None:
        return None
    return _sha256(value, field)


def _object_id(value: dict[str, object], field: str) -> str:
    item = _text(value, field)
    if not _OBJECT_ID.fullmatch(item):
        raise Level1ModelError(field)
    return item


def _boolean(value: dict[str, object], field: str) -> bool:
    item = value[field]
    if type(item) is not bool:
        raise Level1ModelError(field)
    return item


def _counter(value: dict[str, object], field: str) -> int:
    item = value[field]
    if type(item) is not int or not 0 <= item <= _MAX_COUNTER:
        raise Level1ModelError(field)
    return item


def _unit_number(value: dict[str, object], field: str) -> float:
    item = value[field]
    if (
        isinstance(item, bool)
        or not isinstance(item, (int, float))
        or not math.isfinite(item)
        or not 0 <= item <= 1
    ):
        raise Level1ModelError(field)
    return float(item)


def _enum(value: dict[str, object], field: str, enum_type: Type[_E]) -> _E:
    item = _text(value, field)
    try:
        return enum_type(item)
    except ValueError as exc:
        raise Level1ModelError(field) from exc


def _texts(value: dict[str, object], field: str) -> tuple[str, ...]:
    raw = value[field]
    if type(raw) is not list or len(raw) > _MAX_COLLECTION:
        raise Level1ModelError(field)
    return tuple(_text({field: item}, field) for item in raw)


def _sorted_texts(value: dict[str, object], field: str) -> tuple[str, ...]:
    items = _texts(value, field)
    if items != tuple(sorted(items)) or len(set(items)) != len(items):
        raise Level1ModelError(field)
    return items


def _sorted_ids(value: dict[str, object], field: str) -> tuple[str, ...]:
    raw = value[field]
    if type(raw) is not list or len(raw) > _MAX_COLLECTION:
        raise Level1ModelError(field)
    items = tuple(_canonical_id({field: item}, field) for item in raw)
    if items != tuple(sorted(items)) or len(set(items)) != len(items):
        raise Level1ModelError(field)
    return items


def _sorted_sha256s(value: dict[str, object], field: str) -> tuple[str, ...]:
    raw = value[field]
    if type(raw) is not list or len(raw) > _MAX_COLLECTION:
        raise Level1ModelError(field)
    items = tuple(_sha256({field: item}, field) for item in raw)
    if items != tuple(sorted(items)) or len(set(items)) != len(items):
        raise Level1ModelError(field)
    return items


def _sorted_enums(
    value: dict[str, object],
    field: str,
    enum_type: Type[_E],
) -> tuple[_E, ...]:
    raw = value[field]
    if type(raw) is not list or len(raw) > _MAX_COLLECTION:
        raise Level1ModelError(field)
    items = tuple(_enum({field: item}, field, enum_type) for item in raw)
    names = tuple(item.value for item in items)
    if names != tuple(sorted(names)) or len(set(names)) != len(names):
        raise Level1ModelError(field)
    return items


def _validate_relative_path(item: str, field: str) -> str:
    path = PurePosixPath(item)
    if (
        not item
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in item
        or re.match(r"^[A-Za-z]:", item)
        or item != path.as_posix()
    ):
        raise Level1ModelError(field)
    return item


def _relative_path(value: dict[str, object], field: str) -> str:
    return _validate_relative_path(_text(value, field), field)


def _relative_paths(value: dict[str, object], field: str) -> tuple[str, ...]:
    raw = value[field]
    if type(raw) is not list or len(raw) > _MAX_COLLECTION:
        raise Level1ModelError(field)
    items = tuple(
        _validate_relative_path(_text({field: item}, field), field)
        for item in raw
    )
    if items != tuple(sorted(items)) or len(set(items)) != len(items):
        raise Level1ModelError(field)
    return items


def _environment_names(
    value: dict[str, object],
    field: str,
) -> tuple[str, ...]:
    items = _sorted_texts(value, field)
    if any(not _ENVIRONMENT_NAME.fullmatch(item) for item in items):
        raise Level1ModelError(field)
    return items


def _text_map(
    value: dict[str, object],
    field: str,
) -> tuple[tuple[str, str], ...]:
    raw = _object(value, field)
    _exact(raw, raw.keys(), field)
    items = tuple(
        sorted(
            (
                _canonical_id({"key": key}, "key"),
                _text({"item": item}, "item"),
            )
            for key, item in raw.items()
        )
    )
    if len(items) > _MAX_COLLECTION:
        raise Level1ModelError(field)
    return items


def _reason_counts(
    value: dict[str, object],
    field: str,
) -> tuple[tuple[str, int], ...]:
    raw = _object(value, field)
    if isinstance(raw, _ParsedObject) and raw.duplicate:
        raise Level1ModelError(field)
    items = tuple(
        sorted(
            (
                _canonical_id({"key": key}, "key"),
                _counter({"count": item}, "count"),
            )
            for key, item in raw.items()
        )
    )
    if len(items) > _MAX_COLLECTION:
        raise Level1ModelError(field)
    return items


def _findings(
    value: dict[str, object],
    field: str,
    schema: str = "1",
) -> tuple[Level1Finding, ...]:
    raw = value[field]
    if type(raw) is not list or len(raw) > _MAX_COLLECTION:
        raise Level1ModelError(field)
    return tuple(Level1Finding.from_dict(item, schema) for item in raw)
