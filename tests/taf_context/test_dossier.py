"""Behavioral tests for bounded, metadata-only Level 0 dossiers."""

from __future__ import annotations

from dataclasses import replace
import unittest

from taf_context.dossier import build_dossier
from taf_context.freshness import FreshnessAssessment
from taf_context.models import Freshness, RepositorySnapshot


def snapshot(**changes: object) -> RepositorySnapshot:
    values: dict[str, object] = {
        "schema_version": "1",
        "repository_identity": "sha256:repository",
        "canonical_root": "/secret/fixture-prefix/repo",
        "canonical_root_fingerprint": "sha256:root",
        "git_dir": "/secret/fixture-prefix/repo/.git/worktrees/private",
        "git_common_dir": "/secret/fixture-prefix/repo/.git",
        "git_common_dir_fingerprint": "sha256:common",
        "worktree_identity": "sha256:worktree",
        "head_sha": "a" * 40,
        "branch": "main",
        "dirty_fingerprint": "sha256:dirty",
        "dirty_fingerprint_complete": True,
        "tracked_paths": ("README.md", "src/app.py"),
        "staged_paths": (),
        "unstaged_paths": (),
        "untracked_paths": (),
        "ignored_entry_count": 0,
        "generated_or_vendored_count": 0,
        "binary_file_count": 0,
        "oversized_file_count": 0,
        "language_counts": (("Markdown", 1), ("Python", 1)),
        "candidate_artifacts": ("README.md",),
        "provider_markers": (),
        "insertions": 0,
        "deletions": 0,
        "dirty_bytes_hashed": 0,
        "warnings": (),
    }
    values.update(changes)
    return RepositorySnapshot(**values)  # type: ignore[arg-type]


def assessment(**changes: object) -> FreshnessAssessment:
    values: dict[str, object] = {
        "freshness": Freshness.EXACT,
        "reason_codes": ("exact-match",),
        "requires_rebuild": False,
        "can_incrementally_update": False,
    }
    values.update(changes)
    return FreshnessAssessment(**values)  # type: ignore[arg-type]


class DossierStructureTests(unittest.TestCase):
    def test_sections_are_stable_and_only_metadata_is_rendered(self) -> None:
        source = snapshot(
            staged_paths=("src/staged.py",),
            unstaged_paths=("src/unstaged.py",),
            untracked_paths=("notes/untracked.md",),
            tracked_paths=("README.md", "src/staged.py", "src/unstaged.py"),
            candidate_artifacts=("README.md", "docs/superpowers/specs/design.md"),
            warnings=("https://private.invalid/source",),
        )

        result = build_dossier(source, assessment())

        headings = (
            "# TAF Level 0 Context",
            "## Scope",
            "## Freshness",
            "## Worktree State",
            "## Changed Paths",
            "## Candidate Artifacts",
            "## Coverage and Warnings",
            "## Next Safe Action",
        )
        positions = tuple(result.markdown.index(heading) for heading in headings)
        self.assertEqual(positions, tuple(sorted(positions)))
        for path in (
            "src/staged.py",
            "src/unstaged.py",
            "notes/untracked.md",
            "README.md",
            "docs/superpowers/specs/design.md",
        ):
            self.assertIn(path, result.markdown)
        self.assertEqual(result.markdown.count("- kind="), 5)
        for forbidden in (
            source.canonical_root,
            source.git_dir,
            source.git_common_dir,
            "/secret/fixture-prefix",
            "private source bytes",
            "https://private.invalid/source",
        ):
            self.assertNotIn(forbidden, result.markdown)
        self.assertEqual(result.characters_used, len(result.markdown))
        self.assertFalse(result.truncated)
        self.assertEqual(result.omitted_item_count, 0)

    def test_paths_are_ranked_by_state_then_metadata_only_candidate(self) -> None:
        source = snapshot(
            staged_paths=("z-staged.py",),
            unstaged_paths=("a-unstaged.py", "z-staged.py"),
            untracked_paths=("0-untracked.py",),
            candidate_artifacts=("README.md", "z-staged.py"),
        )

        result = build_dossier(source, assessment())

        ranked = tuple(
            result.markdown.index(path)
            for path in (
                "z-staged.py",
                "a-unstaged.py",
                "0-untracked.py",
                "README.md",
            )
        )
        self.assertEqual(ranked, tuple(sorted(ranked)))
        self.assertEqual(result.markdown.count("z-staged.py"), 1)
        self.assertEqual(result.markdown.count("- kind="), 4)

    def test_rejects_non_relative_or_traversing_paths_before_rendering(self) -> None:
        cases = (
            snapshot(
                staged_paths=("/secret/fixture-prefix/absolute.py",),
                candidate_artifacts=(),
            ),
            snapshot(
                candidate_artifacts=("docs/../secret/fixture-prefix/private.md",)
            ),
        )

        for source in cases:
            with self.subTest(source=source):
                with self.assertRaisesRegex(ValueError, "repository-relative"):
                    build_dossier(source, assessment())


