"""Contract tests for exact allow and deny context-consent decisions."""

from __future__ import annotations

import copy
from dataclasses import replace
import unittest

from taf_context.models import ContextAction
from taf_context.provider_models import ConsentRequest, ProviderLocality


_DECIDED_AT = "2026-08-26T00:00:00Z"
_REQUEST_DIGEST = "sha256:" + "a" * 64


def _record(
    action: str = "query",
    repository_identity: str = "sha256:repo",
    provider_identity: str = "local.graph",
    provider_schema_version: str = "1",
    disposition: str = "allow",
    decided_at: str = _DECIDED_AT,
    request_digest: str = _REQUEST_DIGEST,
) -> dict[str, str]:
    """Return a literal v2 record fixture independent of ledger helpers."""
    return {
        "action": action,
        "repository_identity": repository_identity,
        "provider_identity": provider_identity,
        "provider_schema_version": provider_schema_version,
        "disposition": disposition,
        "decided_at": decided_at,
        "request_digest": request_digest,
    }


def _request(*actions: ContextAction) -> ConsentRequest:
    return ConsentRequest.create(
        schema_version="1",
        repository_identity="sha256:repo",
        provider_identity="local.graph",
        provider_schema_version="1",
        actions=actions,
        locality=ProviderLocality.LOCAL,
        data_surface="repository-metadata",
        fallback="native",
        requested_at=_DECIDED_AT,
    )


