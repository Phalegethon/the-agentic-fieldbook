"""Integration tests for deterministic work-recovery collection."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from taf_context.recovery import (
    RecoveryRequest,
    collect_recovery,
    resolve_recovery_base,
)
from taf_context.recovery_models import WorkState

from .repo_factory import commit_all, init_committed_repo, init_repo, run, write


class RecoveryBaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_explicit_base_has_priority(self) -> None:
        repo = init_committed_repo(self.root / "repo")
        run(repo, "git", "branch", "release")

        resolution = resolve_recovery_base(repo, "release")

        self.assertEqual(resolution.ref, "release")
        self.assertEqual(resolution.source, "explicit")
        self.assertEqual(resolution.sha, run(repo, "git", "rev-parse", "release"))

    def test_main_upstream_is_used_but_feature_upstream_is_ignored(self) -> None:
        repo = init_committed_repo(self.root / "repo")
        run(repo, "git", "update-ref", "refs/remotes/origin/main", "HEAD")
        run(repo, "git", "checkout", "-b", "feature")
        run(repo, "git", "config", "branch.feature.remote", "origin")
        run(repo, "git", "config", "branch.feature.merge", "refs/heads/main")

        upstream_main = resolve_recovery_base(repo, None)

        self.assertEqual(upstream_main.ref, "refs/remotes/origin/main")
        self.assertEqual(upstream_main.source, "upstream-main")

        run(repo, "git", "update-ref", "refs/remotes/origin/feature", "HEAD")
        run(repo, "git", "config", "branch.feature.merge", "refs/heads/feature")
        ignored_feature = resolve_recovery_base(repo, None)

        self.assertEqual(ignored_feature.ref, "refs/heads/main")
        self.assertEqual(ignored_feature.source, "local-main")

    def test_origin_head_precedes_local_main(self) -> None:
        repo = init_committed_repo(self.root / "repo")
        first = run(repo, "git", "rev-parse", "HEAD")
        write(repo / "tracked.txt", "second\n")
        second = commit_all(repo, "second")
        run(repo, "git", "update-ref", "refs/remotes/origin/trunk", first)
        run(repo, "git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")

        resolution = resolve_recovery_base(repo, None)

        self.assertEqual(resolution.ref, "refs/remotes/origin/trunk")
        self.assertEqual(resolution.sha, first)
        self.assertNotEqual(resolution.sha, second)
        self.assertEqual(resolution.source, "origin-head")

    def test_local_master_is_used_when_main_is_absent(self) -> None:
        repo = init_committed_repo(self.root / "repo")
        run(repo, "git", "branch", "-m", "master")

        resolution = resolve_recovery_base(repo, None)

        self.assertEqual(resolution.ref, "refs/heads/master")
        self.assertEqual(resolution.source, "local-master")

    def test_unborn_repository_returns_unknown_base(self) -> None:
        repo = init_repo(self.root / "repo")

        resolution = resolve_recovery_base(repo, None)

        self.assertIsNone(resolution.ref)
        self.assertIsNone(resolution.sha)
        self.assertEqual(resolution.source, "unknown")
        self.assertIsNotNone(resolution.warning)

    def test_invalid_explicit_base_fails_closed(self) -> None:
        repo = init_committed_repo(self.root / "repo")

        with self.assertRaisesRegex(ValueError, "base"):
            resolve_recovery_base(repo, "missing-ref")


class RecoveryStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _feature_repo(self) -> tuple[Path, str]:
        repo = init_committed_repo(self.root / "repo")
        base = run(repo, "git", "rev-parse", "main")
        run(repo, "git", "checkout", "-b", "feature")
        return repo, base

    def test_dirty_current_worktree_is_active_dirty(self) -> None:
        repo, _ = self._feature_repo()
        write(repo / "tracked.txt", "unstaged\n")
        write(repo / "staged.txt", "staged\n")
        run(repo, "git", "add", "staged.txt")
        write(repo / "untracked.txt", "private\n")

        result = collect_recovery(RecoveryRequest(repo=repo, base="main"))

        current = result.dossier.current
        self.assertIs(current.state, WorkState.ACTIVE_DIRTY)
        self.assertEqual((current.staged_count, current.unstaged_count, current.untracked_count), (1, 1, 1))
        self.assertIn("dirty-tracked", current.reason_codes)

    def test_clean_unique_commit_is_active_committed(self) -> None:
        repo, _ = self._feature_repo()
        write(repo / "feature.txt", "feature\n")
        commit_all(repo, "feature")

        state = collect_recovery(RecoveryRequest(repo=repo, base="main")).dossier.current

        self.assertIs(state.state, WorkState.ACTIVE_COMMITTED)
        self.assertEqual((state.ahead_count, state.behind_count), (1, 0))

    def test_head_reachable_from_base_is_integrated(self) -> None:
        repo, _ = self._feature_repo()

        state = collect_recovery(RecoveryRequest(repo=repo, base="main")).dossier.current

        self.assertIs(state.state, WorkState.INTEGRATED)

    def test_diverged_branch_is_classified_before_active_committed(self) -> None:
        repo, _ = self._feature_repo()
        write(repo / "feature.txt", "feature\n")
        commit_all(repo, "feature")
        run(repo, "git", "checkout", "main")
        write(repo / "main.txt", "main\n")
        commit_all(repo, "main")
        run(repo, "git", "checkout", "feature")

        state = collect_recovery(RecoveryRequest(repo=repo, base="main")).dossier.current

        self.assertIs(state.state, WorkState.DIVERGED)
        self.assertEqual((state.ahead_count, state.behind_count), (1, 1))

    def test_unresolved_base_is_clean_unresolved(self) -> None:
        repo = init_repo(self.root / "repo")

        state = collect_recovery(RecoveryRequest(repo=repo)).dossier.current

        self.assertIs(state.state, WorkState.CLEAN_UNRESOLVED)

    def test_other_worktree_is_metadata_only_candidate(self) -> None:
        repo, _ = self._feature_repo()
        write(repo / "feature.txt", "feature\n")
        commit_all(repo, "feature")
        linked = self.root / "linked"
        run(repo, "git", "worktree", "add", "-b", "other", str(linked), "main")
        write(linked / "tracked.txt", "dirty elsewhere\n")

        dossier = collect_recovery(RecoveryRequest(repo=repo, base="main")).dossier

        self.assertEqual(len(dossier.candidates), 1)
        self.assertEqual(dossier.candidates[0].branch, "other")
        self.assertEqual(dossier.candidates[0].staged_count, 0)
        self.assertEqual(dossier.candidates[0].unstaged_count, 0)
        self.assertIn("metadata-only", dossier.candidates[0].reason_codes)


if __name__ == "__main__":
    unittest.main()
