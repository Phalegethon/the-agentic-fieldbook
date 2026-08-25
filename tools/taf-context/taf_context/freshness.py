"""Pure, deterministic freshness assessment for portable context manifests."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from taf_context.models import ContextManifest, Freshness, RepositorySnapshot


class HeadRelation(str, Enum):
    """The caller-provided relationship between manifest and current HEAD."""

    MATCHES = "matches"
    FORWARD = "forward"
    DIVERGED = "diverged"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FreshnessExpectation:
    """Authorization and bounded comparison inputs supplied by the caller."""

    repository_authorized: bool
    provider_compatible: bool
    provider_schema_version: str
    include_rules_hash: str
    exclude_rules_hash: str
    required_path_coverage: float
    head_relation: HeadRelation
    changed_path_count: int | None
    maximum_changed_path_count: int
    dirty_state_proven: bool
    manifest_is_corrupt: bool


@dataclass(frozen=True)
class FreshnessAssessment:
    """A freshness result with ordered, machine-readable explanations."""

    freshness: Freshness
    reason_codes: tuple[str, ...]
    requires_rebuild: bool
    can_incrementally_update: bool


_CORRUPT_MANIFEST_WARNING = "manifest-corrupt"
_INCOMPLETE_DIRTY_FINGERPRINT_WARNING = "dirty-fingerprint-incomplete"

_PRECEDENCE = (
    Freshness.UNUSABLE,
    Freshness.STRUCTURALLY_STALE,
    Freshness.UNKNOWN,
    Freshness.INCREMENTALLY_STALE,
    Freshness.PARTIAL,
    Freshness.COMMIT_FRESH_WORKTREE_STALE,
)

_REASON_ORDER = {
    "repository-unauthorized": 0,
    "worktree-scope-mismatch": 1,
    "manifest-corrupt": 2,
    "provider-incompatible": 3,
    "repository-identity-mismatch": 4,
    "provider-schema-mismatch": 5,
    "include-rules-mismatch": 6,
    "exclude-rules-mismatch": 7,
    "head-diverged": 8,
    "head-relation-unproven": 9,
    "dirty-state-unproven": 10,
    "changed-path-count-unproven": 11,
    "changed-path-set-unbounded": 12,
    "head-forward": 13,
    "changed-path-set-bounded": 14,
    "required-path-coverage-absent": 15,
    "dirty-fingerprint-incomplete": 16,
    "dirty-fingerprint-mismatch": 17,
}


def assess_freshness(
    manifest: ContextManifest,
    current: RepositorySnapshot,
    expectation: FreshnessExpectation,
) -> FreshnessAssessment:
    """Classify freshness without accessing Git or the filesystem."""
    reasons: list[tuple[Freshness, str]] = []

    if not expectation.repository_authorized:
        reasons.append((Freshness.UNUSABLE, "repository-unauthorized"))
    if manifest.worktree_identity != current.worktree_identity:
        reasons.append((Freshness.UNUSABLE, "worktree-scope-mismatch"))
    if expectation.manifest_is_corrupt or _CORRUPT_MANIFEST_WARNING in manifest.warnings:
        reasons.append((Freshness.UNUSABLE, "manifest-corrupt"))
    if not expectation.provider_compatible:
        reasons.append((Freshness.UNUSABLE, "provider-incompatible"))

    if manifest.repository_identity != current.repository_identity:
        reasons.append((Freshness.STRUCTURALLY_STALE, "repository-identity-mismatch"))
    if manifest.provider_schema_version != expectation.provider_schema_version:
        reasons.append((Freshness.STRUCTURALLY_STALE, "provider-schema-mismatch"))
    if manifest.include_rules_hash != expectation.include_rules_hash:
        reasons.append((Freshness.STRUCTURALLY_STALE, "include-rules-mismatch"))
    if manifest.exclude_rules_hash != expectation.exclude_rules_hash:
        reasons.append((Freshness.STRUCTURALLY_STALE, "exclude-rules-mismatch"))
    if expectation.head_relation is HeadRelation.DIVERGED:
        reasons.append((Freshness.STRUCTURALLY_STALE, "head-diverged"))

    if expectation.head_relation is HeadRelation.UNKNOWN:
        reasons.append((Freshness.UNKNOWN, "head-relation-unproven"))
    if not expectation.dirty_state_proven:
        reasons.append((Freshness.UNKNOWN, "dirty-state-unproven"))
    if (
        expectation.changed_path_count is None
        and expectation.head_relation is not HeadRelation.MATCHES
    ):
        reasons.append((Freshness.UNKNOWN, "changed-path-count-unproven"))
    if (
        expectation.changed_path_count is not None
        and expectation.changed_path_count > expectation.maximum_changed_path_count
    ):
        reasons.append((Freshness.UNKNOWN, "changed-path-set-unbounded"))

    if expectation.head_relation is HeadRelation.FORWARD:
        reasons.append((Freshness.INCREMENTALLY_STALE, "head-forward"))
    if (
        expectation.changed_path_count is not None
        and 0 < expectation.changed_path_count <= expectation.maximum_changed_path_count
    ):
        reasons.append((Freshness.INCREMENTALLY_STALE, "changed-path-set-bounded"))

    if manifest.path_coverage < expectation.required_path_coverage:
        reasons.append((Freshness.PARTIAL, "required-path-coverage-absent"))
    if (
        not current.dirty_fingerprint_complete
        or _INCOMPLETE_DIRTY_FINGERPRINT_WARNING in manifest.warnings
    ):
        reasons.append((Freshness.PARTIAL, "dirty-fingerprint-incomplete"))

    if (
        expectation.head_relation is HeadRelation.MATCHES
        and manifest.dirty_fingerprint != current.dirty_fingerprint
    ):
        reasons.append((Freshness.COMMIT_FRESH_WORKTREE_STALE, "dirty-fingerprint-mismatch"))

    ordered_reasons = tuple(
        code
        for _, code in sorted(
            reasons,
            key=lambda reason: (_PRECEDENCE.index(reason[0]), _REASON_ORDER[reason[1]]),
        )
    )
    freshness = reasons and min(reasons, key=lambda reason: _PRECEDENCE.index(reason[0]))[0]
    if not freshness:
        freshness = Freshness.EXACT
        ordered_reasons = ("exact-match",)

    return FreshnessAssessment(
        freshness=freshness,
        reason_codes=ordered_reasons,
        requires_rebuild=freshness is Freshness.STRUCTURALLY_STALE,
        can_incrementally_update=freshness
        in (Freshness.INCREMENTALLY_STALE, Freshness.COMMIT_FRESH_WORKTREE_STALE),
    )
