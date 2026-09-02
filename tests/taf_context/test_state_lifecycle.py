"""Tests for inspecting and reclaiming user-local TAF state."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from taf_context.state_lifecycle import CURRENT_RUNTIME_VERSION, summarize_state


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


if __name__ == "__main__":
    unittest.main()
