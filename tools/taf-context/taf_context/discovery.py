"""Pure normalization of already-materialized provider discovery evidence.

Discovery never starts a provider, opens a repository path, or performs a
network operation. Rejection summaries use ``<identity>:<reason-code>`` with
these stable reason codes:

``provider-version-conflict``, ``provider-schema-version-conflict``,
``provider-locality-conflict``, ``provider-capabilities-conflict``,
``registration-schema-version-mismatch``,
``registration-required-capability-mismatch``,
``registration-provider-unavailable``, ``registration-repository-mismatch``,
``reserved-provider-identity``, and ``invalid-host-discovery-source``.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Iterable

from .models import (
    Confidence,
    ContextAction,
    Freshness,
    RepositorySnapshot,
    canonical_json,
)
from .provider_models import (
    Availability,
    DiscoverySnapshot,
    DiscoverySource,
    HostInventory,
    ProjectRegistration,
    ProviderDescriptor,
    ProviderLocality,
    Registration,
    StatusEvidence,
)


_MAX_PROVIDERS = 64
_MAX_SUMMARIES = 64
_MAX_MARKERS = 16
_MAX_STRINGS = 64
_MAX_COUNTER = 2**31 - 1
_NATIVE_IDENTITY = "taf-context"

_VERSION_CONFLICT = "provider-version-conflict"
_SCHEMA_CONFLICT = "provider-schema-version-conflict"
_LOCALITY_CONFLICT = "provider-locality-conflict"
_CAPABILITY_CONFLICT = "provider-capabilities-conflict"
_REGISTRATION_SCHEMA_MISMATCH = "registration-schema-version-mismatch"
_REGISTRATION_CAPABILITY_MISMATCH = "registration-required-capability-mismatch"
_REGISTRATION_UNAVAILABLE = "registration-provider-unavailable"
_REGISTRATION_REPOSITORY_MISMATCH = "registration-repository-mismatch"
_RESERVED_IDENTITY = "reserved-provider-identity"
_INVALID_HOST_SOURCE = "invalid-host-discovery-source"


def native_level0_descriptor(snapshot: RepositorySnapshot) -> ProviderDescriptor:
    """Return the reserved, always-available native Level 0 descriptor."""
    return ProviderDescriptor(
        schema_version="1",
        provider_identity=_NATIVE_IDENTITY,
        provider_version="0.1.1",
        provider_schema_version="1",
        capabilities=("repository-map", "status"),
        locality=ProviderLocality.LOCAL,
        discovery_sources=(DiscoverySource.NATIVE,),
        availability=Availability.AVAILABLE,
        registration=Registration.NATIVE,
        status_evidence=StatusEvidence.MANIFEST_VALIDATED,
        freshness=(
            Freshness.EXACT if snapshot.dirty_fingerprint_complete else Freshness.PARTIAL
        ),
        path_coverage=1.0,
        language_coverage=None,
        latency_ms=None,
        confidence=Confidence.VERIFIED,
        supported_actions=(ContextAction.QUERY,),
        required_actions=(),
        marker_hints=(),
        reason_codes=(),
        warnings=snapshot.warnings,
    )


def discover_providers(
    snapshot: RepositorySnapshot,
    host_inventory: HostInventory,
    user_registry: tuple[ProviderDescriptor, ...],
    registration: ProjectRegistration | None,
    *,
    maximum_providers: int = _MAX_PROVIDERS,
) -> DiscoverySnapshot:
    """Merge bounded passive evidence into one deterministic discovery record."""
    if type(maximum_providers) is not int or not 1 <= maximum_providers <= _MAX_PROVIDERS:
        raise ValueError("maximum-providers-invalid")

    rejections: list[tuple[str, tuple[str, ...]]] = []
    valid_host: list[ProviderDescriptor] = []
    tainted_host_identities: set[str] = set()
    for descriptor in sorted(host_inventory.providers, key=_descriptor_key):
        if descriptor.discovery_sources != (DiscoverySource.HOST_INVENTORY,):
            rejections.append((descriptor.provider_identity, (_INVALID_HOST_SOURCE,)))
            tainted_host_identities.add(descriptor.provider_identity)
            continue
        valid_host.append(descriptor)

    registry = [
        replace(item, discovery_sources=(DiscoverySource.USER_REGISTRY,))
        for item in sorted(user_registry, key=_descriptor_key)
    ]
    host_by_identity = _group(valid_host)
    registry_by_identity = _group(registry)
    identities = sorted(set(host_by_identity) | set(registry_by_identity))

    normalized: dict[str, ProviderDescriptor] = {
        _NATIVE_IDENTITY: native_level0_descriptor(snapshot)
    }
    materialized_paths = frozenset(
        (*snapshot.tracked_paths, *snapshot.untracked_paths, *snapshot.provider_markers)
    )

    for identity in identities:
        host_claims = host_by_identity.get(identity, ())
        registry_claims = registry_by_identity.get(identity, ())
        if identity in tainted_host_identities:
            continue
        if identity == _NATIVE_IDENTITY:
            for _ in (*host_claims, *registry_claims):
                rejections.append((identity, (_RESERVED_IDENTITY,)))
            continue

        conflicts = _conflicts((*host_claims, *registry_claims))
        if conflicts:
            rejections.append((identity, conflicts))
            continue

        host = min(host_claims, key=_descriptor_key) if host_claims else None
        registered = min(registry_claims, key=_descriptor_key) if registry_claims else None
        base = host if host is not None else registered
        if base is None:  # pragma: no cover - identity comes from one of the groups
            continue
        claims = (*host_claims, *registry_claims)
        sources = set()
        if host_claims:
            sources.add(DiscoverySource.HOST_INVENTORY)
        if registry_claims:
            sources.add(DiscoverySource.USER_REGISTRY)
        all_marker_hints = tuple(
            sorted({marker for claim in claims for marker in claim.marker_hints})
        )
        marker_hints = all_marker_hints[:_MAX_MARKERS]
        reason_codes = {reason for claim in claims for reason in claim.reason_codes}
        if any(marker in materialized_paths for marker in all_marker_hints):
            sources.add(DiscoverySource.PROJECT_MARKER)
            reason_codes.add("marker-match")
        normalized[identity] = replace(
            base,
            discovery_sources=_sorted_enums(sources),
            registration=(
                Registration.USER_REGISTERED
                if registry_claims
                else Registration.UNREGISTERED
            ),
            marker_hints=marker_hints,
            reason_codes=tuple(sorted(reason_codes))[:_MAX_STRINGS],
            warnings=tuple(
                sorted({warning for claim in claims for warning in claim.warnings})
            )[:_MAX_STRINGS],
        )

    _apply_registration(
        snapshot,
        registration,
        normalized,
        rejections,
    )

    all_providers = tuple(normalized[identity] for identity in sorted(normalized))
    providers = all_providers[:maximum_providers]
    locally_omitted = all_providers[maximum_providers:]
    omitted_count = host_inventory.omitted_provider_count + len(locally_omitted)
    rejected_count = host_inventory.rejected_provider_count + len(rejections)
    if rejected_count > _MAX_COUNTER:
        raise ValueError("rejected-provider-count-overflow")
    if omitted_count > _MAX_COUNTER:
        raise ValueError("omitted-provider-count-overflow")
    summaries = tuple(
        sorted(
            set(host_inventory.rejection_summaries)
            | {_rejection_summary(identity, reasons) for identity, reasons in rejections}
        )
    )[:_MAX_SUMMARIES]
    partial = bool(
        host_inventory.partial or rejected_count or omitted_count
    )
    warnings = set(snapshot.warnings)
    if partial:
        warnings.discard("inventory-partial")
        ordered_warnings = tuple(
            sorted((*tuple(sorted(warnings))[: _MAX_STRINGS - 1], "inventory-partial"))
        )
    else:
        ordered_warnings = tuple(sorted(warnings))[:_MAX_STRINGS]

    fingerprint = _inventory_fingerprint(
        providers=providers,
        omitted=locally_omitted,
        rejections=rejections,
        upstream_rejected_count=host_inventory.rejected_provider_count,
        upstream_rejection_summaries=host_inventory.rejection_summaries,
        upstream_omitted_count=host_inventory.omitted_provider_count,
    )
    input_bytes = _input_bytes(snapshot, host_inventory, user_registry, registration)
    result = DiscoverySnapshot(
        schema_version="1",
        repository_identity=snapshot.repository_identity,
        worktree_identity=snapshot.worktree_identity,
        inventory_fingerprint=fingerprint,
        providers=providers,
        rejected_provider_count=rejected_count,
        rejection_summaries=summaries,
        omitted_provider_count=omitted_count,
        partial=partial,
        warnings=ordered_warnings,
        input_bytes=input_bytes,
        output_bytes=0,
    )
    return _with_exact_output_bytes(result)


def _apply_registration(
    snapshot: RepositorySnapshot,
    registration: ProjectRegistration | None,
    normalized: dict[str, ProviderDescriptor],
    rejections: list[tuple[str, tuple[str, ...]]],
) -> None:
    if registration is None:
        return
    entries = sorted(
        registration.providers,
        key=lambda item: (
            item.provider_identity,
            item.provider_schema_version,
            item.required_capabilities,
        ),
    )
    repository_mismatch = registration.repository_identity != snapshot.repository_identity
    for entry in entries:
        identity = entry.provider_identity
        if identity == _NATIVE_IDENTITY:
            rejections.append((identity, (_RESERVED_IDENTITY,)))
            continue
        if repository_mismatch:
            rejections.append((identity, (_REGISTRATION_REPOSITORY_MISMATCH,)))
            continue
        descriptor = normalized.get(identity)
        if descriptor is None:
            rejections.append((identity, (_REGISTRATION_UNAVAILABLE,)))
            continue
        if entry.provider_schema_version != descriptor.provider_schema_version:
            rejections.append((identity, (_REGISTRATION_SCHEMA_MISMATCH,)))
            continue
        if not set(entry.required_capabilities).issubset(descriptor.capabilities):
            rejections.append((identity, (_REGISTRATION_CAPABILITY_MISMATCH,)))
            continue
        sources = set(descriptor.discovery_sources)
        sources.add(DiscoverySource.PROJECT_REGISTRATION)
        normalized[identity] = replace(
            descriptor,
            discovery_sources=_sorted_enums(sources),
            registration=Registration.PROJECT_DECLARED,
        )


def _conflicts(claims: tuple[ProviderDescriptor, ...]) -> tuple[str, ...]:
    if len(claims) < 2:
        return ()
    reasons = []
    if len({item.provider_version for item in claims}) > 1:
        reasons.append(_VERSION_CONFLICT)
    if len({item.provider_schema_version for item in claims}) > 1:
        reasons.append(_SCHEMA_CONFLICT)
    if len({item.locality for item in claims}) > 1:
        reasons.append(_LOCALITY_CONFLICT)
    if len({tuple(sorted(item.capabilities)) for item in claims}) > 1:
        reasons.append(_CAPABILITY_CONFLICT)
    return tuple(sorted(reasons))


def _group(
    descriptors: Iterable[ProviderDescriptor],
) -> dict[str, tuple[ProviderDescriptor, ...]]:
    grouped: dict[str, list[ProviderDescriptor]] = {}
    for descriptor in descriptors:
        grouped.setdefault(descriptor.provider_identity, []).append(descriptor)
    return {
        identity: tuple(sorted(items, key=_descriptor_key))
        for identity, items in grouped.items()
    }


def _descriptor_key(descriptor: ProviderDescriptor) -> str:
    return canonical_json(descriptor.to_dict())


def _sorted_enums(values: Iterable[DiscoverySource]) -> tuple[DiscoverySource, ...]:
    return tuple(sorted(set(values), key=lambda item: item.value))


def _rejection_summary(identity: str, reasons: tuple[str, ...]) -> str:
    return f"{identity}:{','.join(sorted(set(reasons)))}"


def _inventory_fingerprint(
    *,
    providers: tuple[ProviderDescriptor, ...],
    omitted: tuple[ProviderDescriptor, ...],
    rejections: list[tuple[str, tuple[str, ...]]],
    upstream_rejected_count: int,
    upstream_rejection_summaries: tuple[str, ...],
    upstream_omitted_count: int,
) -> str:
    records: list[dict[str, object]] = []
    records.extend(
        {"kind": "accepted", "identity": item.provider_identity, "descriptor": item.to_dict()}
        for item in providers
    )
    records.extend(
        {"kind": "omitted", "identity": item.provider_identity, "descriptor": item.to_dict()}
        for item in omitted
    )
    records.extend(
        {"kind": "rejected", "identity": identity, "reason_codes": list(reasons)}
        for identity, reasons in sorted(rejections)
    )
    records.extend(
        {"kind": "upstream-rejected", "summary": summary}
        for summary in sorted(set(upstream_rejection_summaries))
    )
    records.extend(
        (
            {"kind": "upstream-rejected-count", "count": upstream_rejected_count},
            {"kind": "upstream-omitted-count", "count": upstream_omitted_count},
        )
    )
    digest = hashlib.sha256()
    for record in sorted(records, key=canonical_json):
        encoded = canonical_json(record).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"sha256:{digest.hexdigest()}"


def _input_bytes(
    snapshot: RepositorySnapshot,
    host_inventory: HostInventory,
    user_registry: tuple[ProviderDescriptor, ...],
    registration: ProjectRegistration | None,
) -> int:
    value = {
        "host_inventory": host_inventory.to_dict(),
        "registration": None if registration is None else registration.to_dict(),
        "snapshot": snapshot.to_dict(),
        "user_registry": [item.to_dict() for item in user_registry],
    }
    return len(canonical_json(value).encode("utf-8"))


def _with_exact_output_bytes(result: DiscoverySnapshot) -> DiscoverySnapshot:
    output_bytes = result.output_bytes
    for _ in range(8):
        candidate = replace(result, output_bytes=output_bytes)
        measured = len(canonical_json(candidate.to_dict()).encode("utf-8"))
        if measured == output_bytes:
            return candidate
        output_bytes = measured
    raise RuntimeError("output-byte-count-did-not-converge")
