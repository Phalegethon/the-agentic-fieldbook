"""Hybrid inspection, routing, query, and fallback orchestration tests."""

from __future__ import annotations

from dataclasses import replace
import tempfile
import unittest
from pathlib import Path

from taf_context.consent import AuthorizationLedger, ConsentDisposition
from taf_context.level1_models import Level1Request
from taf_context.models import ContextAction, Freshness
from taf_context.provider_broker import execute_broker_request
from taf_context.provider_execution_models import AttemptRecord, AttemptStatus, InspectionRecord, Readiness
from taf_context.provider_models import ConsentRequest, DiscoverySnapshot, StatusEvidence

from .test_level1_models import request_wire
from .test_provider_freshness import descriptor, inspection, snapshot


def consent(provider) -> AuthorizationLedger:
    request = ConsentRequest.create(
        schema_version="1", repository_identity=snapshot().repository_identity,
        provider_identity=provider.provider_identity,
        provider_schema_version=provider.provider_schema_version,
        actions=(ContextAction.INSPECT, ContextAction.QUERY),
        locality=provider.locality, data_surface="repository-metadata",
        fallback="bounded-level1", requested_at="2026-08-27T00:00:00Z",
    )
    return AuthorizationLedger().record(request, ConsentDisposition.ALLOW, "2026-08-27T00:00:00Z")


class ProviderBrokerTests(unittest.TestCase):
    def test_uninspected_authorized_provider_is_refined_then_queried_once(self) -> None:
        provider = descriptor()
        discovery = DiscoverySnapshot("1", snapshot().repository_identity, snapshot().worktree_identity, "sha256:inventory", (provider,), 0, (), 0, False, (), 1, 1)
        wire = request_wire()
        wire.update({"provider_identity": provider.provider_identity, "repository_identity": snapshot().repository_identity, "worktree_identity": snapshot().worktree_identity, "dirty_overlay_fingerprint": snapshot().dirty_fingerprint, "committed_head": snapshot().head_sha})
        request = Level1Request.from_dict(wire)
        calls = []
        def inspect_call(_provider):
            calls.append("inspect")
            return inspection(), AttemptRecord("1", provider.provider_identity, "inspect", AttemptStatus.SUCCEEDED, (), 1, 1, 0)
        def query_call(_provider, _request):
            calls.append("query")
            raise AssertionError("query-called")
        with self.assertRaises(AssertionError):
            execute_broker_request(discovery, request, consent(provider), snapshot(), {provider.provider_identity: object()}, inspect_call=inspect_call, query_call=query_call, fallback_call=None, utc_now="2026-08-27T00:00:00Z")
        self.assertEqual(calls, ["inspect", "query"])

    def test_inspection_failure_uses_only_permitted_fallback(self) -> None:
        provider = descriptor()
        discovery = DiscoverySnapshot("1", snapshot().repository_identity, snapshot().worktree_identity, "sha256:inventory", (provider,), 0, (), 0, False, (), 1, 1)
        wire = request_wire(); wire.update({"provider_identity": provider.provider_identity, "repository_identity": snapshot().repository_identity, "worktree_identity": snapshot().worktree_identity, "dirty_overlay_fingerprint": snapshot().dirty_fingerprint, "committed_head": snapshot().head_sha})
        request = Level1Request.from_dict(wire)
        sentinel = object()
        execution = execute_broker_request(discovery, request, consent(provider), snapshot(), {provider.provider_identity: object()}, inspect_call=lambda _p: (_ for _ in ()).throw(RuntimeError("provider-timeout")), query_call=lambda _p, _r: None, fallback_call=lambda _r: sentinel, utc_now="2026-08-27T00:00:00Z")
        self.assertIs(execution.result, sentinel)
        self.assertEqual(execution.route, "bounded-fallback")
        self.assertEqual(execution.reason_codes, ("provider-timeout",))


if __name__ == "__main__":
    unittest.main()
