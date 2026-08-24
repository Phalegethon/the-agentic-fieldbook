from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SKILL = ROOT / "skills" / "branch-handoff"
VERSION = "1.1.0"


class ReleaseMetadataTest(unittest.TestCase):
    def test_skill_and_plugin_versions_match_1_1_0(self) -> None:
        skill_md = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        match = re.search(r'^  version: "([^"]+)"$', skill_md, re.MULTILINE)
        self.assertIsNotNone(match)
        plugin = json.loads(
            (SKILL / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        assert match is not None
        self.assertEqual(VERSION, match.group(1))
        self.assertEqual(VERSION, plugin["version"])

    def test_marketplace_does_not_duplicate_the_plugin_version(self) -> None:
        marketplace = json.loads(
            (ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
        )
        branch_handoff = next(
            plugin
            for plugin in marketplace["plugins"]
            if plugin["name"] == "branch-handoff"
        )
        self.assertEqual(VERSION, marketplace["version"])
        self.assertNotIn("version", branch_handoff)

    def test_changelog_and_readme_publish_update_paths(self) -> None:
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("## [1.1.0] - 2026-08-24", changelog)
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


if __name__ == "__main__":
    unittest.main()
