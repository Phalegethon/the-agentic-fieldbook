"""Strict portable records for bounded work recovery."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Callable, TypeVar

from .models import Freshness, ManifestError


_OBJECT_ID = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")
_CANONICAL_ID = re.compile(r"[a-z0-9]+(?:[._:-][a-z0-9]+)*")
_MAX_COUNTER = 2**31 - 1
_MAX_STRING = 512
_T = TypeVar("_T")


class EvidenceClass(str, Enum):
    OBSERVED = "observed"
    REPORTED = "reported"
    INFERRED = "inferred"
    UNKNOWN = "unknown"
    CONFLICTED = "conflicted"


class WorkState(str, Enum):
    ACTIVE_DIRTY = "active-dirty"
    ACTIVE_COMMITTED = "active-committed"
    INTEGRATED = "integrated"
    SUPERSEDED_STALE = "superseded-stale"
    DIVERGED = "diverged"
    CLEAN_UNRESOLVED = "clean-unresolved"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RecoveryClaim:
    claim_id: str
    evidence_class: EvidenceClass
    text: str
    repository_identity: str
    worktree_identity: str
    provenance: tuple[str, ...]
    freshness: Freshness
    supports: tuple[str, ...]
    conflicts: tuple[str, ...]
    qualifications: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "RecoveryClaim":
        _exact_fields(value, cls, "recovery_claim")
        claim_id = _canonical_id(value, "claim_id")
        return cls(
            claim_id=claim_id,
            evidence_class=_enum(value, "evidence_class", EvidenceClass),
            text=_bounded_string(value, "text"),
            repository_identity=_bounded_string(value, "repository_identity"),
            worktree_identity=_bounded_string(value, "worktree_identity"),
            provenance=_sorted_references(value, "provenance"),
            freshness=_enum(value, "freshness", Freshness),
            supports=_sorted_ids(value, "supports"),
            conflicts=_sorted_ids(value, "conflicts"),
            qualifications=_sorted_strings(value, "qualifications"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "claim_id": self.claim_id,
            "evidence_class": self.evidence_class.value,
            "text": self.text,
            "repository_identity": self.repository_identity,
            "worktree_identity": self.worktree_identity,
            "provenance": list(self.provenance),
            "freshness": self.freshness.value,
            "supports": list(self.supports),
            "conflicts": list(self.conflicts),
            "qualifications": list(self.qualifications),
        }


@dataclass(frozen=True)
class WorkstreamState:
    worktree_identity: str
    branch: str | None
    head_sha: str | None
    base_sha: str | None
    state: WorkState
    staged_count: int
    unstaged_count: int
    untracked_count: int
    ahead_count: int | None
    behind_count: int | None
    reason_codes: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "WorkstreamState":
        _exact_fields(value, cls, "workstream_state")
        return cls(
            worktree_identity=_bounded_string(value, "worktree_identity"),
            branch=_optional_bounded_string(value, "branch"),
            head_sha=_optional_object_id(value, "head_sha"),
            base_sha=_optional_object_id(value, "base_sha"),
            state=_enum(value, "state", WorkState),
            staged_count=_counter(value, "staged_count"),
            unstaged_count=_counter(value, "unstaged_count"),
            untracked_count=_counter(value, "untracked_count"),
            ahead_count=_optional_counter(value, "ahead_count"),
            behind_count=_optional_counter(value, "behind_count"),
            reason_codes=_sorted_ids(value, "reason_codes"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "worktree_identity": self.worktree_identity,
            "branch": self.branch,
            "head_sha": self.head_sha,
            "base_sha": self.base_sha,
            "state": self.state.value,
            "staged_count": self.staged_count,
            "unstaged_count": self.unstaged_count,
            "untracked_count": self.untracked_count,
            "ahead_count": self.ahead_count,
            "behind_count": self.behind_count,
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True)
class RecoveryCoverage:
    changed_path_count: int
    examined_path_count: int
    cluster_count: int
    included_cluster_count: int
    omitted_item_count: int
    omitted_characters: int
    budget_characters: int

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "RecoveryCoverage":
        _exact_fields(value, cls, "recovery_coverage")
        counts = {
            field: _counter(value, field)
            for field in cls.__dataclass_fields__
        }
        if counts["examined_path_count"] > counts["changed_path_count"]:
            raise ManifestError("examined_path_count")
        if counts["included_cluster_count"] > counts["cluster_count"]:
            raise ManifestError("included_cluster_count")
        return cls(**counts)

    def to_dict(self) -> dict[str, object]:
        return {
            "changed_path_count": self.changed_path_count,
            "examined_path_count": self.examined_path_count,
            "cluster_count": self.cluster_count,
            "included_cluster_count": self.included_cluster_count,
            "omitted_item_count": self.omitted_item_count,
            "omitted_characters": self.omitted_characters,
            "budget_characters": self.budget_characters,
        }


@dataclass(frozen=True)
class RecoveryDossier:
    schema_version: str
    repository_identity: str
    worktree_identity: str
    current: WorkstreamState
    candidates: tuple[WorkstreamState, ...]
    claims: tuple[RecoveryClaim, ...]
    coverage: RecoveryCoverage
    warnings: tuple[str, ...]
    next_action_hint: str

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "RecoveryDossier":
        _exact_fields(value, cls, "recovery_dossier")
        if _bounded_string(value, "schema_version") != "1":
            raise ManifestError("schema_version")
        candidates = _records(value, "candidates", WorkstreamState.from_dict, 64)
        claims = _records(value, "claims", RecoveryClaim.from_dict, 1024)
        if len({candidate.worktree_identity for candidate in candidates}) != len(candidates):
            raise ManifestError("candidates")
        if len({claim.claim_id for claim in claims}) != len(claims):
            raise ManifestError("claims")
        current = _record(value, "current", WorkstreamState.from_dict)
        coverage = _record(value, "coverage", RecoveryCoverage.from_dict)
        return cls(
            schema_version="1",
            repository_identity=_bounded_string(value, "repository_identity"),
            worktree_identity=_bounded_string(value, "worktree_identity"),
            current=current,
            candidates=candidates,
            claims=claims,
            coverage=coverage,
            warnings=_sorted_strings(value, "warnings"),
            next_action_hint=_bounded_string(value, "next_action_hint"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "repository_identity": self.repository_identity,
            "worktree_identity": self.worktree_identity,
            "current": self.current.to_dict(),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "claims": [claim.to_dict() for claim in self.claims],
            "coverage": self.coverage.to_dict(),
            "warnings": list(self.warnings),
            "next_action_hint": self.next_action_hint,
        }


def _exact_fields(value: object, cls: type[object], fallback: str) -> None:
    if type(value) is not dict:
        raise ManifestError(fallback)
    expected = set(cls.__dataclass_fields__)
    actual = set(value)
    unknown = actual - expected
    if unknown:
        raise ManifestError(sorted(unknown)[0])
    missing = expected - actual
    if missing:
        raise ManifestError(sorted(missing)[0])


def _bounded_string(value: dict[str, object], field: str) -> str:
    candidate = value[field]
    if not isinstance(candidate, str) or not candidate or len(candidate) > _MAX_STRING:
        raise ManifestError(field)
    return candidate


def _optional_bounded_string(value: dict[str, object], field: str) -> str | None:
    candidate = value[field]
    if candidate is None:
        return None
    return _bounded_string(value, field)


def _canonical_id(value: dict[str, object], field: str) -> str:
    candidate = _bounded_string(value, field)
    if len(candidate) > 128 or not _CANONICAL_ID.fullmatch(candidate):
        raise ManifestError(field)
    return candidate


def _enum(value: dict[str, object], field: str, enum_type: type[_T]) -> _T:
    candidate = value[field]
    if not isinstance(candidate, str):
        raise ManifestError(field)
    try:
        return enum_type(candidate)  # type: ignore[call-arg]
    except ValueError as error:
        raise ManifestError(field) from error


def _optional_object_id(value: dict[str, object], field: str) -> str | None:
    candidate = value[field]
    if candidate is None:
        return None
    if not isinstance(candidate, str) or not _OBJECT_ID.fullmatch(candidate):
        raise ManifestError(field)
    return candidate


def _counter(value: dict[str, object], field: str) -> int:
    candidate = value[field]
    if type(candidate) is not int or not 0 <= candidate <= _MAX_COUNTER:
        raise ManifestError(field)
    return candidate


def _optional_counter(value: dict[str, object], field: str) -> int | None:
    if value[field] is None:
        return None
    return _counter(value, field)


def _sorted_strings(value: dict[str, object], field: str) -> tuple[str, ...]:
    candidate = value[field]
    if not isinstance(candidate, (list, tuple)):
        raise ManifestError(field)
    items = tuple(candidate)
    if any(not isinstance(item, str) or not item or len(item) > _MAX_STRING for item in items):
        raise ManifestError(field)
    if items != tuple(sorted(items)) or len(items) != len(set(items)):
        raise ManifestError(field)
    return items  # type: ignore[return-value]


def _sorted_ids(value: dict[str, object], field: str) -> tuple[str, ...]:
    items = _sorted_strings(value, field)
    if any(len(item) > 128 or not _CANONICAL_ID.fullmatch(item) for item in items):
        raise ManifestError(field)
    return items


def _sorted_references(value: dict[str, object], field: str) -> tuple[str, ...]:
    items = _sorted_strings(value, field)
    for item in items:
        path = PurePosixPath(item)
        if path.is_absolute() or ".." in path.parts or "\\" in item:
            raise ManifestError(field)
    return items


def _record(
    value: dict[str, object],
    field: str,
    loader: Callable[[dict[str, object]], _T],
) -> _T:
    candidate = value[field]
    if type(candidate) is not dict:
        raise ManifestError(field)
    try:
        return loader(candidate)
    except ManifestError as error:
        raise ManifestError(f"{field}.{error.field}") from error


def _records(
    value: dict[str, object],
    field: str,
    loader: Callable[[dict[str, object]], _T],
    limit: int,
) -> tuple[_T, ...]:
    candidate = value[field]
    if not isinstance(candidate, (list, tuple)) or len(candidate) > limit:
        raise ManifestError(field)
    records: list[_T] = []
    for index, item in enumerate(candidate):
        if type(item) is not dict:
            raise ManifestError(field)
        try:
            records.append(loader(item))
        except ManifestError as error:
            raise ManifestError(f"{field}.{index}.{error.field}") from error
    return tuple(records)