class AuthorizationLedgerTests(unittest.TestCase):
    def test_empty_ledger_has_no_decision_for_every_action(self) -> None:
        from taf_context.consent import AuthorizationLedger

        ledger = AuthorizationLedger()

        for action in ContextAction:
            with self.subTest(action=action):
                self.assertIsNone(
                    ledger.decision_for(action, "sha256:repo", "local.graph", "1")
                )
                self.assertFalse(
                    ledger.is_authorized(action, "sha256:repo", "local.graph", "1")
                )
                self.assertFalse(
                    ledger.is_denied(action, "sha256:repo", "local.graph", "1")
                )

    def test_all_fifty_six_ordered_cross_action_pairs_do_not_imply_decisions(self) -> None:
        from taf_context.consent import AuthorizationLedger

        for decided, requested in (
            (left, right)
            for left in ContextAction
            for right in ContextAction
            if left is not right
        ):
            with self.subTest(decided=decided, requested=requested):
                ledger = AuthorizationLedger.from_dict(
                    {"schema_version": "2", "records": [_record(action=decided.value)]}
                )
                self.assertIsNone(
                    ledger.decision_for(requested, "sha256:repo", "local.graph", "1")
                )
                self.assertFalse(
                    ledger.is_authorized(requested, "sha256:repo", "local.graph", "1")
                )
                self.assertFalse(
                    ledger.is_denied(requested, "sha256:repo", "local.graph", "1")
                )

    def test_allow_and_deny_apply_only_to_the_exact_four_part_scope(self) -> None:
        from taf_context.consent import AuthorizationLedger

        ledger = AuthorizationLedger.from_dict(
            {"schema_version": "2", "records": [_record(), _record(disposition="deny", action="inspect")]}
        )

        self.assertTrue(ledger.is_authorized(ContextAction.QUERY, "sha256:repo", "local.graph", "1"))
        self.assertFalse(ledger.is_denied(ContextAction.QUERY, "sha256:repo", "local.graph", "1"))
        self.assertTrue(ledger.is_denied(ContextAction.INSPECT, "sha256:repo", "local.graph", "1"))
        self.assertFalse(ledger.is_authorized(ContextAction.INSPECT, "sha256:repo", "local.graph", "1"))
        for repository_identity, provider_identity, provider_schema_version in (
            ("sha256:other", "local.graph", "1"),
            ("sha256:repo", "other.graph", "1"),
            ("sha256:repo", "local.graph", "2"),
        ):
            with self.subTest(
                repository_identity=repository_identity,
                provider_identity=provider_identity,
                provider_schema_version=provider_schema_version,
            ):
                self.assertIsNone(
                    ledger.decision_for(
                        ContextAction.QUERY,
                        repository_identity,
                        provider_identity,
                        provider_schema_version,
                    )
                )

    def test_record_adds_one_allow_record_for_every_requested_action(self) -> None:
        from taf_context.consent import AuthorizationLedger, ConsentDisposition

        request = _request(ContextAction.INSPECT, ContextAction.QUERY)
        ledger = AuthorizationLedger().record(request, ConsentDisposition.ALLOW, _DECIDED_AT)

        self.assertEqual(
            ledger.to_dict(),
            {
                "schema_version": "2",
                "records": [
                    _record(action="inspect", request_digest="sha256:" + request.digest),
                    _record(action="query", request_digest="sha256:" + request.digest),
                ],
            },
        )

    def test_record_rejects_a_request_whose_digest_no_longer_matches_its_fields(self) -> None:
        from taf_context.consent import AuthorizationLedger, ConsentDisposition, ConsentError

        request = replace(_request(ContextAction.QUERY), digest="b" * 64)

        with self.assertRaises(ConsentError):
            AuthorizationLedger().record(request, ConsentDisposition.ALLOW, _DECIDED_AT)

    def test_identical_records_are_idempotent(self) -> None:
        from taf_context.consent import AuthorizationLedger

        ledger = AuthorizationLedger.from_dict(
            {"schema_version": "2", "records": [_record(), _record()]}
        )

        self.assertEqual(ledger.to_dict(), {"schema_version": "2", "records": [_record()]})

    def test_same_disposition_records_at_one_scope_and_timestamp_keep_distinct_digests(self) -> None:
        from taf_context.consent import AuthorizationLedger

        first = _record(request_digest="sha256:" + "a" * 64)
        second = _record(request_digest="sha256:" + "b" * 64)

        ledger = AuthorizationLedger.from_dict(
            {"schema_version": "2", "records": [second, first]}
        )

        self.assertEqual(
            ledger.to_dict(),
            {"schema_version": "2", "records": [first, second]},
        )

    def test_later_rfc3339_decision_wins_only_for_its_exact_scope(self) -> None:
        from taf_context.consent import AuthorizationLedger, ConsentDisposition

        ledger = AuthorizationLedger.from_dict(
            {
                "schema_version": "2",
                "records": [
                    _record(disposition="allow", decided_at="2026-08-26T00:00:00Z"),
                    _record(disposition="deny", decided_at="2026-08-26T00:00:01Z"),
                    _record(
                        disposition="allow",
                        provider_schema_version="2",
                        decided_at="2026-08-26T00:00:02Z",
                    ),
                ],
            }
        )

        self.assertEqual(
            ledger.decision_for(ContextAction.QUERY, "sha256:repo", "local.graph", "1"),
            ConsentDisposition.DENY,
        )
        self.assertEqual(
            ledger.decision_for(ContextAction.QUERY, "sha256:repo", "local.graph", "2"),
            ConsentDisposition.ALLOW,
        )

    def test_conflicting_records_at_one_exact_scope_and_timestamp_are_rejected(self) -> None:
        from taf_context.consent import AuthorizationLedger, ConsentError

        with self.assertRaises(ConsentError):
            AuthorizationLedger.from_dict(
                {
                    "schema_version": "2",
                    "records": [_record(disposition="allow"), _record(disposition="deny")],
                }
            )

    def test_decided_at_requires_a_strict_rfc3339_timestamp(self) -> None:
        from taf_context.consent import AuthorizationLedger, ConsentError

        for decided_at in (
            "2026-08-26",
            "2026-08-26T00:00:00",
            "2026-08-26T00:00:00+00",
            "2026-02-30T00:00:00Z",
            "2026-08-26T00:00:00z",
        ):
            with self.subTest(decided_at=decided_at):
                with self.assertRaises(ConsentError):
                    AuthorizationLedger.from_dict(
                        {"schema_version": "2", "records": [_record(decided_at=decided_at)]}
                    )

    def test_revoke_removes_every_and_only_the_exact_scope(self) -> None:
        from taf_context.consent import AuthorizationLedger

        ledger = AuthorizationLedger.from_dict(
            {
                "schema_version": "2",
                "records": [
                    _record(disposition="allow"),
                    _record(disposition="deny", decided_at="2026-08-26T00:00:01Z"),
                    _record(action="inspect"),
                    _record(provider_schema_version="2"),
                    _record(provider_identity="other.graph"),
                    _record(repository_identity="sha256:other"),
                ],
            }
        )

        revoked = ledger.revoke(ContextAction.QUERY, "sha256:repo", "local.graph", "1")

        self.assertEqual(
            revoked.to_dict(),
            {
                "schema_version": "2",
                "records": [
                    _record(action="inspect"),
                    _record(repository_identity="sha256:other"),
                    _record(provider_schema_version="2"),
                    _record(provider_identity="other.graph"),
                ],
            },
        )

    def test_v2_round_trip_is_canonical_and_immutable(self) -> None:
        from taf_context.consent import AuthorizationLedger

        wire = {
            "schema_version": "2",
            "records": [
                _record(action="query", provider_identity="z.graph"),
                _record(action="build", repository_identity="sha256:z"),
                _record(action="build", repository_identity="sha256:a"),
            ],
        }
        expected = {
            "schema_version": "2",
            "records": [
                _record(action="build", repository_identity="sha256:a"),
                _record(action="build", repository_identity="sha256:z"),
                _record(action="query", provider_identity="z.graph"),
            ],
        }

        ledger = AuthorizationLedger.from_dict(copy.deepcopy(wire))

        self.assertEqual(ledger.to_dict(), expected)
        self.assertEqual(AuthorizationLedger.from_dict(copy.deepcopy(expected)), ledger)
        with self.assertRaises((AttributeError, TypeError)):
            ledger.records += (ledger.records[0],)
        with self.assertRaises((AttributeError, TypeError)):
            ledger.records[0].disposition = "deny"  # type: ignore[misc]

    def test_from_dict_requires_the_exact_v2_wire_shape_and_rejects_v1_grants(self) -> None:
        from taf_context.consent import AuthorizationLedger, ConsentError

        for invalid in (
            {},
            {"schema_version": "1", "records": []},
            {"schema_version": "2", "records": "not-a-list"},
            {"schema_version": "2", "records": [{"action": "query"}]},
            {"schema_version": "2", "records": [_record()], "unexpected": True},
            {"grants": []},
            {
                "grants": [
                    {
                        "action": "query",
                        "repository_identity": "sha256:repo",
                        "provider_name": "local.graph",
                        "granted_at": _DECIDED_AT,
                    }
                ]
            },
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ConsentError):
                    AuthorizationLedger.from_dict(invalid)

    def test_from_dict_rejects_invalid_record_members(self) -> None:
        from taf_context.consent import AuthorizationLedger, ConsentError

        for field, value in (
            ("action", "unknown"),
            ("repository_identity", ""),
            ("provider_identity", ""),
            ("provider_schema_version", ""),
            ("disposition", "grant"),
            ("request_digest", "a" * 64),
            ("request_digest", "sha256:" + "A" * 64),
        ):
            with self.subTest(field=field, value=value):
                invalid = _record()
                invalid[field] = value
                with self.assertRaises(ConsentError):
                    AuthorizationLedger.from_dict({"schema_version": "2", "records": [invalid]})


if __name__ == "__main__":
    unittest.main()
