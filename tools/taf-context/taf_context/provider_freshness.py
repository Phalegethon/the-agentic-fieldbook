"""Pure freshness derivation for actively inspected providers."""

from __future__ import annotations

from dataclasses import dataclass, replace

from .models import Confidence, Freshness, RepositorySnapshot
from .provider_execution_models import InspectionRecord, Readiness
from .provider_models import Availability, ProviderDescriptor, StatusEvidence


@dataclass(frozen=True)
class ProviderFreshnessAssessment:
    freshness: Freshness
    reason_codes: tuple[str, ...]


_PRECEDENCE = {
    Freshness.UNUSABLE: 0,
    Freshness.STRUCTURALLY_STALE: 1,
    Freshness.UNKNOWN: 2,
    Freshness.INCREMENTALLY_STALE: 3,
    Freshness.PARTIAL: 4,
    Freshness.COMMIT_FRESH_WORKTREE_STALE: 5,
    Freshness.EXACT: 6,
}
_REASON_ORDER = {
    "worktree-identity-mismatch": 0,
    "provider-unavailable": 1,
    "index-corrupt": 2,
    "repository-identity-mismatch": 3,
    "head-mismatch": 4,
    "dirty-state-incomplete": 5,
    "provider-partial": 6,
    "dirty-overlay-mismatch": 7,
    "exact-match": 8,
}


def derive_provider_freshness(
    inspection: InspectionRecord,
    snapshot: RepositorySnapshot,
) -> ProviderFreshnessAssessment:
    """Derive freshness from exact bindings without performing I/O."""
    if not isinstance(inspection, InspectionRecord):
        raise TypeError("inspection-invalid")
    if not isinstance(snapshot, RepositorySnapshot):
        raise TypeError("snapshot-invalid")

    reasons: list[tuple[Freshness, str]] = []
    if inspection.worktree_identity != snapshot.worktree_identity:
        reasons.append((Freshness.UNUSABLE, "worktree-identity-mismatch"))
    if inspection.readiness is Readiness.UNAVAILABLE:
        reasons.append((Freshness.UNUSABLE, "provider-unavailable"))
    if inspection.readiness is Readiness.CORRUPT:
        reasons.append((Freshness.UNUSABLE, "index-corrupt"))
    if inspection.repository_identity != snapshot.repository_identity:
        reasons.append((Freshness.STRUCTURALLY_STALE, "repository-identity-mismatch"))
    if inspection.committed_head != snapshot.head_sha:
        reasons.append((Freshness.UNKNOWN, "head-mismatch"))
    if not snapshot.dirty_fingerprint_complete:
        reasons.append((Freshness.PARTIAL, "dirty-state-incomplete"))
    if inspection.readiness is Readiness.PARTIAL:
        reasons.append((Freshness.PARTIAL, "provider-partial"))
    if (
        inspection.committed_head == snapshot.head_sha
        and inspection.dirty_overlay_fingerprint != snapshot.dirty_fingerprint
    ):
        reasons.append(
            (Freshness.COMMIT_FRESH_WORKTREE_STALE, "dirty-overlay-mismatch")
        )

    if not reasons:
        return ProviderFreshnessAssessment(Freshness.EXACT, ("exact-match",))
    freshness = min(reasons, key=lambda item: _PRECEDENCE[item[0]])[0]
    reason_codes = tuple(
        code
        for _, code in sorted(
            reasons,
            key=lambda item: (
                _PRECEDENCE[item[0]],
                _REASON_ORDER.get(item[1], len(_REASON_ORDER)),
                item[1],
            ),
        )
    )
    return ProviderFreshnessAssessment(freshness, reason_codes)


def refine_descriptor(
    descriptor: ProviderDescriptor,
    inspection: InspectionRecord,
    assessment: ProviderFreshnessAssessment,
) -> ProviderDescriptor:
    """Return a descriptor whose active evidence is controller-derived."""
    if not isinstance(descriptor, ProviderDescriptor):
        raise TypeError("descriptor-invalid")
    if not isinstance(inspection, InspectionRecord):
        raise TypeError("inspection-invalid")
    if not isinstance(assessment, ProviderFreshnessAssessment):
        raise TypeError("assessment-invalid")
    if (
        descriptor.provider_identity != inspection.provider_identity
        or descriptor.provider_version != inspection.provider_version
    ):
        raise ValueError("provider-inspection-identity-mismatch")
    unavailable = assessment.freshness is Freshness.UNUSABLE
    return replace(
        descriptor,
        capabilities=inspection.capabilities,
        availability=(
            Availability.UNAVAILABLE if unavailable else Availability.AVAILABLE
        ),
        status_evidence=StatusEvidence.PROVIDER_INSPECTED,
        freshness=assessment.freshness,
        path_coverage=inspection.path_coverage,
        language_coverage=inspection.language_coverage,
        confidence=(
            Confidence.VERIFIED
            if assessment.freshness is Freshness.EXACT
            else Confidence.UNCERTAIN
        ),
        reason_codes=tuple(
            sorted(set(descriptor.reason_codes + assessment.reason_codes))
        ),
        warnings=tuple(sorted(set(descriptor.warnings + inspection.warnings))),
    )
