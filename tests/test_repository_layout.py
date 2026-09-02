from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).parents[1]
SKILL = ROOT / "skills" / "branch-handoff"
WORK_RECOVERY_SKILL = ROOT / "skills" / "work-recovery"
PREPARE_REPO_CONTEXT_SKILL = ROOT / "skills" / "prepare-repo-context"
EXPECTED_TAF_CONTEXT_FILES = {
    "__init__.py",
    "__main__.py",
    "cli.py",
    "dossier.py",
    "freshness.py",
    "git_snapshot.py",
    "level1_models.py",
    "level1_render.py",
    "models.py",
    "prepare_cli.py",
    "recovery.py",
    "recovery_cli.py",
    "recovery_models.py",
    "state_lifecycle.py",
    "state_paths.py",
}
EXPECTED_LEVEL1_CONTRACT_FILES = {
    "contracts/level1/request.schema.json",
    "contracts/level1/result.schema.json",
}
NONPRODUCTION_CONTEXT_DIRECTORIES = {
    "conformance",
    "fixture",
    "fixtures",
    "test",
    "tests",
}
CONTEXT_ENGINE_TERMS = (
    "adapter",
    "adapters",
    "bridge",
    "broker",
    "discovery",
    "engine",
    "index",
    "integration",
    "level1",
    "parser",
    "provider",
    "providers",
    "registry",
    "router",
    "routing",
    "runtime",
    "service",
    "storage",
    "store",
    "watcher",
    "watchers",
)
CONTEXT_SCOPE_PREFIXES = ("tafcontext", "contextual", "context")
PRIVATE_BAKEOFF_ROOTS = {"evals", "experiments"}
PRIVATE_BAKEOFF_FILENAMES = {
    "candidate.json",
    "cargo.lock",
    "go.sum",
    "uv.lock",
}
PUBLIC_NATIVE_LEVEL1_ROOT = "tools/taf-context-native"


def _is_public_native_level1_path(relative: Path) -> bool:
    root = Path(PUBLIC_NATIVE_LEVEL1_ROOT)
    return relative == root or root in relative.parents


@contextmanager
def _isolated_context_layout() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        package = root / "tools" / "taf-context" / "taf_context"
        package.mkdir(parents=True)
        for filename in EXPECTED_TAF_CONTEXT_FILES:
            (package / filename).write_text("", encoding="utf-8")
        for relative in EXPECTED_LEVEL1_CONTRACT_FILES:
            contract = root / "tools" / "taf-context" / relative
            contract.parent.mkdir(parents=True, exist_ok=True)
            contract.write_text("{}\n", encoding="utf-8")
        marketplace = root / ".claude-plugin" / "marketplace.json"
        marketplace.parent.mkdir()
        marketplace.write_text('{"plugins":[]}', encoding="utf-8")
        with patch(f"{__name__}.ROOT", root):
            yield root


def _normalized_path_parts(relative: Path) -> tuple[str, ...]:
    parts = relative.parts
    return tuple(
        (Path(part).stem if index == len(parts) - 1 else part)
        .lower()
        .replace("-", "_")
        .replace(".", "_")
        for index, part in enumerate(parts)
    )


def _is_complete_context_compound(remainder: str) -> bool:
    reachable_offsets = {0}
    for offset in range(len(remainder) + 1):
        if offset not in reachable_offsets:
            continue
        for term in CONTEXT_ENGINE_TERMS:
            if remainder.startswith(term, offset):
                reachable_offsets.add(offset + len(term))
    return len(remainder) in reachable_offsets


def _is_context_scope_component(component: str) -> bool:
    collapsed = component.replace("_", "")
    if collapsed == "taf":
        return True
    for prefix in CONTEXT_SCOPE_PREFIXES:
        if collapsed.startswith(prefix):
            remainder = collapsed[len(prefix) :]
            return _is_complete_context_compound(remainder)
    return False


def _is_context_production_scope(relative: Path) -> bool:
    if not relative.parts:
        return False
    normalized_parts = _normalized_path_parts(relative)
    for directory in normalized_parts[:-1]:
        if set(directory.split("_")) & NONPRODUCTION_CONTEXT_DIRECTORIES:
            return False
    return any(
        _is_context_scope_component(component)
        for component in normalized_parts
    )


