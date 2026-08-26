"""Behavior tests for pure, deterministic context-provider routing."""

from __future__ import annotations

import itertools
import unittest
from dataclasses import replace

from taf_context.consent import AuthorizationLedger, ConsentDisposition
from taf_context.models import Confidence, ContextAction, Freshness, canonical_json
from taf_context.provider_models import (
    Availability,
    BrokerRequest,
    ConsentRequest,
    DiscoverySnapshot,
    DiscoverySource,
    ProviderDescriptor,
    ProviderLocality,
    Registration,
    RoutingDecision,
    RoutingStatus,
    StatusEvidence,
)
from taf_context.routing import route_provider


_UTC_NOW = "2026-08-26T12:00:00Z"
_REPOSITORY = "sha256:repo"
_WORKTREE = "sha256:worktree"


class DeterministicRoutingTests(unittest.TestCase):
    def test_level_zero_satisfies_repository_map_without_external_consent(self) -> None:
        external = _descriptor("external.graph", capability="repository-map")

        decision = route_provider(
            _discovery(external),
            _request(
                required_capability="repository-map",
                minimum_language_coverage=None,
                maximum_latency_ms=None,
            ),
            AuthorizationLedger(),
            utc_now=_UTC_NOW,
        )

        self.assertIs(decision.status, RoutingStatus.NATIVE_FALLBACK)
        self.assertEqual(decision.selected_provider, "taf-context")
        self.assertEqual(decision.consent_requests, ())
        self.assertFalse(decision.escalation_required)
        self.assertEqual(decision.next_safe_action, "use-native-context")

    def test_native_consent_bypass_requires_native_level_zero_action_evidence(self) -> None:
        spoofed_native = replace(
            _native(),
            required_actions=(ContextAction.INSTALL,),
            supported_actions=(ContextAction.INSTALL, ContextAction.QUERY),
        )
        discovery = replace(_discovery(), providers=(spoofed_native,))

        decision = route_provider(
            discovery,
            _request(
                required_capability="repository-map",
                minimum_language_coverage=None,
                maximum_latency_ms=None,
            ),
            AuthorizationLedger(),
            utc_now=_UTC_NOW,
        )

        self.assertIs(decision.status, RoutingStatus.INSUFFICIENT_CONTEXT)
        self.assertIn("native-evidence-invalid", _reasons(decision, "taf-context"))

    def test_candidate_only_provider_is_ineligible_and_never_prompted(self) -> None:
        candidate = _descriptor(
            "candidate.graph",
            availability=Availability.CANDIDATE,
            status_evidence=StatusEvidence.UNINSPECTED,
            freshness=Freshness.UNUSABLE,
        )

        decision = _route(candidate)

        self.assertIs(decision.status, RoutingStatus.INSUFFICIENT_CONTEXT)
        self.assertEqual(decision.consent_requests, ())
        self.assertIn("provider-not-available", _reasons(decision, "candidate.graph"))

    def test_uninspected_local_provider_requests_only_missing_inspect(self) -> None:
        provider = _descriptor(
            "local.graph", status_evidence=StatusEvidence.UNINSPECTED
        )

        decision = _route(provider)

        self.assertIs(decision.status, RoutingStatus.CONSENT_REQUIRED)
        self.assertIsNone(decision.selected_provider)
        self.assertEqual(len(decision.consent_requests), 1)
        self.assertEqual(decision.consent_requests[0].provider_identity, "local.graph")
        self.assertEqual(decision.consent_requests[0].actions, (ContextAction.INSPECT,))
        self.assertEqual(
            ConsentRequest.from_dict(decision.consent_requests[0].to_dict()),
            decision.consent_requests[0],
        )

    def test_inspected_local_with_inspect_allow_requests_only_missing_query(self) -> None:
        provider = _descriptor(
            "local.graph", status_evidence=StatusEvidence.PROVIDER_INSPECTED
        )
        consent = _record(provider, ConsentDisposition.ALLOW, ContextAction.INSPECT)

        decision = _route(provider, consent=consent)

        self.assertIs(decision.status, RoutingStatus.CONSENT_REQUIRED)
        self.assertEqual(decision.consent_requests[0].actions, (ContextAction.QUERY,))

    def test_network_provider_with_query_allow_requests_only_missing_network(self) -> None:
        provider = _descriptor(
            "network.graph",
            locality=ProviderLocality.NETWORK_BACKED,
            status_evidence=StatusEvidence.PROVIDER_INSPECTED,
        )
        consent = _record(provider, ConsentDisposition.ALLOW, ContextAction.QUERY)

        decision = _route(
            provider,
            consent=consent,
            request=_request(network_acceptable=True),
        )

        self.assertIs(decision.status, RoutingStatus.CONSENT_REQUIRED)
        self.assertEqual(decision.consent_requests[0].actions, (ContextAction.NETWORK,))

    def test_exact_query_deny_suppresses_repeat_prompt(self) -> None:
        provider = _descriptor(
            "local.graph", status_evidence=StatusEvidence.PROVIDER_INSPECTED
        )
        denied = _record(provider, ConsentDisposition.DENY, ContextAction.QUERY)

        decision = _route(provider, consent=denied)

        self.assertIs(decision.status, RoutingStatus.INSUFFICIENT_CONTEXT)
        self.assertEqual(decision.consent_requests, ())
        self.assertFalse(decision.escalation_required)
        self.assertIn("consent-query-denied", _reasons(decision, "local.graph"))
        self.assertEqual(decision.next_safe_action, "respect-consent-denial")

    def test_explicit_preference_can_reprompt_exact_deny_but_never_selects(self) -> None:
        provider = _descriptor(
            "local.graph", status_evidence=StatusEvidence.PROVIDER_INSPECTED
        )
        denied = _record(
            provider,
            ConsentDisposition.DENY,
            ContextAction.QUERY,
            requested_at="2026-08-26T11:00:00Z",
        )

        decision = _route(
            provider,
            consent=denied,
            request=_request(preferred_provider="local.graph"),
        )

        self.assertIs(decision.status, RoutingStatus.CONSENT_REQUIRED)
        self.assertIsNone(decision.selected_provider)
        self.assertEqual(decision.consent_requests[0].actions, (ContextAction.QUERY,))
        self.assertEqual(decision.consent_requests[0].requested_at, _UTC_NOW)
        self.assertNotEqual(
            decision.consent_requests[0].digest,
            denied.records[0].request_digest.removeprefix("sha256:"),
        )

    def test_unusable_consent_state_fails_closed_without_prompt(self) -> None:
        provider = _descriptor(
            "local.graph", status_evidence=StatusEvidence.PROVIDER_INSPECTED
        )
        allowed = _record(provider, ConsentDisposition.ALLOW, ContextAction.QUERY)

        decision = route_provider(
            _discovery(provider),
            _request(),
            allowed,
            utc_now=_UTC_NOW,
            consent_state_usable=False,
        )

        self.assertIs(decision.status, RoutingStatus.INSUFFICIENT_CONTEXT)
        self.assertIsNone(decision.selected_provider)
        self.assertEqual(decision.consent_requests, ())
        self.assertIn("consent-store-corrupt", _reasons(decision, "local.graph"))
        self.assertEqual(decision.next_safe_action, "repair-consent-store")

    def test_stale_and_partial_providers_are_ineligible_without_automatic_update(self) -> None:
        stale = _descriptor(
            "stale.graph",
            freshness=Freshness.STRUCTURALLY_STALE,
            status_evidence=StatusEvidence.PROVIDER_INSPECTED,
        )
        partial = _descriptor(
            "partial.graph",
            freshness=Freshness.PARTIAL,
            status_evidence=StatusEvidence.PROVIDER_INSPECTED,
        )
        consent = _record(stale, ConsentDisposition.ALLOW, ContextAction.QUERY)
        consent = _record(
            partial, ConsentDisposition.ALLOW, ContextAction.QUERY, ledger=consent
        )

        decision = route_provider(
            _discovery(stale, partial),
            _request(minimum_freshness=Freshness.INCREMENTALLY_STALE),
            consent,
            utc_now=_UTC_NOW,
        )

        self.assertIs(decision.status, RoutingStatus.INSUFFICIENT_CONTEXT)
        self.assertEqual(decision.consent_requests, ())
        self.assertIn("freshness-insufficient", _reasons(decision, "stale.graph"))
        self.assertIn("freshness-insufficient", _reasons(decision, "partial.graph"))
        self.assertNotIn("update", decision.next_safe_action)

    def test_repository_and_worktree_mismatch_are_ineligible(self) -> None:
        provider = _descriptor(
            "local.graph", status_evidence=StatusEvidence.PROVIDER_INSPECTED
        )
        consent = _record(provider, ConsentDisposition.ALLOW, ContextAction.QUERY)
        cases = (
            (replace(_discovery(provider), repository_identity="sha256:other"), "repository-mismatch"),
            (replace(_discovery(provider), worktree_identity="sha256:other"), "worktree-mismatch"),
        )

        for discovery, reason in cases:
            with self.subTest(reason=reason):
                decision = route_provider(
                    discovery, _request(), consent, utc_now=_UTC_NOW
                )
                self.assertIs(decision.status, RoutingStatus.INSUFFICIENT_CONTEXT)
                self.assertEqual(decision.consent_requests, ())
                self.assertIn(reason, _reasons(decision, "local.graph"))

    def test_preferred_provider_ranks_first_only_when_otherwise_eligible(self) -> None:
        preferred = _descriptor(
            "preferred.graph",
            freshness=Freshness.INCREMENTALLY_STALE,
            path_coverage=0.8,
            latency_ms=9.0,
            confidence=Confidence.INFERRED,
            status_evidence=StatusEvidence.PROVIDER_INSPECTED,
        )
        stronger = _descriptor(
            "stronger.graph", status_evidence=StatusEvidence.PROVIDER_INSPECTED
        )
        consent = _allow_query(preferred, stronger)
        request = _request(
            minimum_freshness=Freshness.STRUCTURALLY_STALE,
            minimum_path_coverage=0.5,
        )

        ordinary = route_provider(
            _discovery(preferred, stronger), request, consent, utc_now=_UTC_NOW
        )
        explicit = route_provider(
            _discovery(preferred, stronger),
            replace(request, preferred_provider="preferred.graph"),
            consent,
            utc_now=_UTC_NOW,
        )
        ineligible = route_provider(
            _discovery(replace(preferred, freshness=Freshness.UNUSABLE), stronger),
            replace(request, preferred_provider="preferred.graph"),
            consent,
            utc_now=_UTC_NOW,
        )

        self.assertEqual(ordinary.selected_provider, "stronger.graph")
        self.assertEqual(explicit.selected_provider, "preferred.graph")
        self.assertEqual(ineligible.selected_provider, "stronger.graph")

    def test_ranking_uses_freshness_coverage_locality_latency_confidence_then_identity(self) -> None:
        cases = (
            (
                _descriptor("winner.fresh", freshness=Freshness.EXACT, path_coverage=0.7),
                _descriptor("loser.fresh", freshness=Freshness.INCREMENTALLY_STALE, path_coverage=1.0),
                "winner.fresh",
            ),
            (
                _descriptor("winner.coverage", path_coverage=1.0, language_coverage=1.0),
                _descriptor("loser.coverage", path_coverage=0.9, language_coverage=0.9),
                "winner.coverage",
            ),
            (
                _descriptor("winner.local", locality=ProviderLocality.LOCAL, latency_ms=9.0),
                _descriptor("loser.local", locality=ProviderLocality.NETWORK_BACKED, latency_ms=1.0),
                "winner.local",
            ),
            (
                _descriptor("winner.latency", latency_ms=1.0, confidence=Confidence.INFERRED),
                _descriptor("loser.latency", latency_ms=2.0, confidence=Confidence.VERIFIED),
                "winner.latency",
            ),
            (
                _descriptor("winner.confidence", confidence=Confidence.VERIFIED),
                _descriptor("loser.confidence", confidence=Confidence.INFERRED),
                "winner.confidence",
            ),
            (_descriptor("alpha.graph"), _descriptor("beta.graph"), "alpha.graph"),
        )

        for left, right, expected in cases:
            left = replace(left, status_evidence=StatusEvidence.PROVIDER_INSPECTED)
            right = replace(right, status_evidence=StatusEvidence.PROVIDER_INSPECTED)
            consent = _allow_query(left, right)
            for provider in (left, right):
                if provider.locality is ProviderLocality.NETWORK_BACKED:
                    consent = _record(
                        provider,
                        ConsentDisposition.ALLOW,
                        ContextAction.NETWORK,
                        ledger=consent,
                    )
            with self.subTest(expected=expected):
                decision = route_provider(
                    _discovery(left, right),
                    _request(
                        minimum_freshness=Freshness.STRUCTURALLY_STALE,
                        minimum_path_coverage=0.5,
                        minimum_language_coverage=0.5,
                        network_acceptable=True,
                    ),
                    consent,
                    utc_now=_UTC_NOW,
                )
                self.assertIs(decision.status, RoutingStatus.SELECTED)
                self.assertEqual(decision.selected_provider, expected)

    def test_rejections_display_five_summaries_and_preserve_exact_counts(self) -> None:
        rejected = tuple(
            _descriptor(
                f"bad-{index:02d}",
                availability=Availability.CANDIDATE,
                freshness=Freshness.UNUSABLE,
            )
            for index in range(7)
        )

        decision = route_provider(
            _discovery(
                *rejected,
                rejected_provider_count=2,
                omitted_provider_count=3,
            ),
            _request(),
            AuthorizationLedger(),
            utc_now=_UTC_NOW,
        )

        self.assertEqual(len(decision.rejected_alternatives), 5)
        self.assertEqual(
            tuple(item.provider_identity for item in decision.rejected_alternatives),
            ("bad-00", "bad-01", "bad-02", "bad-03", "bad-04"),
        )
        self.assertEqual(decision.rejected_count, 10)
        self.assertEqual(decision.omitted_count, 3)

    def test_every_provider_permutation_is_byte_identical(self) -> None:
        providers = (
            _descriptor("alpha.graph", status_evidence=StatusEvidence.PROVIDER_INSPECTED),
            _descriptor("beta.graph", status_evidence=StatusEvidence.PROVIDER_INSPECTED),
            _descriptor("gamma.graph", status_evidence=StatusEvidence.PROVIDER_INSPECTED),
        )
        consent = _allow_query(*providers)
        wires = []

        for permutation in itertools.permutations(providers):
            discovery = replace(_discovery(), providers=(_native(), *permutation))
            decision = route_provider(
                discovery, _request(), consent, utc_now=_UTC_NOW
            )
            wires.append(canonical_json(decision.to_dict()).encode("utf-8"))

        self.assertTrue(all(wire == wires[0] for wire in wires[1:]))

    def test_output_budgets_use_whole_summary_lines_and_exact_fixed_point_bytes(self) -> None:
        providers = tuple(
            _descriptor(
                f"bad-{index:02d}",
                availability=Availability.CANDIDATE,
                freshness=Freshness.UNUSABLE,
            )
            for index in range(7)
        )
        request = _request(
            maximum_machine_output_bytes=16 * 1024,
            maximum_model_output_characters=35,
        )

        first = route_provider(
            _discovery(*providers), request, AuthorizationLedger(), utc_now=_UTC_NOW
        )
        second = route_provider(
            _discovery(*reversed(providers)),
            request,
            AuthorizationLedger(),
            utc_now=_UTC_NOW,
        )

        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.model_summary, "decision: insufficient-context")
        self.assertLessEqual(first.output_characters, 35)
        self.assertEqual(first.output_characters, len(first.model_summary))
        self.assertIn("model-summary-lines-omitted-4", first.selection_reason_codes)
        self.assertEqual(
            first.output_bytes,
            len(canonical_json(first.to_dict()).encode("utf-8")),
        )
        self.assertLessEqual(first.output_bytes, 16 * 1024)
        self.assertEqual(RoutingDecision.from_dict(first.to_dict()), first)

        with self.assertRaisesRegex(ValueError, "^machine-output-budget-too-small$"):
            route_provider(
                _discovery(*providers),
                replace(request, maximum_machine_output_bytes=1),
                AuthorizationLedger(),
                utc_now=_UTC_NOW,
            )

    def test_model_summary_is_a_prefix_when_the_next_whole_line_does_not_fit(self) -> None:
        provider = _descriptor(
            "a" * 128, status_evidence=StatusEvidence.PROVIDER_INSPECTED
        )
        consent = _allow_query(provider)

        decision = route_provider(
            _discovery(provider),
            _request(maximum_model_output_characters=100),
            consent,
            utc_now=_UTC_NOW,
        )

        self.assertEqual(decision.model_summary, "decision: selected")
        self.assertIn("model-summary-lines-omitted-4", decision.selection_reason_codes)

    def test_rejected_count_overflow_fails_closed_instead_of_emitting_invalid_wire(self) -> None:
        candidate = _descriptor(
            "candidate.graph",
            availability=Availability.CANDIDATE,
            freshness=Freshness.UNUSABLE,
        )

        with self.assertRaisesRegex(ValueError, "^rejected-count-overflow$"):
            route_provider(
                _discovery(candidate, rejected_provider_count=2**31 - 1),
                _request(),
                AuthorizationLedger(),
                utc_now=_UTC_NOW,
            )


