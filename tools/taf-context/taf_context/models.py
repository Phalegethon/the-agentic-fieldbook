"""Portable, provider-independent records for context-index metadata.

The manifest deliberately contains fingerprints rather than machine-local paths,
so it can be stored and compared without exposing a checkout's layout.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Any


class ManifestError(ValueError):
    """Raised when a manifest field is absent or does not match the schema."""

    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(f"invalid manifest field: {field}")


class Freshness(str, Enum):
    EXACT = "exact"
    COMMIT_FRESH_WORKTREE_STALE = "commit-fresh-worktree-stale"
    INCREMENTALLY_STALE = "incrementally-stale"
    STRUCTURALLY_STALE = "structurally-stale"
    PARTIAL = "partial"
    UNKNOWN = "unknown"
    UNUSABLE = "unusable"


class Confidence(str, Enum):
    VERIFIED = "verified"
    INFERRED = "inferred"
    UNCERTAIN = "uncertain"


class BackgroundState(str, Enum):
    NOT_BUILT = "not-built"
    AWAITING_CONSENT = "awaiting-consent"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ContextAction(str, Enum):
    BUILD = "build"
    UPDATE = "update"
    WATCH = "watch"
    INSTALL = "install"
    NETWORK = "network"
    DELETE = "delete"


@dataclass(frozen=True)
class RepositorySnapshot:
    schema_version: str
    repository_identity: str
    canonical_root: str
    canonical_root_fingerprint: str
    git_dir: str
    git_common_dir: str
    git_common_dir_fingerprint: str
    worktree_identity: str
    head_sha: str | None
    branch: str | None
    dirty_fingerprint: str
    dirty_fingerprint_complete: bool
    tracked_paths: tuple[str, ...]
    staged_paths: tuple[str, ...]
    unstaged_paths: tuple[str, ...]
    untracked_paths: tuple[str, ...]
    ignored_entry_count: int
    generated_or_vendored_count: int
    binary_file_count: int
    oversized_file_count: int
    language_counts: tuple[tuple[str, int], ...]
    candidate_artifacts: tuple[str, ...]
    provider_markers: tuple[str, ...]
    insertions: int
    deletions: int
    dirty_bytes_hashed: int
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class Coverage:
    """The provider-neutral coverage and storage metrics in a manifest."""

    language_coverage: tuple[tuple[str, float], ...]
    path_coverage: float
    tracked_file_count: int
    indexed_file_count: int
    skipped_file_count: int
    parse_failure_count: int
    generated_or_vendored_count: int
    storage_bytes: int


@dataclass(frozen=True)
class ContextManifest:
    """Validated portable metadata for one provider's context index."""

    schema_version: str
    repository_identity: str
    canonical_root_fingerprint: str
    git_common_dir_fingerprint: str
    worktree_identity: str
    head_sha: str | None
    dirty_fingerprint: str
    provider_name: str
    provider_version: str
    provider_index_id: str
    provider_schema_version: str
    index_levels: tuple[str, ...]
    capabilities: tuple[str, ...]
    created_at: str
    updated_at: str
    include_rules_hash: str
    exclude_rules_hash: str
    language_coverage: tuple[tuple[str, float], ...]
    path_coverage: float
    tracked_file_count: int
    indexed_file_count: int
    skipped_file_count: int
    parse_failure_count: int
    generated_or_vendored_count: int
    storage_bytes: int
    background_state: BackgroundState
    warnings: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "ContextManifest":
        """Construct a manifest only when *value* matches schema version 1."""
        if type(value) is not dict:
            raise ManifestError("manifest")

        expected = set(cls.__dataclass_fields__)
        actual = set(value)
        unknown = actual - expected
        if unknown:
            raise ManifestError(sorted(unknown)[0])
        missing = expected - actual
        if missing:
            raise ManifestError(sorted(missing)[0])

        schema_version = _string(value, "schema_version")
        if schema_version != "1":
            raise ManifestError("schema_version")

        strings = {
            field: _string(value, field)
            for field in (
                "repository_identity",
                "canonical_root_fingerprint",
                "git_common_dir_fingerprint",
                "worktree_identity",
                "dirty_fingerprint",
                "provider_name",
                "provider_version",
                "provider_index_id",
                "provider_schema_version",
                "created_at",
                "updated_at",
                "include_rules_hash",
                "exclude_rules_hash",
            )
        }
        head_sha = _optional_string(value, "head_sha")
        index_levels = _strings(value, "index_levels")
        capabilities = _repository_relative_strings(value, "capabilities")
        warnings = _repository_relative_strings(value, "warnings")
        language_coverage = _coverage(value, "language_coverage")
        path_coverage = _unit_interval(value, "path_coverage")
        counts = {
            field: _non_negative_integer(value, field)
            for field in (
                "tracked_file_count",
                "indexed_file_count",
                "skipped_file_count",
                "parse_failure_count",
                "generated_or_vendored_count",
                "storage_bytes",
            )
        }
        try:
            background_state = BackgroundState(_string(value, "background_state"))
        except ValueError as exc:
            raise ManifestError("background_state") from exc

        return cls(
            schema_version=schema_version,
            head_sha=head_sha,
            index_levels=index_levels,
            capabilities=capabilities,
            language_coverage=language_coverage,
            path_coverage=path_coverage,
            background_state=background_state,
            warnings=warnings,
            **strings,
            **counts,
        )

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-ready representation using ordinary mutable containers."""
        return {
            "schema_version": self.schema_version,
            "repository_identity": self.repository_identity,
            "canonical_root_fingerprint": self.canonical_root_fingerprint,
            "git_common_dir_fingerprint": self.git_common_dir_fingerprint,
            "worktree_identity": self.worktree_identity,
            "head_sha": self.head_sha,
            "dirty_fingerprint": self.dirty_fingerprint,
            "provider_name": self.provider_name,
            "provider_version": self.provider_version,
            "provider_index_id": self.provider_index_id,
            "provider_schema_version": self.provider_schema_version,
            "index_levels": list(self.index_levels),
            "capabilities": list(self.capabilities),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "include_rules_hash": self.include_rules_hash,
            "exclude_rules_hash": self.exclude_rules_hash,
            "language_coverage": dict(self.language_coverage),
            "path_coverage": self.path_coverage,
            "tracked_file_count": self.tracked_file_count,
            "indexed_file_count": self.indexed_file_count,
            "skipped_file_count": self.skipped_file_count,
            "parse_failure_count": self.parse_failure_count,
            "generated_or_vendored_count": self.generated_or_vendored_count,
            "storage_bytes": self.storage_bytes,
            "background_state": self.background_state.value,
            "warnings": list(self.warnings),
        }


def canonical_json(value: object) -> str:
    """Serialize *value* to a compact, deterministic UTF-8 JSON string."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ) + "\n"