def _is_forbidden_context_production_surface(relative: Path) -> bool:
    if not _is_context_production_scope(relative):
        return False
    normalized_parts = _normalized_path_parts(relative)
    tokens = {
        token
        for part in normalized_parts
        for token in part.split("_")
        if token
    }
    normalized_path = "/".join(normalized_parts)
    has_provider_adapter = bool(
        {"provider", "providers"} & tokens and {"adapter", "adapters"} & tokens
    ) or any("provideradapter" in part for part in normalized_parts)
    has_watcher = bool({"watcher", "watchers"} & tokens) or any(
        part.replace("_", "").endswith(("watcher", "watchers"))
        for part in normalized_parts
    )
    has_level_one = "level1" in normalized_path or "level_1" in normalized_path
    has_parser_or_storage = bool(
        {"parser", "parsers", "storage", "store", "stores"} & tokens
    ) or any(
        part.replace("_", "").endswith(
            ("parser", "parsers", "storage", "store", "stores")
        )
        for part in normalized_parts
    )
    return bool(
        relative.name.lower() == "skill.md"
        or has_provider_adapter
        or has_watcher
        or has_level_one
        or has_parser_or_storage
    )


def _is_private_bakeoff_artifact(relative: Path) -> bool:
    return bool(
        relative.parts
        and (
            relative.parts[0].lower() in PRIVATE_BAKEOFF_ROOTS
            or relative.name.lower() in PRIVATE_BAKEOFF_FILENAMES
            or relative.suffix.lower() == ".jsonl"
        )
    )


