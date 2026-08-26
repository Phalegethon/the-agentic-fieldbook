from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SKILL = ROOT / "skills" / "branch-handoff"
WORK_RECOVERY = ROOT / "skills" / "work-recovery"
VERSION = "1.2.0"
MARKETPLACE_VERSION = "1.3.0"


class ReleaseMetadataTest(unittest.TestCase):
    def test_skill_and_plugin_versions_match_1_2_0(self) -> None:
        skill_md = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        match = re.search(r'^  version: "([^"]+)"$', skill_md, re.MULTILINE)
        self.assertIsNotNone(match)
        plugin = json.loads(
            (SKILL / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        assert match is not None
        self.assertEqual(VERSION, match.group(1))
        self.assertEqual(VERSION, plugin["version"])

    def test_marketplace_1_3_0_exposes_both_independent_plugins_in_order(self) -> None:
        marketplace = json.loads(
            (ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
        )
        self.assertEqual(MARKETPLACE_VERSION, marketplace["version"])
        self.assertEqual(
            ["branch-handoff", "work-recovery"],
            [plugin["name"] for plugin in marketplace["plugins"]],
        )
        self.assertEqual(
            ["./skills/branch-handoff", "./skills/work-recovery"],
            [plugin["source"] for plugin in marketplace["plugins"]],
        )
        self.assertTrue(all("version" not in plugin for plugin in marketplace["plugins"]))
        self.assertEqual(len({plugin["name"] for plugin in marketplace["plugins"]}), 2)

    def test_work_recovery_skill_and_plugin_versions_match_1_0_0(self) -> None:
        skill_md = (WORK_RECOVERY / "SKILL.md").read_text(encoding="utf-8")
        match = re.search(r'^  version: "([^"]+)"$', skill_md, re.MULTILINE)
        self.assertIsNotNone(match)
        plugin = json.loads(
            (WORK_RECOVERY / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        assert match is not None
        self.assertEqual("1.0.0", match.group(1))
        self.assertEqual("1.0.0", plugin["version"])
        self.assertEqual("https://github.com/Phalegethon", plugin["author"]["url"])
        self.assertEqual("MIT", plugin["license"])

    def test_changelog_and_readme_publish_update_paths(self) -> None:
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("## [1.2.0] - 2026-08-24", changelog)
        self.assertIn("optional Jira and GitHub context", readme)
        self.assertIn(
            "Re-run the matching agent-specific install command",
            readme,
        )
        update_section = readme.split("## Update `branch-handoff`", 1)[1]
        self.assertIn("--agent claude-code", update_section)
        self.assertIn("--global", update_section)
        self.assertNotIn("skills@latest update branch-handoff", readme)
        self.assertIn("auto-update", readme.lower())
        self.assertIn("GitHub Release", readme)

    def test_readme_publishes_work_recovery_install_update_use_and_boundaries(self) -> None:
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("## [1.3.0] - 2026-08-26", changelog)
        self.assertIn("## Install `work-recovery`", readme)
        self.assertIn("## Update `work-recovery`", readme)
        self.assertIn("## Use work-recovery", readme)
        work_section = readme.split("## Install `work-recovery`", 1)[1]
        for agent in ("claude-code", "codex", "antigravity", "antigravity-cli"):
            self.assertIn(f"--agent {agent}", work_section)
        self.assertIn("--skill work-recovery", work_section)
        self.assertIn("Git and Python 3", work_section)
        self.assertIn("single best next step", work_section)
        self.assertIn("compact continuation prompt", work_section)
        self.assertIn("does not build or update an index", work_section.lower())
        self.assertIn("does not run tests", work_section.lower())


if __name__ == "__main__":
    unittest.main()