def _native() -> ProviderDescriptor:
    return ProviderDescriptor(
        schema_version="1",
        provider_identity="taf-context",
        provider_version="0.1.0",
        provider_schema_version="1",
        capabilities=("repository-map", "status"),
        locality=ProviderLocality.LOCAL,
        discovery_sources=(DiscoverySource.NATIVE,),
        availability=Availability.AVAILABLE,
        registration=Registration.NATIVE,
        status_evidence=StatusEvidence.MANIFEST_VALIDATED,
        freshness=Freshness.EXACT,
        path_coverage=1.0,
        language_coverage=None,
        latency_ms=None,
        confidence=Confidence.VERIFIED,
        supported_actions=(ContextAction.QUERY,),
        required_actions=(),
        marker_hints=(),
        reason_codes=(),
        warnings=(),
    )


def _descriptor(
    identity: str,
    *,
    capability: str = "semantic-search",
    availability: Availability = Availability.AVAILABLE,
    locality: ProviderLocality = ProviderLocality.LOCAL,
    status_evidence: StatusEvidence = StatusEvidence.MANIFEST_VALIDATED,
    freshness: Freshness = Freshness.EXACT,
    path_coverage: float | None = 1.0,
    language_coverage: float | None = 1.0,
    latency_ms: float | None = 1.0,
    confidence: Confidence = Confidence.VERIFIED,
) -> ProviderDescriptor:
    supported = (ContextAction.INSPECT, ContextAction.NETWORK, ContextAction.QUERY)
    return ProviderDescriptor(
        schema_version="1",
        provider_identity=identity,
        provider_version="1.0.0",
        provider_schema_version="1",
        capabilities=tuple(sorted((capability, "status"))),
        locality=locality,
        discovery_sources=(DiscoverySource.USER_REGISTRY,),
        availability=availability,
        registration=Registration.USER_REGISTERED,
        status_evidence=status_evidence,
        freshness=freshness,
        path_coverage=path_coverage,
        language_coverage=language_coverage,
        latency_ms=latency_ms,
        confidence=confidence,
        supported_actions=supported,
        required_actions=(),
        marker_hints=(),
        reason_codes=(),
        warnings=(),
    )


