"""Deterministic active inspection, routing, query, and fallback orchestration."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from typing import Callable, Mapping

from .consent import AuthorizationLedger
from .level1_models import Level1Operation, Level1Request, Level1Result
from .models import ContextAction, Freshness, RepositorySnapshot
from .provider_execution_models import AttemptRecord, InspectionRecord
from .provider_freshness import derive_provider_freshness, refine_descriptor
from .provider_models import BrokerRequest, DiscoverySnapshot, ProviderDescriptor, RoutingStatus
from .provider_process import ProviderProcessError
from .routing import route_provider


@dataclass(frozen=True)
class BrokerExecution:
    result: Level1Result | None
    route: str
    attempts: tuple[AttemptRecord, ...]
    reason_codes: tuple[str, ...]
    next_safe_action: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "1",
            "route": self.route,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "reason_codes": list(self.reason_codes),
            "next_safe_action": self.next_safe_action,
            "result": None if self.result is None else self.result.to_dict(),
        }


def execute_broker_request(
    discovery: DiscoverySnapshot,
    request: Level1Request,
    consent: AuthorizationLedger,
    snapshot: RepositorySnapshot,
    adapters: Mapping[str, object],
    *,
    inspect_call: Callable[
        [ProviderDescriptor], tuple[InspectionRecord, AttemptRecord]
    ],
    query_call: Callable[
        [ProviderDescriptor, Level1Request],
        Level1Result | tuple[Level1Result, AttemptRecord],
    ],
    fallback_call: Callable[[Level1Request], Level1Result] | None,
    utc_now: str,
    maximum_inspections: int = 3,
) -> BrokerExecution:
    """Inspect only authorized candidates, reroute once, and execute one route."""
    if type(maximum_inspections) is not int or not 0 <= maximum_inspections <= 3:
        raise ValueError("invalid-maximum-inspections")
    attempts: list[AttemptRecord] = []
    reasons: list[str] = []
    refined: list[ProviderDescriptor] = []
    inspections: dict[str, InspectionRecord] = {}
    inspected_count = 0
    ordered_providers = sorted(
        discovery.providers,
        key=lambda item: (
            item.provider_identity != request.provider_identity,
            item.provider_identity,
        ),
    )
    for provider in ordered_providers:
        if provider.provider_identity not in adapters:
            refined.append(provider)
            continue
        if request.required_capability not in provider.capabilities:
            refined.append(provider)
            continue
        if not consent.is_authorized(ContextAction.INSPECT, discovery.repository_identity, provider.provider_identity, provider.provider_schema_version):
            refined.append(provider)
            continue
        if inspected_count >= maximum_inspections:
            reasons.append("inspection-budget-exhausted")
            refined.append(provider)
            continue
        inspected_count += 1
        try:
            inspected, attempt = inspect_call(provider)
            assessment = derive_provider_freshness(inspected, snapshot)
            refined.append(refine_descriptor(provider, inspected, assessment))
            inspections[provider.provider_identity] = inspected
            attempts.append(attempt)
        except Exception as error:
            reasons.append(
                _reason_code(error, "provider-inspection-failed")
            )
            refined.append(provider)
    refreshed = replace(discovery, providers=tuple(sorted(refined, key=lambda item: item.provider_identity)))
    broker_request = BrokerRequest(
        "1", request.consumer_identity, request.repository_identity,
        request.worktree_identity, request.required_capability,
        request.minimum_freshness, None, None, False, None,
        16 * 1024, request.maximum_model_output_characters,
        request.provider_identity,
    )
    decision = route_provider(refreshed, broker_request, consent, utc_now=utc_now)
    if decision.status is RoutingStatus.SELECTED and decision.selected_provider:
        provider = next(item for item in refreshed.providers if item.provider_identity == decision.selected_provider)
        routed_request = _routed_request(
            request, provider.provider_identity,
            inspections.get(provider.provider_identity),
        )
        try:
            queried = query_call(provider, routed_request)
            if isinstance(queried, tuple) and len(queried) == 2:
                result, attempt = queried
                attempts.append(attempt)
            else:
                result = queried
            return BrokerExecution(result, provider.provider_identity, tuple(attempts), tuple(sorted(set(reasons))), "use-cited-evidence")
        except Exception as error:
            reasons.append(_reason_code(error, "provider-query-failed"))
    if fallback_call is not None:
        result = fallback_call(_fallback_request(request, snapshot))
        return BrokerExecution(result, "bounded-fallback", tuple(attempts), tuple(sorted(set(reasons))), "review-bounded-evidence")
    return BrokerExecution(None, "none", tuple(attempts), tuple(sorted(set(reasons + list(decision.selection_reason_codes)))), decision.next_safe_action)


def _fallback_request(
    request: Level1Request, snapshot: RepositorySnapshot
) -> Level1Request:
    index_identity = request.index_identity
    if request.operation not in {Level1Operation.ESTIMATE, Level1Operation.BUILD}:
        material = "\0".join(
            (
                "taf-bounded-fallback-v1",
                snapshot.repository_identity,
                snapshot.worktree_identity,
                snapshot.head_sha or "unborn",
                snapshot.dirty_fingerprint,
            )
        ).encode("utf-8")
        index_identity = "sha256:" + hashlib.sha256(material).hexdigest()
    return replace(
        request,
        provider_identity="taf.bounded-fallback",
        index_identity=index_identity,
    )


def _routed_request(
    request: Level1Request,
    provider_identity: str,
    inspection: InspectionRecord | None,
) -> Level1Request:
    index_identity = request.index_identity
    if (
        inspection is not None
        and request.operation not in {
            Level1Operation.ESTIMATE,
            Level1Operation.BUILD,
        }
    ):
        index_identity = inspection.index_identity
    return replace(
        request,
        provider_identity=provider_identity,
        index_identity=index_identity,
    )


def _reason_code(error: Exception, fallback: str) -> str:
    if isinstance(error, ProviderProcessError):
        return error.reason_code
    return fallback
