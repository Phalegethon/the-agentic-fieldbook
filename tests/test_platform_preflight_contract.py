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


if __name__ == "__main__":
    unittest.main()