class DossierBudgetTests(unittest.TestCase):
    def test_1024_character_budget_is_line_atomic_with_exact_omissions(self) -> None:
        paths = tuple(
            f"src/component-{index:03d}-with-a-deliberately-long-name.py"
            for index in range(40)
        )
        source = snapshot(
            staged_paths=paths, tracked_paths=paths, candidate_artifacts=()
        )

        result = build_dossier(source, assessment(), max_chars=1024)

        included = sum(path in result.markdown for path in paths)
        self.assertLessEqual(result.characters_used, 1024)
        self.assertTrue(result.markdown.endswith("\n"))
        self.assertTrue(result.truncated)
        self.assertEqual(result.omitted_item_count, 40 - included)
        self.assertIn(f"omitted-item-count={40 - included}\n", result.markdown)
        self.assertNotIn("\ufffd", result.markdown)

    def test_12000_character_budget_has_exact_omissions_for_unicode_paths(self) -> None:
        paths = tuple(
            f"docs/İstanbul-çalışma-{index:03d}-{'x' * 45}.md"
            for index in range(220)
        )
        source = snapshot(untracked_paths=paths, candidate_artifacts=paths)

        result = build_dossier(source, assessment(), max_chars=12000)

        included = sum(path in result.markdown for path in paths)
        self.assertLessEqual(result.characters_used, 12000)
        self.assertTrue(result.truncated)
        self.assertEqual(result.omitted_item_count, 220 - included)
        self.assertIn(f"omitted-item-count={220 - included}\n", result.markdown)
        self.assertEqual(result.markdown.encode("utf-8").decode("utf-8"), result.markdown)

    def test_compact_mandatory_lines_preserve_required_signals(self) -> None:
        source = snapshot(
            staged_paths=tuple(f"p/{index}.py" for index in range(200)),
            unstaged_paths=("also-dirty.py",),
            untracked_paths=("new.py",),
            warnings=tuple(f"warning-{index}-{'x' * 400}" for index in range(20)),
            dirty_fingerprint_complete=False,
        )
        partial = replace(
            assessment(),
            freshness=Freshness.PARTIAL,
            reason_codes=("dirty-fingerprint-incomplete",),
        )

        result = build_dossier(source, partial, max_chars=1024)

        self.assertIn("freshness=partial", result.markdown)
        self.assertIn("staged=200", result.markdown)
        self.assertIn("unstaged=1", result.markdown)
        self.assertIn("untracked=1", result.markdown)
        self.assertIn("warning-count=21", result.markdown)
        self.assertIn("next-action=", result.markdown)
        self.assertLessEqual(result.characters_used, 1024)

    def test_budgets_outside_the_hard_range_are_rejected(self) -> None:
        for invalid in (True, 1023, 12001):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    build_dossier(snapshot(), assessment(), max_chars=invalid)


if __name__ == "__main__":
    unittest.main()
