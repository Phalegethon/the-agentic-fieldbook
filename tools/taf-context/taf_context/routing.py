"""Pure, native-first, deterministic routing over passive discovery evidence."""

from __future__ import annotations

from dataclasses import dataclass, replace

from .consent import AuthorizationLedger, ConsentDisposition
from .models import Confidence, ContextAction, Freshness, canonical_json
from .provider_models import (
    Availability,
    BrokerRequest,
    ConsentRequest,
    DiscoverySnapshot,
    DiscoverySource,
    ProviderDescriptor,
    ProviderLocality,
    Registration,
    RejectedAlternative,
    RoutingDecision,
    RoutingStatus,
    StatusEvidence,
)


FRESHNESS_RANK = {
    Freshness.UNUSABLE: 0,
    Freshness.UNKNOWN: 1,
    Freshness.STRUCTURALLY_STALE: 2,
    Freshness.PARTIAL: 3,
    Freshness.INCREMENTALLY_STALE: 4,
    Freshness.COMMIT_FRESH_WORKTREE_STALE: 5,
    Freshness.EXACT: 6,
}

_CONFIDENCE_RANK = {
    Confidence.VERIFIED: 0,
    Confidence.INFERRED: 1,
    Confidence.UNCERTAIN: 2,
}
_EVIDENCE_RANK = {
    StatusEvidence.PROVIDER_INSPECTED: 0,
    StatusEvidence.MANIFEST_VALIDATED: 1,
    StatusEvidence.UNINSPECTED: 2,
}
_NATIVE_IDENTITY = "taf-context"
_MAX_REJECTION_SUMMARIES = 5
_MAX_COUNTER = 2**31 - 1
_MAX_MACHINE_OUTPUT_BYTES = 16 * 1024
_MAX_MODEL_OUTPUT_CHARACTERS = 2000
_ESCALATABLE_ACTIONS = frozenset(
    (ContextAction.INSPECT, ContextAction.NETWORK, ContextAction.QUERY)
)


@dataclass(frozen=True)
class _Evaluation:
    provider: ProviderDescriptor
    structural_reasons: tuple[str, ...]
    missing_actions: tuple[ContextAction, ...]
    denied_actions: tuple[ContextAction, ...]
    eligible: bool

    @property
    def reason_codes(self) -> tuple[str, ...]:
        reasons = set(self.structural_reasons)
        if self.provider.status_evidence is StatusEvidence.UNINSPECTED:
            reasons.add("provider-uninspected")
        reasons.update(
            f"consent-{action.value}-required" for action in self.missing_actions
        )
        reasons.update(
            f"consent-{action.value}-denied" for action in self.denied_actions
        )
        return tuple(sorted(reasons))