def _string(value: dict[str, object], field: str) -> str:
    candidate = value[field]
    if not isinstance(candidate, str) or not candidate:
        raise ManifestError(field)
    return candidate


def _optional_string(value: dict[str, object], field: str) -> str | None:
    candidate = value[field]
    if candidate is None:
        return None
    if not isinstance(candidate, str) or not candidate:
        raise ManifestError(field)
    return candidate


def _strings(value: dict[str, object], field: str) -> tuple[str, ...]:
    candidate = value[field]
    if not isinstance(candidate, (list, tuple)) or any(
        not isinstance(item, str) or not item for item in candidate
    ):
        raise ManifestError(field)
    return tuple(candidate)


def _repository_relative_strings(value: dict[str, object], field: str) -> tuple[str, ...]:
    items = _strings(value, field)
    if any(not _is_repository_relative(item) for item in items):
        raise ManifestError(field)
    return items


def _is_repository_relative(value: str) -> bool:
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts and "\\" not in value


def _coverage(value: dict[str, object], field: str) -> tuple[tuple[str, float], ...]:
    candidate = value[field]
    if type(candidate) is not dict:
        raise ManifestError(field)

    normalized: list[tuple[str, float]] = []
    for language, coverage in candidate.items():
        if not isinstance(language, str) or not language:
            raise ManifestError(field)
        if isinstance(coverage, bool) or not isinstance(coverage, (int, float)):
            raise ManifestError(field)
        if not 0.0 <= coverage <= 1.0:
            raise ManifestError(field)
        normalized.append((language, float(coverage)))
    return tuple(sorted(normalized))


def _unit_interval(value: dict[str, object], field: str) -> float:
    candidate = value[field]
    if isinstance(candidate, bool) or not isinstance(candidate, (int, float)):
        raise ManifestError(field)
    if not 0.0 <= candidate <= 1.0:
        raise ManifestError(field)
    return float(candidate)


def _non_negative_integer(value: dict[str, object], field: str) -> int:
    candidate = value[field]
    if isinstance(candidate, bool) or not isinstance(candidate, int) or candidate < 0:
        raise ManifestError(field)
    return candidate
