"""Denial-first tests for exact-action context consent."""

from __future__ import annotations

import copy
import unittest

from taf_context.models import ContextAction


class AuthorizationLedgerTests(unittest.TestCase):
    def test_empty_ledger_denies_every_action(self) -> None:
        from taf_context.consent import AuthorizationLedger

        ledger = AuthorizationLedger()

        for action in ContextAction:
            with self.subTest(action=action):
                self.assertFalse(ledger.is_authorized(action, "repo", "provider"))

    def test_grant_authorizes_only_exact_action_and_scope(self) -> None:
        from taf_context.consent import AuthorizationLedger

        ledger = AuthorizationLedger().authorize(
            ContextAction.BUILD, "repo-a", "provider-a", "2026-08-25T00:00:00Z"
        )

        self.assertTrue(ledger.is_authorized(ContextAction.BUILD, "repo-a", "provider-a"))
        self.assertFalse(ledger.is_authorized(ContextAction.BUILD, "repo-b", "provider-a"))
        self.assertFalse(ledger.is_authorized(ContextAction.BUILD, "repo-a", "provider-b"))

    def test_authorization_for_one_action_never_implies_any_other_action(self) -> None:
        from taf_context.consent import AuthorizationLedger

        for granted, requested in ((a, b) for a in ContextAction for b in ContextAction if a != b):
            with self.subTest(granted=granted, requested=requested):
                ledger = AuthorizationLedger().authorize(
                    granted, "repo", "provider", "2026-08-25T00:00:00Z"
                )
                self.assertFalse(ledger.is_authorized(requested, "repo", "provider"))

    def test_duplicate_grant_is_idempotent(self) -> None:
        from taf_context.consent import AuthorizationLedger

        grant = (ContextAction.BUILD, "repo", "provider", "2026-08-25T00:00:00Z")
        ledger = AuthorizationLedger().authorize(*grant).authorize(*grant)

        self.assertEqual(ledger.grants, (grant,))

    def test_round_trip_preserves_exact_sorted_grants(self) -> None:
        from taf_context.consent import AuthorizationLedger

        ledger = AuthorizationLedger()
        ledger = ledger.authorize(ContextAction.UPDATE, "repo-b", "provider", "2026-08-25T00:00:00Z")
        ledger = ledger.authorize(ContextAction.BUILD, "repo-z", "provider", "2026-08-25T00:00:00Z")
        ledger = ledger.authorize(ContextAction.BUILD, "repo-a", "provider", "2026-08-25T00:00:00Z")
        expected = {
            "grants": [
                {
                    "action": "build",
                    "repository_identity": "repo-a",
                    "provider_name": "provider",
                    "granted_at": "2026-08-25T00:00:00Z",
                },
                {
                    "action": "build",
                    "repository_identity": "repo-z",
                    "provider_name": "provider",
                    "granted_at": "2026-08-25T00:00:00Z",
                },
                {
                    "action": "update",
                    "repository_identity": "repo-b",
                    "provider_name": "provider",
                    "granted_at": "2026-08-25T00:00:00Z",
                },
            ]
        }

        self.assertEqual(ledger.to_dict(), expected)
        self.assertEqual(AuthorizationLedger.from_dict(copy.deepcopy(expected)), ledger)

    def test_ledger_and_grants_are_immutable(self) -> None:
        from taf_context.consent import AuthorizationLedger

        ledger = AuthorizationLedger().authorize(
            ContextAction.BUILD, "repo", "provider", "2026-08-25T00:00:00Z"
        )

        with self.assertRaises((AttributeError, TypeError)):
            ledger.grants += (ledger.grants[0],)
        with self.assertRaises(TypeError):
            ledger.grants[0][0] = ContextAction.UPDATE  # type: ignore[index]

    def test_rejects_empty_scope_values_and_unknown_actions(self) -> None:
        from taf_context.consent import AuthorizationLedger, ConsentError

        for repository, provider in (("", "provider"), ("repo", "")):
            with self.subTest(repository=repository, provider=provider):
                with self.assertRaises(ConsentError):
                    AuthorizationLedger().authorize(
                        ContextAction.BUILD, repository, provider, "2026-08-25T00:00:00Z"
                    )

        with self.assertRaises(ConsentError):
            AuthorizationLedger().authorize(
                "unknown", "repo", "provider", "2026-08-25T00:00:00Z"  # type: ignore[arg-type]
            )

    def test_from_dict_rejects_malformed_or_unknown_fields(self) -> None:
        from taf_context.consent import AuthorizationLedger, ConsentError

        valid = {
            "grants": [
                {
                    "action": "build",
                    "repository_identity": "repo",
                    "provider_name": "provider",
                    "granted_at": "2026-08-25T00:00:00Z",
                }
            ]
        }

        for invalid in (
            {},
            {"grants": "not-a-list"},
            {"grants": [{"action": "build"}]},
            {**valid, "unexpected": True},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ConsentError):
                    AuthorizationLedger.from_dict(invalid)

        invalid_grant = copy.deepcopy(valid)
        invalid_grant["grants"][0]["action"] = "unknown"
        with self.assertRaises(ConsentError):
            AuthorizationLedger.from_dict(invalid_grant)


if __name__ == "__main__":
    unittest.main()