def route_provider(
    discovery: DiscoverySnapshot,
    request: BrokerRequest,
    consent: AuthorizationLedger,
    *,
    utc_now: str,
    consent_state_usable: bool = True,
) -> RoutingDecision:
    """Return one bounded decision without provider, filesystem, or network I/O."""
    if not isinstance(discovery, DiscoverySnapshot):
        raise TypeError("discovery-invalid")
    if not isinstance(request, BrokerRequest):
        raise TypeError("request-invalid")
    if not isinstance(consent, AuthorizationLedger):
        raise TypeError("consent-invalid")
    if not isinstance(utc_now, str) or not utc_now or len(utc_now) > 256:
        raise ValueError("utc-now-invalid")
    if type(consent_state_usable) is not bool:
        raise TypeError("consent-state-usable-invalid")

    providers = tuple(sorted(discovery.providers, key=_provider_identity_key))
    native = next(
        (provider for provider in providers if provider.provider_identity == _NATIVE_IDENTITY),
        None,
    )
    native_reasons: tuple[str, ...] = ()
    if native is not None:
        native_reasons = _structural_reasons(
            native, discovery, request, native=True
        )
        if not native_reasons:
            return _finalize(
                request=request,
                status=RoutingStatus.NATIVE_FALLBACK,
                selected_provider=_NATIVE_IDENTITY,
                selection_reason_codes=("native-level0-satisfies-request",),
                rejected_alternatives=(),
                eligible_count=1,
                rejected_count=discovery.rejected_provider_count,
                omitted_count=discovery.omitted_provider_count,
                consent_requests=(),
                escalation_required=False,
                next_safe_action="use-native-context",
            )

    evaluations = tuple(
        _evaluate_external(
            provider,
            discovery,
            request,
            consent,
            consent_state_usable=consent_state_usable,
        )
        for provider in providers
        if provider.provider_identity != _NATIVE_IDENTITY
    )
    eligible = tuple(item for item in evaluations if item.eligible)
    rejected = list(item for item in evaluations if not item.eligible)
    if native is not None and native_reasons:
        rejected.append(
            _Evaluation(native, native_reasons, (), (), False)
        )

    rejected_alternatives = _rejected_alternatives(rejected)
    rejected_count = discovery.rejected_provider_count + len(rejected)
    if rejected_count > _MAX_COUNTER:
        raise ValueError("rejected-count-overflow")

    if eligible:
        selected = min(
            eligible,
            key=lambda item: _rank_key(item.provider, request),
        ).provider
        reasons = {"eligible"}
        if request.preferred_provider == selected.provider_identity:
            reasons.add("preferred-provider")
        return _finalize(
            request=request,
            status=RoutingStatus.SELECTED,
            selected_provider=selected.provider_identity,
            selection_reason_codes=tuple(sorted(reasons)),
            rejected_alternatives=rejected_alternatives,
            eligible_count=len(eligible),
            rejected_count=rejected_count,
            omitted_count=discovery.omitted_provider_count,
            consent_requests=(),
            escalation_required=False,
            next_safe_action="query-selected-provider",
        )

    escalation = _choose_escalation(evaluations, request)
    if escalation is not None:
        evaluation, actions = escalation
        consent_request = ConsentRequest.create(
            schema_version="1",
            repository_identity=request.repository_identity,
            provider_identity=evaluation.provider.provider_identity,
            provider_schema_version=evaluation.provider.provider_schema_version,
            actions=actions,
            locality=evaluation.provider.locality,
            data_surface=(
                "repository-metadata"
                if evaluation.provider.locality is ProviderLocality.LOCAL
                else "repository-metadata-network"
            ),
            fallback="native-level-0",
            requested_at=utc_now,
        )
        return _finalize(
            request=request,
            status=RoutingStatus.CONSENT_REQUIRED,
            selected_provider=None,
            selection_reason_codes=("consent-required",),
            rejected_alternatives=rejected_alternatives,
            eligible_count=0,
            rejected_count=rejected_count,
            omitted_count=discovery.omitted_provider_count,
            consent_requests=(consent_request,),
            escalation_required=True,
            next_safe_action=_consent_action_token(actions),
        )

    if not consent_state_usable and evaluations:
        next_safe_action = "repair-consent-store"
    elif any(item.denied_actions for item in evaluations):
        next_safe_action = "respect-consent-denial"
    elif any(
        item.provider.status_evidence is StatusEvidence.UNINSPECTED
        and not item.structural_reasons
        and ContextAction.INSPECT not in item.missing_actions
        and ContextAction.INSPECT not in item.denied_actions
        for item in evaluations
    ):
        next_safe_action = "inspect-provider"
    else:
        next_safe_action = "provide-more-context"
    return _finalize(
        request=request,
        status=RoutingStatus.INSUFFICIENT_CONTEXT,
        selected_provider=None,
        selection_reason_codes=("no-eligible-provider",),
        rejected_alternatives=rejected_alternatives,
        eligible_count=0,
        rejected_count=rejected_count,
        omitted_count=discovery.omitted_provider_count,
        consent_requests=(),
        escalation_required=False,
        next_safe_action=next_safe_action,
    )


def _evaluate_external(
    provider: ProviderDescriptor,
    discovery: DiscoverySnapshot,
    request: BrokerRequest,
    consent: AuthorizationLedger,
    *,
    consent_state_usable: bool,
) -> _Evaluation:
    structural = list(_structural_reasons(provider, discovery, request, native=False))
    required_actions = set(provider.required_actions)
    required_actions.add(ContextAction.QUERY)
    if provider.locality is ProviderLocality.NETWORK_BACKED:
        required_actions.add(ContextAction.NETWORK)
    if provider.status_evidence is StatusEvidence.UNINSPECTED:
        required_actions.add(ContextAction.INSPECT)

    unsupported = required_actions - set(provider.supported_actions)
    structural.extend(
        f"action-{action.value}-unsupported" for action in unsupported
    )

    if not consent_state_usable:
        structural.append("consent-store-corrupt")
        missing: tuple[ContextAction, ...] = ()
        denied: tuple[ContextAction, ...] = ()
    else:
        missing_items: list[ContextAction] = []
        denied_items: list[ContextAction] = []
        for action in sorted(required_actions, key=lambda item: item.value):
            disposition = consent.decision_for(
                action,
                request.repository_identity,
                provider.provider_identity,
                provider.provider_schema_version,
            )
            if disposition is ConsentDisposition.DENY:
                denied_items.append(action)
            elif disposition is not ConsentDisposition.ALLOW:
                missing_items.append(action)
        missing = tuple(missing_items)
        denied = tuple(denied_items)

    structural_tuple = tuple(sorted(set(structural)))
    eligible = bool(
        not structural_tuple
        and provider.status_evidence is not StatusEvidence.UNINSPECTED
        and not missing
        and not denied
    )
    return _Evaluation(provider, structural_tuple, missing, denied, eligible)


