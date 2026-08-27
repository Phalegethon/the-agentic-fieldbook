"""Deterministic active inspection, routing, query, and fallback orchestration."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Mapping

from .consent import AuthorizationLedger
from .level1_models import Level1Request
from .models import ContextAction, Freshness, RepositorySnapshot
from .provider_freshness import derive_provider_freshness, refine_descriptor
from .provider_models import BrokerRequest, DiscoverySnapshot, ProviderDescriptor, RoutingStatus
from .routing import route_provider


@dataclass(frozen=True)
class BrokerExecution:
    result: object
    route: str
    attempts: tuple[object, ...]
    reason_codes: tuple[str, ...]
    next_safe_action: str


def execute_broker_request(
    discovery: DiscoverySnapshot,
    request: Level1Request,
    consent: AuthorizationLedger,
    snapshot: RepositorySnapshot,
    adapters: Mapping[str, object],
    *,
    inspect_call: Callable[[ProviderDescriptor], tuple[object, object]],
    query_call: Callable[[ProviderDescriptor, Level1Request], object],
    fallback_call: Callable[[Level1Request], object] | None,
    utc_now: str,
) -> BrokerExecution:
    """Inspect only authorized candidates, reroute once, and execute one route."""
    attempts: list[object] = []
    reasons: list[str] = []
    refined: list[ProviderDescriptor] = []
    for provider in sorted(discovery.providers, key=lambda item: item.provider_identity):
        if provider.provider_identity not in adapters:
            refined.append(provider)
            continue
        if request.required_capability not in provider.capabilities:
            refined.append(provider)
            continue
        if not consent.is_authorized(ContextAction.INSPECT, discovery.repository_identity, provider.provider_identity, provider.provider_schema_version):
            refined.append(provider)
            continue
        try:
            inspected, attempt = inspect_call(provider)
            assessment = derive_provider_freshness(inspected, snapshot)
            refined.append(refine_descriptor(provider, inspected, assessment))
            attempts.append(attempt)
        except Exception as error:
            code = getattr(error, "reason_code", str(error) or "provider-inspection-failed")
            reasons.append(code if len(code) <= 256 else "provider-inspection-failed")
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
        queried = query_call(provider, request)
        if isinstance(queried, tuple) and len(queried) == 2:
            result, attempt = queried
            attempts.append(attempt)
        else:
            result = queried
        return BrokerExecution(result, provider.provider_identity, tuple(attempts), tuple(sorted(set(reasons))), "use-cited-evidence")
    if fallback_call is not None:
        result = fallback_call(request)
        return BrokerExecution(result, "bounded-fallback", tuple(attempts), tuple(sorted(set(reasons))), "review-bounded-evidence")
    return BrokerExecution(None, "none", tuple(attempts), tuple(sorted(set(reasons + list(decision.selection_reason_codes)))), decision.next_safe_action)
