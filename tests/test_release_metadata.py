from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SKILL = ROOT / "skills" / "branch-handoff"
WORK_RECOVERY = ROOT / "skills" / "work-recovery"
TAF_VERSION = "2.1.0"
SKILL_VERSIONS = {
    "branch-handoff": "1.2.1",
    "prepare-repo-context": "1.0.0",
    "work-recovery": "1.0.1",
}


class ReleaseMetadataTest(unittest.TestCase):
    def test_native_release_workflow_preserves_cross_platform_security_contract(self) -> None:
        attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        workflow = (
            ROOT / ".github" / "workflows" / "release-native-context.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("* text=auto eol=lf", attributes)
        self.assertIn("TMPDIR: ${{ runner.temp }}", workflow)
        self.assertIn("Prepare private Windows temp", workflow)
        self.assertIn("/setowner", workflow)
        self.assertIn("/inheritance:r", workflow)
        self.assertIn("release_tag:", workflow)
        self.assertIn("tag_name:", workflow)

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

    def test_codex_marketplace_exposes_the_same_root_taf_plugin(self) -> None:
        marketplace = json.loads(
            (ROOT / ".agents" / "plugins" / "marketplace.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual("the-agentic-fieldbook", marketplace["name"])
        self.assertEqual("The Agentic Fieldbook", marketplace["interface"]["displayName"])
        self.assertEqual(
            [
                {
                    "name": "taf",
                    "source": {"source": "local", "path": "./"},
                    "policy": {
                        "installation": "AVAILABLE",
                        "authentication": "ON_INSTALL",
                    },
                    "category": "Developer Tools",
                }
            ],
            marketplace["plugins"],
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
        self.assertIn("/taf:prepare-repo-context", entry["description"])
        self.assertLessEqual(len(entry["description"]), 200)

    def test_readme_publishes_one_taf_install_migration_and_update_path(self) -> None:
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("## [2.0.0] - 2026-08-27", changelog)
        self.assertIn("## [2.1.0] - 2026-08-30", changelog)
        for required in (
            "Install TAF once",
            "## Install TAF",
            "/plugin install taf@the-agentic-fieldbook",
            "codex plugin add taf@the-agentic-fieldbook",
            "/taf:branch-handoff",
            "/taf:work-recovery",
            "/taf:prepare-repo-context",
            "## Migrate to TAF 2.0",
            "## Update TAF",
            "/plugin update taf@the-agentic-fieldbook",
        ):
            self.assertIn(required, readme)
        for legacy in (
            "/plugin install branch-handoff@the-agentic-fieldbook",
            "/plugin install work-recovery@the-agentic-fieldbook",
            "/branch-handoff:branch-handoff",
            "/work-recovery:work-recovery",
        ):
            self.assertNotIn(legacy, readme)

        migration = readme.split("## Migrate to TAF 2.0", 1)[1].split(
            "## Update TAF", 1
        )[0]
        uninstall_branch = migration.index(
            "/plugin uninstall branch-handoff@the-agentic-fieldbook"
        )
        uninstall_recovery = migration.index(
            "/plugin uninstall work-recovery@the-agentic-fieldbook"
        )
        marketplace_update = migration.index(
            "/plugin marketplace update the-agentic-fieldbook"
        )
        install_taf = migration.index("/plugin install taf@the-agentic-fieldbook")
        self.assertLess(uninstall_branch, uninstall_recovery)
        self.assertLess(uninstall_recovery, marketplace_update)
        self.assertLess(marketplace_update, install_taf)

    def test_readme_preserves_skill_use_and_runtime_boundaries(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        normalized = " ".join(readme.split()).lower()
        self.assertNotIn("## Install `work-recovery`", readme)
        self.assertNotIn("## Update `work-recovery`", readme)
        self.assertNotIn("## Install `branch-handoff`", readme)
        self.assertNotIn("## Update `branch-handoff`", readme)
        self.assertIn("## Use branch-handoff", readme)
        self.assertIn("## Use work-recovery", readme)
        self.assertIn("## Use prepare-repo-context", readme)
        for phrase in (
            "optional Jira and GitHub context",
            "Git and Python 3",
            "single best next step",
            "compact continuation prompt",
            "does not build or update an index",
            "does not run tests",
            "does not load the full repository into model context",
        ):
            self.assertIn(phrase.lower(), normalized)


if __name__ == "__main__":
    unittest.main()