def _structural_reasons(
    provider: ProviderDescriptor,
    discovery: DiscoverySnapshot,
    request: BrokerRequest,
    *,
    native: bool,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if discovery.repository_identity != request.repository_identity:
        reasons.append("repository-mismatch")
    if discovery.worktree_identity != request.worktree_identity:
        reasons.append("worktree-mismatch")
    if provider.availability is not Availability.AVAILABLE:
        reasons.append("provider-not-available")
    if request.required_capability not in provider.capabilities:
        reasons.append("capability-missing")
    if FRESHNESS_RANK[provider.freshness] < FRESHNESS_RANK[request.minimum_freshness]:
        reasons.append("freshness-insufficient")
    _coverage_reason(
        reasons,
        "path",
        provider.path_coverage,
        request.minimum_path_coverage,
    )
    _coverage_reason(
        reasons,
        "language",
        provider.language_coverage,
        request.minimum_language_coverage,
    )
    if (
        provider.locality is ProviderLocality.NETWORK_BACKED
        and not request.network_acceptable
    ):
        reasons.append("network-not-acceptable")
    if (
        request.maximum_latency_ms is not None
        and provider.latency_ms is not None
        and provider.latency_ms > request.maximum_latency_ms
    ):
        reasons.append("latency-exceeded")
    if native and not _valid_native_evidence(provider):
        reasons.append("native-evidence-invalid")
    return tuple(sorted(set(reasons)))


def _coverage_reason(
    reasons: list[str],
    name: str,
    actual: float | None,
    minimum: float | None,
) -> None:
    if minimum is None:
        return
    if actual is None:
        reasons.append(f"{name}-coverage-unknown")
    elif actual < minimum:
        reasons.append(f"{name}-coverage-insufficient")


def _valid_native_evidence(provider: ProviderDescriptor) -> bool:
    return bool(
        provider.provider_identity == _NATIVE_IDENTITY
        and provider.locality is ProviderLocality.LOCAL
        and provider.registration is Registration.NATIVE
        and provider.discovery_sources == (DiscoverySource.NATIVE,)
        and provider.status_evidence is not StatusEvidence.UNINSPECTED
        and provider.required_actions == ()
        and ContextAction.QUERY in provider.supported_actions
    )


def _choose_escalation(
    evaluations: tuple[_Evaluation, ...],
    request: BrokerRequest,
) -> tuple[_Evaluation, tuple[ContextAction, ...]] | None:
    candidates: list[tuple[int, tuple[object, ...], _Evaluation, tuple[ContextAction, ...]]] = []
    for evaluation in evaluations:
        if evaluation.structural_reasons:
            continue
        preferred = request.preferred_provider == evaluation.provider.provider_identity
        if evaluation.denied_actions and not preferred:
            continue
        allowed_for_stage, priority = _escalation_stage(evaluation.provider)
        actions = tuple(
            sorted(
                (
                    set(evaluation.missing_actions)
                    | (set(evaluation.denied_actions) if preferred else set())
                )
                & allowed_for_stage
                & _ESCALATABLE_ACTIONS,
                key=lambda item: item.value,
            )
        )
        if not actions:
            continue
        candidates.append(
            (priority, _rank_key(evaluation.provider, request), evaluation, actions)
        )
    if not candidates:
        return None
    _, _, evaluation, actions = min(
        candidates, key=lambda item: (item[0], item[1])
    )
    return evaluation, actions


def _escalation_stage(
    provider: ProviderDescriptor,
) -> tuple[frozenset[ContextAction], int]:
    uninspected = provider.status_evidence is StatusEvidence.UNINSPECTED
    if provider.locality is ProviderLocality.LOCAL:
        if uninspected:
            return frozenset((ContextAction.INSPECT,)), 0
        return frozenset((ContextAction.QUERY,)), 1
    if uninspected:
        return frozenset((ContextAction.INSPECT, ContextAction.NETWORK)), 2
    return frozenset((ContextAction.QUERY, ContextAction.NETWORK)), 3


def _rank_key(
    provider: ProviderDescriptor, request: BrokerRequest
) -> tuple[object, ...]:
    path_coverage = -1.0 if provider.path_coverage is None else provider.path_coverage
    language_coverage = (
        -1.0 if provider.language_coverage is None else provider.language_coverage
    )
    latency = float("inf") if provider.latency_ms is None else provider.latency_ms
    return (
        0 if request.preferred_provider == provider.provider_identity else 1,
        len(provider.capabilities) - 1,
        -FRESHNESS_RANK[provider.freshness],
        -path_coverage,
        -language_coverage,
        0 if provider.locality is ProviderLocality.LOCAL else 1,
        latency,
        _CONFIDENCE_RANK[provider.confidence],
        _EVIDENCE_RANK[provider.status_evidence],
        provider.provider_identity,
    )


def _provider_identity_key(provider: ProviderDescriptor) -> tuple[str, str]:
    return provider.provider_identity, canonical_json(provider.to_dict())


def _rejected_alternatives(
    rejected: list[_Evaluation],
) -> tuple[RejectedAlternative, ...]:
    return tuple(
        RejectedAlternative(item.provider.provider_identity, item.reason_codes)
        for item in sorted(rejected, key=lambda entry: _provider_identity_key(entry.provider))[
            :_MAX_REJECTION_SUMMARIES
        ]
    )


def _consent_action_token(actions: tuple[ContextAction, ...]) -> str:
    if len(actions) == 1:
        return f"request-{actions[0].value}-consent"
    return "request-provider-consent"


def _finalize(
    *,
    request: BrokerRequest,
    status: RoutingStatus,
    selected_provider: str | None,
    selection_reason_codes: tuple[str, ...],
    rejected_alternatives: tuple[RejectedAlternative, ...],
    eligible_count: int,
    rejected_count: int,
    omitted_count: int,
    consent_requests: tuple[ConsentRequest, ...],
    escalation_required: bool,
    next_safe_action: str,
) -> RoutingDecision:
    summary, omitted_lines = _render_summary(
        status=status,
        selected_provider=selected_provider,
        selection_reason_codes=selection_reason_codes,
        consent_requests=consent_requests,
        maximum_characters=min(
            request.maximum_model_output_characters,
            _MAX_MODEL_OUTPUT_CHARACTERS,
        ),
    )
    reasons = set(selection_reason_codes)
    if omitted_lines:
        reasons.add(f"model-summary-lines-omitted-{omitted_lines}")
    result = RoutingDecision(
        schema_version="1",
        status=status,
        selected_provider=selected_provider,
        selection_reason_codes=tuple(sorted(reasons)),
        rejected_alternatives=rejected_alternatives,
        eligible_count=eligible_count,
        rejected_count=rejected_count,
        omitted_count=omitted_count,
        consent_requests=consent_requests,
        escalation_required=escalation_required,
        next_safe_action=next_safe_action,
        model_summary=summary,
        output_bytes=0,
        output_characters=len(summary),
    )
    measured = _with_exact_output_bytes(result)
    machine_limit = min(
        request.maximum_machine_output_bytes, _MAX_MACHINE_OUTPUT_BYTES
    )
    if measured.output_bytes <= machine_limit:
        return measured

    original_alternatives = measured.rejected_alternatives
    compact_reasons = set(measured.selection_reason_codes)
    compact_summary = ""
    compact_reasons.add(f"model-summary-characters-omitted-{len(summary)}")
    for kept in range(len(original_alternatives), -1, -1):
        compact = replace(
            measured,
            selection_reason_codes=tuple(
                sorted(
                    compact_reasons
                    | {
                        f"machine-rejections-omitted-{len(original_alternatives) - kept}"
                    }
                )
            ),
            rejected_alternatives=original_alternatives[:kept],
            model_summary=compact_summary,
            output_bytes=0,
            output_characters=0,
        )
        compact = _with_exact_output_bytes(compact)
        if compact.output_bytes <= machine_limit:
            return compact
    raise ValueError("machine-output-budget-too-small")


def _render_summary(
    *,
    status: RoutingStatus,
    selected_provider: str | None,
    selection_reason_codes: tuple[str, ...],
    consent_requests: tuple[ConsentRequest, ...],
    maximum_characters: int,
) -> tuple[str, int]:
    if consent_requests:
        consent_line = "consent-required: " + ",".join(
            action.value for action in consent_requests[0].actions
        )
    else:
        consent_line = "consent-required: none"
    lines = (
        f"decision: {status.value}",
        f"selected-provider: {selected_provider or 'none'}",
        "reasons: " + (",".join(selection_reason_codes) or "none"),
        consent_line,
        "fallback: native-level-0",
    )
    accepted: list[str] = []
    for line in lines:
        candidate = "\n".join((*accepted, line))
        if len(candidate) <= maximum_characters:
            accepted.append(line)
        else:
            break
    return "\n".join(accepted), len(lines) - len(accepted)


def _with_exact_output_bytes(result: RoutingDecision) -> RoutingDecision:
    output_bytes = result.output_bytes
    for _ in range(4):
        candidate = replace(result, output_bytes=output_bytes)
        measured = len(canonical_json(candidate.to_dict()).encode("utf-8"))
        if measured == output_bytes:
            return candidate
        output_bytes = measured
    raise RuntimeError("output-byte-count-did-not-converge")
