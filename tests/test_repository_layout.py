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
        expected_package_files = {
            "__init__.py",
            "__main__.py",
            "cli.py",
            "consent.py",
            "discovery.py",
            "dossier.py",
            "freshness.py",
            "git_snapshot.py",
            "models.py",
            "provider_cli.py",
            "provider_models.py",
            "provider_state.py",
            "routing.py",
        }
        actual_package_files = {
            path.name for path in package.iterdir() if path.is_file()
        }
        self.assertEqual(expected_package_files, actual_package_files)

        self.assertFalse((ROOT / "tools" / "taf-context" / "SKILL.md").exists())

        context_tool = ROOT / "tools" / "taf-context"
        forbidden_engine_segments = {
            "adapter",
            "adapters",
            "indexes",
            "integration",
            "integrations",
            "level1",
            "level_1",
            "parser",
            "parsers",
            "provider_adapters",
            "providers",
            "storage",
            "stores",
            "watcher",
            "watchers",
        }
        for path in context_tool.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            relative = path.relative_to(context_tool)
            normalized_parts = {
                part.lower().replace("-", "_").removesuffix(".py")
                for part in relative.parts
            }
            self.assertFalse(
                normalized_parts & forbidden_engine_segments,
                relative.as_posix(),
            )
            self.assertFalse(
                relative.stem.lower().endswith(
                    ("_adapter", "_parser", "_storage", "_watcher")
                ),
                relative.as_posix(),
            )
            normalized_path = relative.as_posix().lower().replace("-", "_")
            self.assertNotIn("level1", normalized_path, relative.as_posix())
            self.assertNotIn("level_1", normalized_path, relative.as_posix())

        marketplace = json.loads(
            (ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
        )
        marketplace_sources = [plugin["source"] for plugin in marketplace["plugins"]]
        self.assertNotIn("taf-context", json.dumps(marketplace_sources))

        private_prefixes = (
            ".superpowers",
            "benchmarks/context-discovery-routing",
            "benchmarks/context-infrastructure",
            "evals/context-discovery-routing",
            "evals/context-infrastructure",
            "docs/superpowers",
            "tests/taf_context/evidence",
            "tests/taf_context/private",
        )
        public_paths = tuple(
            path.relative_to(ROOT).as_posix()
            for path in ROOT.rglob("*")
            if ".git" not in path.relative_to(ROOT).parts
        )
        self.assertFalse(
            any(path.startswith(private_prefixes) for path in public_paths)
        )
        self.assertFalse(
            any(
                path.startswith("docs/")
                and "context-discovery-routing" in path
                and any(token in path for token in ("design", "execution", "plan", "spec"))
                for path in public_paths
            )
        )

        private_result_names = {
            "benchmark-evidence.json",
            "benchmark-results.json",
            "benchmark-results.md",
            "measurements.json",
        }
        self.assertFalse(
            any(
                path.startswith("tests/taf_context/")
                and Path(path).name in private_result_names
                for path in public_paths
            )
        )

        local_state_names = {"audit.jsonl", "consent.json", "providers.json"}
        self.assertFalse(
            any(
                Path(path).name in local_state_names
                and not {"conformance", "fixtures"} & set(Path(path).parts)
                for path in public_paths
            )
        )


if __name__ == "__main__":
    unittest.main()