class RepositoryLayoutTest(unittest.TestCase):
    def test_native_level1_exception_is_limited_to_its_exact_root(self) -> None:
        self.assertTrue(
            _is_public_native_level1_path(
                Path("tools/taf-context-native/internal/wire/types.go")
            )
        )
        self.assertFalse(
            _is_public_native_level1_path(
                Path("tools/not-taf-context-native/internal/wire/types.go")
            )
        )
        self.assertFalse(
            _is_public_native_level1_path(
                Path("tools/taf-context-native-copy/internal/wire/types.go")
            )
        )

    def test_native_level1_adapter_template_stays_inside_the_native_root(self) -> None:
        template = ROOT / PUBLIC_NATIVE_LEVEL1_ROOT / "adapter" / "manifest.template.json"
        self.assertTrue(template.is_file())
        self.assertTrue(_is_public_native_level1_path(template.relative_to(ROOT)))

    def test_private_bakeoff_artifacts_are_rejected_from_public_layout(self) -> None:
        forbidden = (
            Path("experiments/level1-bakeoff/python/candidate.json"),
            Path("evals/level1-bakeoff/evidence/run/evidence.jsonl"),
            Path("tools/taf-context/uv.lock"),
            Path("tools/taf-context/candidate.json"),
        )
        allowed = (
            Path("tests/taf_context/benchmark_level1.py"),
            Path("tests/taf_context/level1_replacement_controller.py"),
            Path("tests/taf_context/level1_replacement_scoring.py"),
            Path("tests/taf_context/test_level1_replacement_controller.py"),
            Path("tests/taf_context/test_level1_replacement_scoring.py"),
            Path("tools/taf-context/contracts/level1/request.schema.json"),
        )
        self.assertTrue(all(_is_private_bakeoff_artifact(path) for path in forbidden))
        self.assertFalse(any(_is_private_bakeoff_artifact(path) for path in allowed))

    def test_context_scope_predicate_distinguishes_compound_paths(self) -> None:
        context_paths = (
            "skills/contextbridge/SKILL.md",
            "src/tafcontext/parser.py",
            "src/contextual_runtime/storage.py",
            "tools/contextprovideradapter.py",
            "tools/contextualruntime/storage.py",
            "packages/contextlevel1parser.py",
            "src/contextdiscoveryrouting/watcher.py",
        )
        unrelated_paths = (
            "tools/logging/watcher.py",
            "packages/book-level1/parser.py",
            "tools/cloud/provider_adapter.py",
            "packages/tafcontext/tests/test_watcher.py",
            "tools/contextvars/watcher.py",
            "tests/taf_context/test_watcher.py",
            "samples/fixtures/context-watcher/level1/parser.json",
        )
        for relative in context_paths:
            with self.subTest(relative=relative):
                self.assertTrue(_is_context_production_scope(Path(relative)))
        for relative in unrelated_paths:
            with self.subTest(relative=relative):
                self.assertFalse(_is_context_production_scope(Path(relative)))

    def test_branch_handoff_is_a_standalone_agent_skill(self) -> None:
        skill_md = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(skill_md.startswith("---\nname: branch-handoff\n"))
        self.assertTrue((SKILL / "scripts" / "collect_diff.py").is_file())
        self.assertTrue((SKILL / "references" / "handoff-contract.md").is_file())

    def test_work_recovery_is_a_standalone_agent_skill(self) -> None:
        skill_md = (WORK_RECOVERY_SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(skill_md.startswith("---\nname: work-recovery\n"))
        self.assertTrue((WORK_RECOVERY_SKILL / "scripts" / "collect_recovery.py").is_file())
        self.assertTrue((WORK_RECOVERY_SKILL / "scripts" / "runtime-manifest.json").is_file())
        self.assertTrue((WORK_RECOVERY_SKILL / "references" / "recovery-contract.md").is_file())

    def test_prepare_repo_context_exposes_one_runnable_skill_entrypoint(self) -> None:
        self.assertTrue((PREPARE_REPO_CONTEXT_SKILL / "SKILL.md").is_file())
        self.assertTrue(
            (PREPARE_REPO_CONTEXT_SKILL / "scripts" / "prepare_repo_context.py").is_file()
        )
        self.assertTrue(
            (PREPARE_REPO_CONTEXT_SKILL / "agents" / "openai.yaml").is_file()
        )

    def test_claude_marketplace_exposes_only_the_taf_product(self) -> None:
        marketplace = json.loads(
            (ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
        )
        self.assertEqual("the-agentic-fieldbook", marketplace["name"])
        self.assertEqual("Gürkan Süerdem", marketplace["owner"]["name"])
        self.assertEqual("https://github.com/Phalegethon", marketplace["owner"]["url"])
        self.assertEqual(
            [
                {
                    "name": "taf",
                    "source": "./",
                }
            ],
            [
                {"name": plugin["name"], "source": plugin["source"]}
                for plugin in marketplace["plugins"]
            ],
        )

    def test_root_manifests_have_consistent_taf_identity(self) -> None:
        for relative in (
            Path(".claude-plugin/plugin.json"),
            Path(".codex-plugin/plugin.json"),
        ):
            with self.subTest(manifest=relative.as_posix()):
                plugin = json.loads((ROOT / relative).read_text(encoding="utf-8"))
                self.assertEqual("taf", plugin["name"])
                self.assertEqual("2.1.2", plugin["version"])
                self.assertEqual("MIT", plugin["license"])
                self.assertEqual("Gürkan Süerdem", plugin["author"]["name"])
                self.assertEqual(
                    "https://github.com/Phalegethon", plugin["author"]["url"]
                )
                self.assertEqual(
                    "https://github.com/Phalegethon/the-agentic-fieldbook",
                    plugin["repository"],
                )

        self.assertFalse((SKILL / ".claude-plugin" / "plugin.json").exists())
        self.assertFalse(
            (WORK_RECOVERY_SKILL / ".claude-plugin" / "plugin.json").exists()
        )

    def test_codex_marketplace_points_to_the_canonical_repository_root(self) -> None:
        marketplace = json.loads(
            (ROOT / ".agents" / "plugins" / "marketplace.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            [("taf", "local", "./")],
            [
                (
                    plugin["name"],
                    plugin["source"]["source"],
                    plugin["source"]["path"],
                )
                for plugin in marketplace["plugins"]
            ],
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
        actual_package_entries = {
            path.relative_to(package).as_posix()
            for path in package.rglob("*")
            if "__pycache__" not in path.relative_to(package).parts
        }
        self.assertEqual(EXPECTED_TAF_CONTEXT_FILES, actual_package_entries)
        actual_contract_files = {
            path.relative_to(ROOT / "tools" / "taf-context").as_posix()
            for path in (ROOT / "tools" / "taf-context" / "contracts").rglob("*")
            if path.is_file()
        }
        self.assertEqual(EXPECTED_LEVEL1_CONTRACT_FILES, actual_contract_files)
        self.assertFalse((ROOT / "tools" / "taf-context" / "adapters").exists())

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
            if relative.as_posix() in EXPECTED_LEVEL1_CONTRACT_FILES or relative.as_posix() in {
                "taf_context/level1_models.py",
                "taf_context/level1_render.py",
            }:
                continue
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
            sorted(
                path.relative_to(ROOT).as_posix()
                for path in ROOT.rglob("*")
                if not {".git", ".worktrees"}.intersection(
                    path.relative_to(ROOT).parts
                )
            )
        )
        public_files = tuple(
            sorted(
                path.relative_to(ROOT)
                for path in ROOT.rglob("*")
                if path.is_file()
                and not {".git", ".worktrees"}.intersection(
                    path.relative_to(ROOT).parts
                )
            )
        )
        self.assertFalse(
            any(
                _is_private_bakeoff_artifact(path)
                and not _is_public_native_level1_path(path)
                for path in public_files
            )
        )
        for relative in public_files:
            # The production native engine is intentionally public, but only at
            # this exact root. The general context-engine leakage policy still
            # applies everywhere else in the repository.
            if _is_public_native_level1_path(relative):
                continue
            if relative.as_posix() in {
                "tools/taf-context/contracts/level1/request.schema.json",
                "tools/taf-context/contracts/level1/result.schema.json",
                "tools/taf-context/taf_context/level1_models.py",
                "tools/taf-context/taf_context/level1_render.py",
            }:
                continue
            self.assertFalse(
                _is_forbidden_context_production_surface(relative),
                relative.as_posix(),
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

    def test_public_boundary_rejects_nested_production_subpackage(self) -> None:
        with _isolated_context_layout() as root:
            mutation = (
                root
                / "tools"
                / "taf-context"
                / "taf_context"
                / "neutral"
                / "helpers.py"
            )
            mutation.parent.mkdir()
            mutation.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(AssertionError, "neutral/helpers.py"):
                self.test_public_layout_contains_taf_context_engine_without_exposure()

    def test_public_boundary_rejects_alternate_context_production_surfaces(
        self,
    ) -> None:
        relative_mutations = (
            "skills/context-bridge/SKILL.md",
            "tools/context-bridge/provider_adapter.py",
            "src/context_runtime/watcher.py",
            "packages/context-level1/parser.py",
        )
        for relative in relative_mutations:
            with self.subTest(relative=relative), _isolated_context_layout() as root:
                mutation = root / relative
                mutation.parent.mkdir(parents=True)
                mutation.write_text("", encoding="utf-8")
                with self.assertRaisesRegex(AssertionError, relative):
                    self.test_public_layout_contains_taf_context_engine_without_exposure()

    def test_public_boundary_rejects_compound_context_production_surfaces(
        self,
    ) -> None:
        relative_mutations = (
            "skills/contextbridge/SKILL.md",
            "src/tafcontext/parser.py",
            "src/contextual_runtime/storage.py",
        )
        for relative in relative_mutations:
            with self.subTest(relative=relative), _isolated_context_layout() as root:
                mutation = root / relative
                mutation.parent.mkdir(parents=True)
                mutation.write_text("", encoding="utf-8")
                with self.assertRaisesRegex(AssertionError, relative):
                    self.test_public_layout_contains_taf_context_engine_without_exposure()

    def test_public_boundary_rejects_multi_term_context_compounds(self) -> None:
        relative_mutations = (
            "tools/contextprovideradapter.py",
            "tools/contextualruntime/storage.py",
            "packages/contextlevel1parser.py",
            "src/contextdiscoveryrouting/watcher.py",
        )
        for relative in relative_mutations:
            with self.subTest(relative=relative), _isolated_context_layout() as root:
                mutation = root / relative
                mutation.parent.mkdir(parents=True, exist_ok=True)
                mutation.write_text("", encoding="utf-8")
                with self.assertRaisesRegex(AssertionError, relative):
                    self.test_public_layout_contains_taf_context_engine_without_exposure()

    def test_public_boundary_allows_unrelated_production_components(self) -> None:
        relative_mutations = (
            "tools/logging/watcher.py",
            "packages/book-level1/parser.py",
            "tools/cloud/provider_adapter.py",
        )
        for relative in relative_mutations:
            with self.subTest(relative=relative), _isolated_context_layout() as root:
                mutation = root / relative
                mutation.parent.mkdir(parents=True)
                mutation.write_text("", encoding="utf-8")
                self.test_public_layout_contains_taf_context_engine_without_exposure()

    def test_public_boundary_allows_nested_tests_and_contextvars(self) -> None:
        relative_mutations = (
            "packages/tafcontext/tests/test_watcher.py",
            "tools/contextvars/watcher.py",
        )
        for relative in relative_mutations:
            with self.subTest(relative=relative), _isolated_context_layout() as root:
                mutation = root / relative
                mutation.parent.mkdir(parents=True)
                mutation.write_text("", encoding="utf-8")
                self.test_public_layout_contains_taf_context_engine_without_exposure()

    def test_public_boundary_allows_negative_test_vocabulary_and_fixtures(
        self,
    ) -> None:
        with _isolated_context_layout() as root:
            negative_test = root / "tests" / "taf_context" / "test_provider_adapter.py"
            negative_test.parent.mkdir(parents=True)
            negative_test.write_text("watcher level1 storage", encoding="utf-8")
            fixture = (
                root
                / "tests"
                / "taf_context"
                / "fixtures"
                / "context-watcher"
                / "level1"
                / "parser.json"
            )
            fixture.parent.mkdir(parents=True)
            fixture.write_text('{"provider_adapter":true}', encoding="utf-8")
            self.test_public_layout_contains_taf_context_engine_without_exposure()


if __name__ == "__main__":
    unittest.main()