def _discovery(
    *providers: ProviderDescriptor,
    rejected_provider_count: int = 0,
    omitted_provider_count: int = 0,
) -> DiscoverySnapshot:
    ordered = tuple(sorted((_native(), *providers), key=lambda item: item.provider_identity))
    return DiscoverySnapshot(
        schema_version="1",
        repository_identity=_REPOSITORY,
        worktree_identity=_WORKTREE,
        inventory_fingerprint="sha256:inventory",
        providers=ordered,
        rejected_provider_count=rejected_provider_count,
        rejection_summaries=(),
        omitted_provider_count=omitted_provider_count,
        partial=bool(rejected_provider_count or omitted_provider_count),
        warnings=(),
        input_bytes=1,
        output_bytes=2,
    )


def _request(
    *,
    required_capability: str = "semantic-search",
    minimum_freshness: Freshness = Freshness.EXACT,
    minimum_path_coverage: float | None = 1.0,
    minimum_language_coverage: float | None = 1.0,
    network_acceptable: bool = False,
    maximum_latency_ms: float | None = 10.0,
    maximum_machine_output_bytes: int = 16 * 1024,
    maximum_model_output_characters: int = 2000,
    preferred_provider: str | None = None,
) -> BrokerRequest:
    return BrokerRequest(
        schema_version="1",
        consumer_identity="test-consumer",
        repository_identity=_REPOSITORY,
        worktree_identity=_WORKTREE,
        required_capability=required_capability,
        minimum_freshness=minimum_freshness,
        minimum_path_coverage=minimum_path_coverage,
        minimum_language_coverage=minimum_language_coverage,
        network_acceptable=network_acceptable,
        maximum_latency_ms=maximum_latency_ms,
        maximum_machine_output_bytes=maximum_machine_output_bytes,
        maximum_model_output_characters=maximum_model_output_characters,
        preferred_provider=preferred_provider,
    )


