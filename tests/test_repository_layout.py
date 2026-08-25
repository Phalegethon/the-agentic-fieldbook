from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SKILL = ROOT / "skills" / "branch-handoff"


class RepositoryLayoutTest(unittest.TestCase):
    def test_branch_handoff_is_a_standalone_agent_skill(self) -> None:
        skill_md = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(skill_md.startswith("---\nname: branch-handoff\n"))
        self.assertTrue((SKILL / "scripts" / "collect_diff.py").is_file())
        self.assertTrue((SKILL / "references" / "handoff-contract.md").is_file())

    def test_claude_marketplace_exposes_only_implemented_skills(self) -> None:
        marketplace = json.loads(
            (ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
        )
        self.assertEqual("the-agentic-fieldbook", marketplace["name"])
        self.assertEqual("Gürkan Süerdem", marketplace["owner"]["name"])
        self.assertEqual("https://github.com/Phalegethon", marketplace["owner"]["url"])
        self.assertEqual(
            [
                {
                    "name": "branch-handoff",
                    "source": "./skills/branch-handoff",
                }
            ],
            [
                {"name": plugin["name"], "source": plugin["source"]}
                for plugin in marketplace["plugins"]
            ],
        )

    def test_branch_handoff_has_consistent_claude_plugin_identity(self) -> None:
        plugin = json.loads(
            (SKILL / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual("branch-handoff", plugin["name"])
        self.assertEqual("1.2.0", plugin["version"])
        self.assertEqual("MIT", plugin["license"])
        self.assertEqual("Gürkan Süerdem", plugin["author"]["name"])
        self.assertEqual("https://github.com/Phalegethon", plugin["author"]["url"])
        self.assertEqual(
            "https://github.com/Phalegethon/the-agentic-fieldbook",
            plugin["repository"],
        )

    def test_publication_files_identify_taf(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("# The Agentic Fieldbook", readme)
        self.assertIn("skills/branch-handoff", readme)
        self.assertIn("https://github.com/Phalegethon", readme)
        self.assertNotIn("<github-owner>", readme)
        self.assertTrue((ROOT / "LICENSE").is_file())
        self.assertTrue((ROOT / "CONTRIBUTING.md").is_file())

    def test_public_repository_supports_external_contributions(self) -> None:
        codeowners = (ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
        contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        self.assertEqual("* @Phalegethon", codeowners.strip())
        self.assertIn("fork", contributing.lower())
        self.assertIn("pull request", contributing.lower())
        self.assertTrue((ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md").is_file())
        self.assertTrue((ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml").is_file())
        self.assertTrue((ROOT / ".github" / "ISSUE_TEMPLATE" / "skill_proposal.yml").is_file())

    def test_owner_only_development_artifacts_are_not_in_public_tree(self) -> None:
        self.assertFalse((ROOT / ".superpowers").exists())
        self.assertFalse((ROOT / "docs" / "superpowers").exists())
        self.assertFalse((ROOT / "tests" / "preparing_branch_handoff" / "evidence").exists())
        self.assertFalse((ROOT / "AGENTS.md").exists())
        self.assertFalse((ROOT / "CLAUDE.md").exists())

    def test_public_layout_contains_taf_context_engine_without_exposure(self) -> None:
        package = ROOT / "tools" / "taf-context" / "taf_context"
        package_files = {
            "__init__.py",
            "__main__.py",
            "cli.py",
            "consent.py",
            "dossier.py",
            "freshness.py",
            "git_snapshot.py",
            "models.py",
        }
        for filename in package_files:
            self.assertTrue((package / filename).is_file(), filename)

        self.assertFalse((ROOT / "tools" / "taf-context" / "SKILL.md").exists())

        marketplace = json.loads(
            (ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
        )
        marketplace_sources = [plugin["source"] for plugin in marketplace["plugins"]]
        self.assertNotIn("taf-context", json.dumps(marketplace_sources))

        private_prefixes = (
            "evals/context-infrastructure",
            "benchmarks/context-infrastructure",
            "docs/superpowers",
        )
        public_paths = (
            path.relative_to(ROOT).as_posix()
            for path in ROOT.rglob("*")
            if ".git" not in path.relative_to(ROOT).parts
        )
        self.assertFalse(
            any(path.startswith(private_prefixes) for path in public_paths)
        )


if __name__ == "__main__":
    unittest.main()
