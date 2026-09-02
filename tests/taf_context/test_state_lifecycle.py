"""Tests for inspecting and reclaiming user-local TAF state."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from taf_context.state_lifecycle import (
    CURRENT_RUNTIME_VERSION,
    Candidate,
    apply_plan,
    plan_remove,
    summarize_state,
)
from taf_context.state_paths import StateError


def make_entry(root: Path, repo: str, worktree: str, *, bound: bool, generation: str = "gen-a") -> Path:
    entry = root / "repositories" / repo / worktree
    native = entry / "native" / "generations" / generation
    native.mkdir(parents=True)
    (native / "index.bin").write_bytes(b"x" * 1000)
    (native / "manifest.json").write_text("{}", encoding="utf-8")
    (native / "READY").write_text("sha256:" + generation, encoding="utf-8")
    (entry / "native" / "CURRENT").write_text(generation + "\n", encoding="utf-8")
    if bound:
        (entry / "binding.json").write_text(
            json.dumps({"schema_version": "1", "repository_identity": "sha256:" + repo,
                        "worktree_identity": "sha256:" + worktree, "index_identity": "sha256:" + "0" * 64}),
            encoding="utf-8",
        )
    return entry


def make_runtime(root: Path, version: str) -> Path:
    binary = root / "runtime" / version / "darwin-arm64" / "taf-level1"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"binary" * 100)
    return binary


class SummarizeStateTests(unittest.TestCase):
    def test_missing_root_summarizes_to_zero_without_creating_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "absent"
            summary = summarize_state(root)
        self.assertEqual(summary, {"root_bytes": 0, "entry_count": 0, "orphan_count": 0, "stale_runtime_count": 0})
        self.assertFalse(root.exists())

    def test_counts_entries_orphans_stale_runtimes_and_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_entry(root, "a" * 64, "1" * 64, bound=True)
            make_entry(root, "b" * 64, "1" * 64, bound=False)
            make_entry(root, "b" * 64, "2" * 64, bound=False)
            make_runtime(root, CURRENT_RUNTIME_VERSION)
            make_runtime(root, "0.0.1")
            summary = summarize_state(root)
        self.assertEqual(summary["entry_count"], 3)
        self.assertEqual(summary["orphan_count"], 2)
        self.assertEqual(summary["stale_runtime_count"], 1)
        self.assertGreater(summary["root_bytes"], 3 * 1000 + 2 * 600)

    def test_symlinked_content_is_not_followed_when_measuring(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            outside = Path(directory) / "outside"
            outside.mkdir()
            (outside / "big").write_bytes(b"y" * 100_000)
            make_entry(root, "a" * 64, "1" * 64, bound=True)
            os.symlink(outside, root / "repositories" / "link")
            summary = summarize_state(root)
        self.assertLess(summary["root_bytes"], 100_000)
        self.assertEqual(summary["entry_count"], 1)


class RemovePlanTests(unittest.TestCase):
    def test_plan_names_exactly_the_requested_worktree_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_entry(root, "a" * 64, "1" * 64, bound=True)
            make_entry(root, "a" * 64, "2" * 64, bound=True)
            plan = plan_remove(root, "a" * 64, "1" * 64)
            self.assertEqual([c.category for c in plan], ["worktree-entry"])
            self.assertEqual(plan[0].relative_path, f"repositories/{'a' * 64}/{'1' * 64}")
            self.assertGreater(plan[0].bytes, 1000)
            self.assertEqual(plan_remove(root, "c" * 64, "1" * 64), [])

    def test_apply_deletes_only_the_planned_entry_and_leaves_no_trash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = make_entry(root, "a" * 64, "1" * 64, bound=True)
            sibling = make_entry(root, "a" * 64, "2" * 64, bound=True)
            removed = apply_plan(root, plan_remove(root, "a" * 64, "1" * 64))
            self.assertEqual([c.relative_path for c in removed], [f"repositories/{'a' * 64}/{'1' * 64}"])
            self.assertFalse(target.exists())
            self.assertTrue(sibling.exists())
            self.assertEqual([p for p in root.iterdir() if p.name.startswith(".trash-")], [])

    def test_apply_refuses_symlink_and_outside_candidates_before_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            outside = Path(directory) / "outside"
            outside.mkdir()
            (outside / "keep").write_text("x", encoding="utf-8")
            make_entry(root, "a" * 64, "1" * 64, bound=True)
            link = root / "repositories" / ("b" * 64)
            link.mkdir()
            os.symlink(outside, link / ("1" * 64))
            bad = [Candidate("worktree-entry", f"repositories/{'b' * 64}/{'1' * 64}", 1)]
            with self.assertRaises(StateError) as caught:
                apply_plan(root, bad)
            self.assertEqual(caught.exception.code, "state-boundary-violation")
            self.assertTrue((outside / "keep").exists())
            escaping = [Candidate("worktree-entry", "../outside", 1)]
            with self.assertRaises(StateError):
                apply_plan(root, escaping)
            self.assertTrue((outside / "keep").exists())

    def test_apply_on_missing_root_raises_a_stable_state_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "absent"
            with self.assertRaises(StateError) as caught:
                apply_plan(root, [Candidate("worktree-entry", "repositories/x/y", 1)])
            self.assertEqual(caught.exception.code, "state-root-unavailable")
            self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