def _record(
    provider: ProviderDescriptor,
    disposition: ConsentDisposition,
    *actions: ContextAction,
    ledger: AuthorizationLedger | None = None,
    requested_at: str = "2026-08-26T10:00:00Z",
) -> AuthorizationLedger:
    ordered = tuple(sorted(set(actions), key=lambda item: item.value))
    request = ConsentRequest.create(
        schema_version="1",
        repository_identity=_REPOSITORY,
        provider_identity=provider.provider_identity,
        provider_schema_version=provider.provider_schema_version,
        actions=ordered,
        locality=provider.locality,
        data_surface="repository-metadata",
        fallback="native-level-0",
        requested_at=requested_at,
    )
    return (ledger or AuthorizationLedger()).record(
        request, disposition, requested_at
    )


def _allow_query(*providers: ProviderDescriptor) -> AuthorizationLedger:
    ledger = AuthorizationLedger()
    for provider in providers:
        ledger = _record(
            provider,
            ConsentDisposition.ALLOW,
            ContextAction.QUERY,
            ledger=ledger,
        )
    return ledger


def _route(
    provider: ProviderDescriptor,
    *,
    consent: AuthorizationLedger | None = None,
    request: BrokerRequest | None = None,
):
    return route_provider(
        _discovery(provider),
        request or _request(),
        consent or AuthorizationLedger(),
        utc_now=_UTC_NOW,
    )


def _reasons(decision: RoutingDecision, identity: str) -> tuple[str, ...]:
    return next(
        item.reason_codes
        for item in decision.rejected_alternatives
        if item.provider_identity == identity
    )


if __name__ == "__main__":
    unittest.main()
