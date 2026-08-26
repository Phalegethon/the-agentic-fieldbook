from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SKILL = ROOT / "skills" / "branch-handoff"
WORK_RECOVERY = ROOT / "skills" / "work-recovery"
TAF_VERSION = "2.0.0"
SKILL_VERSIONS = {
    "branch-handoff": "1.2.1",
    "work-recovery": "1.0.1",
}


class ReleaseMetadataTest(unittest.TestCase):
    def test_skill_versions_remain_independent_of_the_product_version(self) -> None:
        for skill_name, expected_version in SKILL_VERSIONS.items():
            with self.subTest(skill=skill_name):
                skill = ROOT / "skills" / skill_name
                skill_md = (skill / "SKILL.md").read_text(encoding="utf-8")
                match = re.search(r'^  version: "([^"]+)"$', skill_md, re.MULTILINE)
                self.assertIsNotNone(match)
                assert match is not None
                self.assertEqual(expected_version, match.group(1))
                self.assertFalse((skill / ".claude-plugin" / "plugin.json").exists())

    def test_root_plugin_manifests_publish_one_taf_product(self) -> None:
        claude = json.loads(
            (ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        codex = json.loads(
            (ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(("taf", TAF_VERSION), (claude["name"], claude["version"]))
        self.assertEqual(("taf", TAF_VERSION), (codex["name"], codex["version"]))
        self.assertEqual("The Agentic Fieldbook (TAF)", claude["displayName"])
        self.assertEqual("./skills/", codex["skills"])
        self.assertEqual(
            set(SKILL_VERSIONS),
            {path.parent.name for path in (ROOT / "skills").glob("*/SKILL.md")},
        )

    def test_marketplace_exposes_one_root_sourced_taf_plugin(self) -> None:
        marketplace = json.loads(
            (ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
        )
        self.assertEqual(TAF_VERSION, marketplace["version"])
        self.assertEqual(
            [{"name": "taf", "source": "./"}],
            [
                {"name": plugin["name"], "source": plugin["source"]}
                for plugin in marketplace["plugins"]
            ],
        )

    def test_claude_catalog_explains_outcome_and_namespaced_invocation(self) -> None:
        marketplace = json.loads(
            (ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
        )
        self.assertEqual(1, len(marketplace["plugins"]))
        entry = marketplace["plugins"][0]
        self.assertEqual("The Agentic Fieldbook (TAF)", entry["displayName"])
        self.assertIn("handoff", entry["description"].lower())
        self.assertIn("recover", entry["description"].lower())
        self.assertIn("/taf:branch-handoff", entry["description"])
        self.assertIn("/taf:work-recovery", entry["description"])
        self.assertLessEqual(len(entry["description"]), 200)

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
