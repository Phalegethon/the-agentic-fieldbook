"""Tests for incremental refresh: change deltas and the engine's change document."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from taf_context.git_snapshot import collect_snapshot
from taf_context.refresh import (
    CHANGE_DOCUMENT_NAME,
    Binding,
    RefreshLock,
    build_change_document,
    change_manifest_identity,
    changed_paths_between,
    dirty_paths_of,
    remove_change_document,
    write_change_document,
)
from tests.taf_context.repo_factory import commit_all, init_committed_repo, init_repo, run, write

SHA_A, SHA_B, SHA_C, SHA_D, SHA_E = ("sha256:" + char * 64 for char in "abcde")


def _fields(paths: list[str]) -> dict:
    return {
        "schema_version": "1", "prior_index_identity": SHA_A,
        "before_repository_identity": SHA_B, "before_worktree_identity": SHA_C,
        "before_committed_head": "1" * 40, "before_dirty_overlay_fingerprint": SHA_D,
        "after_repository_identity": SHA_B, "after_worktree_identity": SHA_C,
        "after_committed_head": "2" * 40, "after_dirty_overlay_fingerprint": SHA_E,
        "changed_paths": paths,
    }


def _bind(snapshot, index: str = SHA_A) -> Binding:
    return Binding(index, snapshot.head_sha, snapshot.dirty_fingerprint, dirty_paths_of(snapshot))


class ChangeManifestIdentityTests(unittest.TestCase):
    def test_matches_the_engine_vectors(self) -> None:
        # Vectors computed with the Go engine's changeManifestIdentity
        # (encoding/json: sorted keys, no whitespace, <>& escaped, UTF-8 kept).
        self.assertEqual(
            change_manifest_identity(_fields(["a&b/<c>.py", "src/app.py", "ünï.txt"])),
            "sha256:44aa89bde5534d67efb628ab0ebfdc5bb4d6cb8903b84e77f17a97a2bff89100",
        )
        self.assertEqual(
            change_manifest_identity(_fields([])),
            "sha256:4fe339fc1f67421e5fe63940adebf2d35a8ced4db4e360aaea203cf27c3fa638",
        )


class ChangedPathsTests(unittest.TestCase):
    def test_commit_edit_delete_rename_and_untracked_are_all_listed_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = init_committed_repo(Path(directory) / "repo")
            write(repo / "src/a.py", "a\n"); write(repo / "src/b.py", "b\n"); write(repo / "src/c.py", "c\n")
            commit_all(repo, "baseline")
            write(repo / "scratch.txt", "old dirty\n")
            before = collect_snapshot(repo)
            binding = _bind(before)
            # Commit: edit a, delete b, rename c -> d; keep scratch dirty, add e untracked.
            write(repo / "src/a.py", "a2\n"); (repo / "src/b.py").unlink()
            run(repo, "git", "mv", "src/c.py", "src/d.py")
            commit_all(repo, "changes")
            write(repo / "src/e.py", "e\n")
            after = collect_snapshot(repo)
            self.assertEqual(
                changed_paths_between(binding, after),
                ["scratch.txt", "src/a.py", "src/b.py", "src/c.py", "src/d.py", "src/e.py"],
            )

    def test_edit_without_commit_lists_only_dirty_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = init_committed_repo(Path(directory) / "repo")
            binding = _bind(collect_snapshot(repo))
            write(repo / "tracked.txt", "edited\n")
            self.assertEqual(changed_paths_between(binding, collect_snapshot(repo)), ["tracked.txt"])

    def test_reverted_dirty_path_is_still_listed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = init_committed_repo(Path(directory) / "repo")
            write(repo / "tracked.txt", "edited\n")
            binding = _bind(collect_snapshot(repo))
            write(repo / "tracked.txt", "tracked\n")  # back to the committed content
            self.assertEqual(changed_paths_between(binding, collect_snapshot(repo)), ["tracked.txt"])

    def test_branch_switch_lists_every_differing_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = init_committed_repo(Path(directory) / "repo")
            binding = _bind(collect_snapshot(repo))
            run(repo, "git", "checkout", "-q", "-b", "feature")
            for index in range(300):
                write(repo / f"gen/file_{index:03d}.py", f"x = {index}\n")
            commit_all(repo, "many files")
            changed = changed_paths_between(binding, collect_snapshot(repo))
            self.assertEqual(len(changed), 300)
            self.assertEqual(changed, sorted(changed))

    def test_unborn_then_first_commit_has_no_delta(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = init_repo(Path(directory) / "repo")
            write(repo / "a.txt", "a\n")
            unborn = collect_snapshot(repo)
            self.assertIsNone(unborn.head_sha)
            binding = Binding(SHA_A, None, None, None)  # a build never binds an unborn repo
            commit_all(repo, "first")
            self.assertIsNone(changed_paths_between(binding, collect_snapshot(repo)))

    def test_missing_old_commit_and_schema_1_binding_yield_no_delta(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = init_committed_repo(Path(directory) / "repo")
            snapshot = collect_snapshot(repo)
            gone = Binding(SHA_A, "f" * 40, snapshot.dirty_fingerprint, ())
            self.assertIsNone(changed_paths_between(gone, snapshot))
            self.assertIsNone(changed_paths_between(Binding(SHA_A, None, None, None), snapshot))

    def test_too_many_paths_and_unsafe_paths_yield_no_delta(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = init_committed_repo(Path(directory) / "repo")
            snapshot = collect_snapshot(repo)
            many = Binding(SHA_A, snapshot.head_sha, snapshot.dirty_fingerprint, tuple(f"p/{i}" for i in range(10001)))
            self.assertIsNone(changed_paths_between(many, snapshot))
            for unsafe in ("../x", "/abs", "a//b", "a/./b", "back\\slash", "nul\x00"):
                weird = Binding(SHA_A, snapshot.head_sha, snapshot.dirty_fingerprint, (unsafe,))
                self.assertIsNone(changed_paths_between(weird, snapshot), unsafe)

    def test_git_timeout_or_os_error_yields_no_delta_instead_of_raising(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = init_committed_repo(Path(directory) / "repo")
            binding = _bind(collect_snapshot(repo))
            write(repo / "tracked.txt", "edited\n")
            commit_all(repo, "second")
            snapshot = collect_snapshot(repo)
            real_run = subprocess.run

            def timing_out(argv: list[str], **kwargs: object):
                if "diff" in argv:
                    raise subprocess.TimeoutExpired(argv, 20)
                return real_run(argv, **kwargs)

            with mock.patch("taf_context.git_snapshot.subprocess.run", side_effect=timing_out):
                self.assertIsNone(changed_paths_between(binding, snapshot))


class ChangeDocumentTests(unittest.TestCase):
    def test_document_has_the_twelve_fields_and_a_valid_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = init_committed_repo(Path(directory) / "repo")
            before = collect_snapshot(repo)
            binding = _bind(before)
            write(repo / "tracked.txt", "edited\n")
            after = collect_snapshot(repo)
            document = build_change_document(binding, after, ["tracked.txt"])
            self.assertEqual(
                sorted(document),
                sorted(["schema_version", "prior_index_identity", "before_repository_identity", "before_worktree_identity",
                        "before_committed_head", "before_dirty_overlay_fingerprint", "after_repository_identity",
                        "after_worktree_identity", "after_committed_head", "after_dirty_overlay_fingerprint",
                        "level0_change_manifest_identity", "changed_paths"]),
            )
            self.assertEqual(document["prior_index_identity"], SHA_A)
            self.assertEqual(document["before_committed_head"], before.head_sha)
            self.assertEqual(document["before_dirty_overlay_fingerprint"], before.dirty_fingerprint)
            self.assertEqual(document["after_dirty_overlay_fingerprint"], after.dirty_fingerprint)
            fields = {key: value for key, value in document.items() if key != "level0_change_manifest_identity"}
            self.assertEqual(document["level0_change_manifest_identity"], change_manifest_identity(fields))

    def test_write_and_remove_use_a_private_file_under_the_state_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_root = Path(directory) / "native"
            state_root.mkdir()
            name = write_change_document(state_root, {"schema_version": "1", "changed_paths": ["a"]})
            self.assertEqual(name, CHANGE_DOCUMENT_NAME)
            path = state_root / CHANGE_DOCUMENT_NAME
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_bytes()), {"schema_version": "1", "changed_paths": ["a"]})
            self.assertEqual([p.name for p in state_root.iterdir()], [CHANGE_DOCUMENT_NAME])
            remove_change_document(state_root)
            self.assertFalse(path.exists())
            remove_change_document(state_root)  # idempotent


class RefreshLockTests(unittest.TestCase):
    def test_lock_is_created_and_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with RefreshLock(root) as lock:
                self.assertTrue((root / ".refresh.lock").is_file())
                self.assertFalse(lock.waited)
            self.assertFalse((root / ".refresh.lock").exists())

    def test_young_foreign_lock_is_waited_for_then_bypassed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".refresh.lock").write_text("999999 0\n", encoding="utf-8")
            started = time.monotonic()
            with RefreshLock(root, wait=0.3, poll=0.05) as lock:
                self.assertTrue(lock.waited)
            self.assertGreaterEqual(time.monotonic() - started, 0.25)
            self.assertTrue((root / ".refresh.lock").exists())  # not ours: left alone

    def test_stale_foreign_lock_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / ".refresh.lock"
            lock_path.write_text("999999 0\n", encoding="utf-8")
            os.utime(lock_path, (1, 1))
            with RefreshLock(root) as lock:
                self.assertFalse(lock.waited)
                self.assertIn(str(os.getpid()), lock_path.read_text(encoding="utf-8"))
            self.assertFalse(lock_path.exists())

    def test_stale_lock_that_cannot_be_removed_is_bypassed_after_wait(self) -> None:
        # A stale lock whose unlink keeps failing (read-only directory, foreign
        # process recreating the file) must not loop forever: it proceeds
        # without the lock once `wait` elapses, exactly like a young lock.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / ".refresh.lock"
            lock_path.write_text("999999 0\n", encoding="utf-8")
            os.utime(lock_path, (1, 1))
            started = time.monotonic()
            with mock.patch("taf_context.refresh.os.unlink", side_effect=PermissionError("locked")):
                with RefreshLock(root, wait=0.3, poll=0.05) as lock:
                    self.assertTrue(lock.waited)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 1.0)
            self.assertTrue(lock_path.exists())  # never removed: unlink always failed
