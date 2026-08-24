from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SKILL_DIR = ROOT / "skills" / "branch-handoff"


class PlatformPreflightContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        cls.platform = (SKILL_DIR / "references" / "platform-actions.md").read_text(
            encoding="utf-8"
        )

    def test_explicit_platform_target_preflights_before_collector(self) -> None:
        target_marker = "An explicitly supplied Jira issue key/URL or GitHub PR number/URL"
        collector_marker = "Invoke `<python> <skill-dir>/scripts/collect_diff.py` exactly once"
        self.assertIn(target_marker, self.skill)
        self.assertIn(collector_marker, self.skill)
        self.assertLess(self.skill.index(target_marker), self.skill.index(collector_marker))
        self.assertIn("before any repository or diff work", self.skill)

    def test_platform_reference_forbids_collection_while_choice_is_pending(self) -> None:
        self.assertIn("The collector has not run", self.platform)
        self.assertIn("Do not start repository or diff work", self.platform)

    def test_no_platform_signal_preserves_the_local_fast_path(self) -> None:
        self.assertIn(
            "With no explicit platform target, perform no adapter lookup, question, or network request",
            self.skill,
        )

    def test_jira_permission_bundles_connection_and_bounded_read(self) -> None:
        self.assertIn(
            "One selection authorizes both connection-status verification and the bounded read",
            self.platform,
        )
        for field in (
            "key",
            "summary",
            "description/acceptance criteria",
            "type",
            "status",
            "priority",
            "components/labels",
            "links",
        ):
            self.assertIn(field, self.platform)
        self.assertIn("Comments and attachments remain excluded", self.platform)

    def test_structured_choice_has_a_numbered_fallback(self) -> None:
        self.assertNotIn("Ask one question at a time", self.platform)
        self.assertIn("structured question tool", self.platform)
        self.assertIn("numbered choices", self.platform)

    def test_platform_failure_continues_to_a_complete_local_report(self) -> None:
        self.assertIn(
            "Decline, missing adapter, authentication failure, or read failure",
            self.platform,
        )
        self.assertIn("complete diff-only report", self.platform)

    def test_writes_require_exact_target_and_exact_draft(self) -> None:
        self.assertIn("Post once", self.platform)
        self.assertIn("Edit draft", self.platform)
        self.assertIn("Keep local", self.platform)
        self.assertIn("exact target", self.platform)
        self.assertIn("exact sanitized draft", self.platform)


if __name__ == "__main__":
    unittest.main()
