"""Tests for inspecting and reclaiming user-local TAF state."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from taf_context.state_lifecycle import (
    CURRENT_RUNTIME_VERSION,
    Candidate,
    apply_plan,
    plan_gc,
    plan_prune_generations,
    plan_remove,
    summarize_state,
)
from taf_context.state_paths import StateError


def make_entry(
    root: Path, repo: str, worktree: str, *, bound: bool, generation: str = "e" * 64, schema: str = "1"
) -> Path:
    entry = root / "repositories" / repo / worktree
    native = entry / "native" / "generations" / generation
    native.mkdir(parents=True)
    (native / "index.bin").write_bytes(b"x" * 1000)
    (native / "manifest.json").write_text("{}", encoding="utf-8")
    (native / "READY").write_text("sha256:" + generation, encoding="utf-8")
    (entry / "native" / "CURRENT").write_text(generation + "\n", encoding="utf-8")
    if bound:
        payload = {
            "schema_version": schema,
            "repository_identity": "sha256:" + repo,
            "worktree_identity": "sha256:" + worktree,
            "index_identity": "sha256:" + "0" * 64,
        }
        if schema == "2":
            payload.update(
                {
                    "head_sha": "a" * 40,
                    "dirty_fingerprint": "sha256:" + "b" * 64,
                    "dirty_paths": [],
                }
            )
        (entry / "binding.json").write_text(json.dumps(payload), encoding="utf-8")
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

    def test_schema_2_binding_is_valid_and_large_bindings_are_allowed_up_to_one_mebibyte(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = make_entry(root, "a" * 64, "1" * 64, bound=True, schema="2")
            self.assertEqual(summarize_state(root)["orphan_count"], 0)
            padded = json.loads((entry / "binding.json").read_text(encoding="utf-8"))
            padded["dirty_paths"] = ["p" * 200] * 4000
            (entry / "binding.json").write_text(json.dumps(padded), encoding="utf-8")
            self.assertEqual(summarize_state(root)["orphan_count"], 0)


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

    def test_apply_validates_every_candidate_before_deleting_any(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid = make_entry(root, "a" * 64, "1" * 64, bound=True)
            plan = plan_remove(root, "a" * 64, "1" * 64) + [Candidate("worktree-entry", "../outside", 1)]
            with self.assertRaises(StateError):
                apply_plan(root, plan)
            self.assertTrue(valid.exists())


class GcPlanTests(unittest.TestCase):
    def _populate(self, root: Path, now: float) -> dict[str, Path]:
        fresh = make_entry(root, "a" * 64, "1" * 64, bound=True)
        os.utime(fresh / "binding.json", (now, now))
        old = make_entry(root, "a" * 64, "2" * 64, bound=True)
        stamp = now - 40 * 86400
        os.utime(old / "binding.json", (stamp, stamp))
        orphan = make_entry(root, "b" * 64, "1" * 64, bound=False)
        extra_generation = fresh / "native" / "generations" / ("f" * 64)
        extra_generation.mkdir()
        (extra_generation / "index.bin").write_bytes(b"z" * 10)
        staging = fresh / "native" / "generations" / ".stage-abc"
        staging.mkdir()
        make_runtime(root, CURRENT_RUNTIME_VERSION)
        make_runtime(root, "0.0.9")
        (root / "providers.json").write_text("[]", encoding="utf-8")
        trash = root / ".trash-deadbeef"
        trash.mkdir()
        (trash / "leftover").write_text("x", encoding="utf-8")
        return {"fresh": fresh, "old": old, "orphan": orphan, "extra": extra_generation, "staging": staging, "trash": trash}

    def test_plan_lists_every_category_and_spares_fresh_state(self) -> None:
        now = time.time()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._populate(root, now)
            plan = plan_gc(root, unused_for_days=30, now=now)
        by_category = {}
        for item in plan:
            by_category.setdefault(item.category, []).append(item.relative_path)
        self.assertEqual(by_category["orphan-entry"], [f"repositories/{'b' * 64}/{'1' * 64}"])
        self.assertEqual(by_category["unused-entry"], [f"repositories/{'a' * 64}/{'2' * 64}"])
        self.assertEqual(by_category["stale-runtime"], ["runtime/0.0.9"])
        self.assertEqual(
            sorted(by_category["unreferenced-generation"]),
            sorted([
                f"repositories/{'a' * 64}/{'1' * 64}/native/generations/.stage-abc",
                f"repositories/{'a' * 64}/{'1' * 64}/native/generations/{'f' * 64}",
            ]),
        )
        self.assertEqual(by_category["legacy-control-file"], ["providers.json"])
        self.assertEqual(by_category["trash-leftover"], [".trash-deadbeef"])
        self.assertEqual(by_category["empty-parent"], [f"repositories/{'b' * 64}"])
        self.assertNotIn(f"repositories/{'a' * 64}/{'1' * 64}", [c.relative_path for c in plan])
        self.assertEqual(
            [c.category for c in plan],
            sorted([c.category for c in plan], key=["orphan-entry", "unused-entry", "stale-runtime",
                   "unreferenced-generation", "legacy-control-file", "trash-leftover", "empty-parent"].index),
        )

    def test_unused_for_zero_treats_every_bound_entry_as_unused(self) -> None:
        now = time.time()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._populate(root, now)
            plan = plan_gc(root, unused_for_days=0, now=now + 1)
        unused = sorted(c.relative_path for c in plan if c.category == "unused-entry")
        self.assertEqual(unused, [f"repositories/{'a' * 64}/{'1' * 64}", f"repositories/{'a' * 64}/{'2' * 64}"])

    def test_apply_gc_removes_exactly_the_plan(self) -> None:
        now = time.time()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._populate(root, now)
            removed = apply_plan(root, plan_gc(root, unused_for_days=30, now=now))
            self.assertTrue(paths["fresh"].exists())
            self.assertTrue((paths["fresh"] / "native" / "generations" / ("e" * 64)).exists())
            for key in ("old", "orphan", "extra", "staging", "trash"):
                self.assertFalse(paths[key].exists(), key)
            self.assertFalse((root / "runtime" / "0.0.9").exists())
            self.assertTrue((root / "runtime" / CURRENT_RUNTIME_VERSION).exists())
            self.assertFalse((root / "providers.json").exists())
            self.assertFalse((root / "repositories" / ("b" * 64)).exists())
            self.assertEqual(len(removed), 8)

    def test_already_empty_repository_directory_is_an_empty_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "repositories" / ("c" * 64)).mkdir(parents=True)
            plan = plan_gc(root, unused_for_days=30, now=time.time())
            self.assertEqual(
                [(c.category, c.relative_path) for c in plan],
                [("empty-parent", f"repositories/{'c' * 64}")],
            )

    def test_missing_current_pointer_proposes_no_generation(self) -> None:
        now = time.time()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = make_entry(root, "a" * 64, "1" * 64, bound=True)
            os.utime(entry / "binding.json", (now, now))
            (entry / "native" / "CURRENT").unlink()
            plan = plan_gc(root, unused_for_days=30, now=now)
            self.assertEqual([c for c in plan if c.category == "unreferenced-generation"], [])
            self.assertTrue((entry / "native" / "generations" / ("e" * 64)).exists())

    def test_corrupt_current_pointer_proposes_no_generation(self) -> None:
        now = time.time()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = make_entry(root, "a" * 64, "1" * 64, bound=True)
            os.utime(entry / "binding.json", (now, now))
            (entry / "native" / "CURRENT").write_text("not-a-generation-id\n", encoding="utf-8")
            plan = plan_gc(root, unused_for_days=30, now=now)
            self.assertEqual([c for c in plan if c.category == "unreferenced-generation"], [])
            self.assertTrue((entry / "native" / "generations" / ("e" * 64)).exists())

    def test_unrecognised_repository_directory_is_never_a_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stray = root / "repositories" / "not-an-identity"
            stray.mkdir(parents=True)
            (stray / "important.txt").write_text("keep", encoding="utf-8")
            plan = plan_gc(root, unused_for_days=30, now=time.time())
            self.assertEqual(plan, [])

    def test_empty_parent_reports_residual_file_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_entry(root, "b" * 64, "1" * 64, bound=False)
            (root / "repositories" / ("b" * 64) / ".DS_Store").write_bytes(b"x" * 5000)
            plan = plan_gc(root, unused_for_days=30, now=time.time())
            parents = [c for c in plan if c.category == "empty-parent"]
            self.assertEqual([c.bytes for c in parents], [5000])

    def test_entry_unused_for_exactly_the_cutoff_is_reclaimed(self) -> None:
        now = 1_700_000_000.0
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = make_entry(root, "a" * 64, "1" * 64, bound=True)
            stamp = now - 30 * 86400
            os.utime(entry / "binding.json", (stamp, stamp))
            plan = plan_gc(root, unused_for_days=30, now=now)
            self.assertEqual([c.category for c in plan if c.relative_path.endswith("1" * 64)], ["unused-entry"])
            os.utime(entry / "binding.json", (stamp + 1, stamp + 1))
            plan = plan_gc(root, unused_for_days=30, now=now)
            self.assertEqual([c for c in plan if c.category == "unused-entry"], [])

    def test_current_pointer_with_padding_proposes_no_generation_sweep(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = make_entry(root, "a" * 64, "1" * 64, bound=True)
            extra = entry / "native" / "generations" / ("f" * 64)
            extra.mkdir()
            for token in ("  " + "e" * 64 + "\n", "e" * 64 + "\n\n", "\t" + "e" * 64):
                (entry / "native" / "CURRENT").write_text(token, encoding="utf-8")
                plan = plan_gc(root, unused_for_days=30, now=time.time())
                self.assertEqual([c for c in plan if c.category == "unreferenced-generation"], [], repr(token))
            (entry / "native" / "CURRENT").write_text("e" * 64 + "\n", encoding="utf-8")
            plan = plan_gc(root, unused_for_days=30, now=time.time())
            self.assertEqual(
                [c.relative_path for c in plan if c.category == "unreferenced-generation"],
                [f"repositories/{'a' * 64}/{'1' * 64}/native/generations/{'f' * 64}"],
            )

    def test_repository_directory_vanishing_during_plan_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_entry(root, "b" * 64, "1" * 64, bound=False)
            original = Path.iterdir

            def flaky_iterdir(self: Path):
                if self.name == "b" * 64:
                    raise FileNotFoundError(str(self))
                return original(self)

            with mock.patch.object(Path, "iterdir", flaky_iterdir):
                plan = plan_gc(root, unused_for_days=30, now=time.time())
            self.assertEqual([c.category for c in plan if c.category == "empty-parent"], ["empty-parent"])


class PruneGenerationsTests(unittest.TestCase):
    def test_prunes_only_unreferenced_generations_older_than_the_grace_period(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = make_entry(root, "a" * 64, "1" * 64, bound=True, schema="2", generation="c" * 64)
            generations = entry / "native" / "generations"
            old, young, staging = generations / ("0" * 64), generations / ("1" * 64), generations / ".stage-abc"
            for path in (old, young, staging):
                path.mkdir()
                (path / "index.bin").write_bytes(b"x")
            now = 1_700_000_000.0
            os.utime(old, (now - 120, now - 120))
            os.utime(staging, (now - 120, now - 120))
            os.utime(young, (now - 10, now - 10))
            plan = plan_prune_generations(root, entry, now=now)
            self.assertEqual(sorted(c.relative_path for c in plan), sorted([
                old.relative_to(root).as_posix(), staging.relative_to(root).as_posix(),
            ]))
            self.assertTrue(all(c.category == "unreferenced-generation" for c in plan))
            apply_plan(root, plan)
            self.assertEqual(sorted(p.name for p in generations.iterdir()), sorted(["1" * 64, "c" * 64]))

    def test_unreadable_current_pointer_prunes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = make_entry(root, "a" * 64, "1" * 64, bound=True)
            (entry / "native" / "CURRENT").write_text("garbage\n", encoding="utf-8")
            stale = entry / "native" / "generations" / ("0" * 64)
            stale.mkdir()
            os.utime(stale, (1, 1))
            self.assertEqual(plan_prune_generations(root, entry, now=1_700_000_000.0), [])


if __name__ == "__main__":
    unittest.main()
