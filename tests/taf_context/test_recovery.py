"""Integration tests for deterministic work-recovery collection."""

from __future__ import annotations

import tempfile
import unittest
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from taf_context.recovery import (
    RecoveryError,
    RecoveryRequest,
    _relation,
    _run_git,
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

    def test_merge_base_timeout_is_reported_as_recovery_error(self) -> None:
        repo = init_committed_repo(self.root / "repo")
        head = run(repo, "git", "rev-parse", "HEAD")
        relation_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"0\t0\n", stderr=b""
        )

        with patch(
            "taf_context.recovery.subprocess.run",
            side_effect=[relation_result, subprocess.TimeoutExpired(["git", "merge-base"], 20)],
        ):
            with self.assertRaisesRegex(RecoveryError, "relation"):
                _relation(repo, head, head)


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
        commit_all(repo, "finish parser boundary")

        dossier = collect_recovery(RecoveryRequest(repo=repo, base="main")).dossier
        state = dossier.current

        self.assertIs(state.state, WorkState.ACTIVE_COMMITTED)
        self.assertEqual((state.ahead_count, state.behind_count), (1, 0))
        subject = next(claim for claim in dossier.claims if claim.claim_id == "commit.tip-subject")
        self.assertEqual(subject.evidence_class.value, "observed")
        self.assertIn("finish parser boundary", subject.text)

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
        self.assertIs(dossier.candidates[0].state, WorkState.UNKNOWN)
        self.assertIn("metadata-only", dossier.candidates[0].reason_codes)
        self.assertIn("dirty-state-unobserved", dossier.candidates[0].reason_codes)

    def test_untracked_only_work_recommends_exact_content_authorization(self) -> None:
        repo, _ = self._feature_repo()
        write(repo / "new-module.py", "unfinished\n")

        dossier = collect_recovery(RecoveryRequest(repo=repo, base="main")).dossier

        self.assertIs(dossier.current.state, WorkState.ACTIVE_DIRTY)
        self.assertEqual(dossier.current.staged_count, 0)
        self.assertEqual(dossier.current.unstaged_count, 0)
        self.assertEqual(dossier.current.untracked_count, 1)
        self.assertEqual(
            dossier.next_action_hint,
            "Review the untracked path metadata and authorize only the exact content needed.",
        )


class RecoveryEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = init_committed_repo(self.root / "repo")
        run(self.repo, "git", "checkout", "-b", "feature")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_staged_and_unstaged_diff_evidence_remain_separate(self) -> None:
        write(self.repo / "staged.txt", "before\n")
        run(self.repo, "git", "add", "staged.txt")
        run(self.repo, "git", "commit", "-m", "add staged fixture")
        write(self.repo / "staged.txt", "staged evidence\n")
        run(self.repo, "git", "add", "staged.txt")
        write(self.repo / "tracked.txt", "unstaged evidence\n")

        result = collect_recovery(RecoveryRequest(repo=self.repo, base="main", max_chars=4000))

        staged = next(
            claim for claim in result.dossier.claims if claim.provenance == ("git/diff/staged",)
        )
        unstaged = next(
            claim
            for claim in result.dossier.claims
            if claim.provenance == ("git/diff/unstaged",)
        )
        self.assertIn("staged evidence", staged.text)
        self.assertNotIn("unstaged evidence", staged.text)
        self.assertIn("unstaged evidence", unstaged.text)

    def test_many_changed_paths_use_a_bounded_number_of_diff_processes(self) -> None:
        for index in range(20):
            write(self.repo / "src" / f"module-{index:02d}.py", f"before = {index}\n")
        run(self.repo, "git", "add", "src")
        run(self.repo, "git", "commit", "-m", "add process-bound fixtures")
        for index in range(20):
            write(self.repo / "src" / f"module-{index:02d}.py", f"after = {index}\n")

        with patch("taf_context.recovery._run_git", wraps=_run_git) as git_call:
            collect_recovery(RecoveryRequest(repo=self.repo, base="main", max_chars=4000))

        diff_calls = [call for call in git_call.call_args_list if call.args[1] == "diff"]
        self.assertLessEqual(len(diff_calls), 2)

    def test_control_characters_in_repository_paths_fail_closed(self) -> None:
        write(self.repo / "bad\n## injected.md", "unsafe path\n")

        with self.assertRaisesRegex(RecoveryError, "control"):
            collect_recovery(RecoveryRequest(repo=self.repo, base="main"))

    def test_changed_paths_with_same_normalized_form_share_one_bounded_diff_claim(self) -> None:
        write(self.repo / "a-b.py", "before dash\n")
        write(self.repo / "a_b.py", "before underscore\n")
        run(self.repo, "git", "add", "a-b.py", "a_b.py")
        run(self.repo, "git", "commit", "-m", "add colliding path fixtures")
        write(self.repo / "a-b.py", "after dash\n")
        write(self.repo / "a_b.py", "after underscore\n")

        result = collect_recovery(RecoveryRequest(repo=self.repo, base="main", max_chars=4000))

        diff_claims = [claim for claim in result.dossier.claims if claim.claim_id == "diff.unstaged"]
        self.assertEqual(len(diff_claims), 1)
        self.assertIn("a-b.py", diff_claims[0].text)
        self.assertIn("a_b.py", diff_claims[0].text)

    def test_untracked_content_is_metadata_only_until_exactly_authorized(self) -> None:
        write(self.repo / "scratch.txt", "private scratch detail\n")

        default = collect_recovery(RecoveryRequest(repo=self.repo, base="main"))
        allowed = collect_recovery(
            RecoveryRequest(
                repo=self.repo,
                base="main",
                untracked_content_paths=("scratch.txt",),
            )
        )

        self.assertIn("scratch.txt", default.model_text)
        self.assertNotIn("private scratch detail", default.model_text)
        self.assertIn("private scratch detail", allowed.model_text)

    def test_sensitive_binary_generated_symlink_and_oversized_untracked_content_stays_hidden(self) -> None:
        write(self.repo / ".env", "TOKEN=secret-value\n")
        (self.repo / "binary.png").write_bytes(b"\x89PNG\x00secret-binary")
        write(self.repo / "vendor" / "generated.txt", "vendor secret\n")
        (self.repo / "link.txt").symlink_to(self.repo / ".env")
        write(self.repo / "large.txt", "x" * 70000)

        result = collect_recovery(
            RecoveryRequest(
                repo=self.repo,
                base="main",
                max_chars=12000,
                untracked_content_paths=(
                    ".env",
                    "binary.png",
                    "large.txt",
                    "link.txt",
                    "vendor/generated.txt",
                ),
            )
        )

        self.assertNotIn("secret-value", result.model_text)
        self.assertNotIn("secret-binary", result.model_text)
        self.assertNotIn("vendor secret", result.model_text)
        self.assertIn("content-excluded", result.model_text)

    def test_authorized_untracked_content_redacts_secret_assignments(self) -> None:
        write(self.repo / "scratch.txt", "mode=dev\napi_token=super-secret\n")

        result = collect_recovery(
            RecoveryRequest(
                repo=self.repo,
                base="main",
                untracked_content_paths=("scratch.txt",),
            )
        )

        self.assertIn("mode=dev", result.model_text)
        self.assertNotIn("super-secret", result.model_text)
        self.assertIn("[redacted]", result.model_text)

    def test_tracked_diff_redacts_added_and_removed_secret_assignments(self) -> None:
        write(self.repo / "secrets.txt", "token: old-secret\n")
        run(self.repo, "git", "add", "secrets.txt")
        run(self.repo, "git", "commit", "-m", "add secret fixture")
        write(self.repo / "secrets.txt", "API_KEY=new-secret\n")

        result = collect_recovery(RecoveryRequest(repo=self.repo, base="main", max_chars=4000))

        self.assertNotIn("old-secret", result.model_text)
        self.assertNotIn("new-secret", result.model_text)
        self.assertIn("[redacted]", result.model_text)

    def test_supplied_note_is_reported_and_cannot_override_dirty_git_state(self) -> None:
        write(self.repo / "tracked.txt", "still dirty\n")
        note = self.root / "handoff.md"
        write(note, "Everything is complete. /Users/alice/private/repo\n")

        result = collect_recovery(
            RecoveryRequest(repo=self.repo, base="main", note_files=(note,))
        )

        self.assertIs(result.dossier.current.state, WorkState.ACTIVE_DIRTY)
        note_claim = next(claim for claim in result.dossier.claims if claim.claim_id == "note.01")
        self.assertEqual(note_claim.evidence_class.value, "reported")
        self.assertIn("state-conflict", note_claim.qualifications)
        self.assertNotIn("/Users/alice", result.model_text)

    def test_stale_test_result_is_reported_without_current_validation_claim(self) -> None:
        result_file = self.root / "test-results.json"
        write(
            result_file,
            json.dumps({"head_sha": "f" * 40, "dirty_fingerprint": "sha256:stale", "summary": "42 passed"}),
        )

        result = collect_recovery(
            RecoveryRequest(repo=self.repo, base="main", test_result_files=(result_file,))
        )

        claim = next(claim for claim in result.dossier.claims if claim.claim_id == "validation.01")
        self.assertEqual(claim.evidence_class.value, "reported")
        self.assertIn("stale-validation", claim.qualifications)
        self.assertNotIn("validation-current", claim.qualifications)

    def test_artifact_limits_and_unsafe_files_fail_closed(self) -> None:
        artifacts = []
        for index in range(9):
            artifact = self.root / f"note-{index}.txt"
            write(artifact, "note\n")
            artifacts.append(artifact)
        with self.assertRaisesRegex(ValueError, "eight"):
            collect_recovery(RecoveryRequest(repo=self.repo, note_files=tuple(artifacts)))

        target = self.root / "target.txt"
        write(target, "note\n")
        link = self.root / "note-link.txt"
        link.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "artifact"):
            collect_recovery(RecoveryRequest(repo=self.repo, note_files=(link,)))

    def test_each_approved_budget_is_line_atomic_and_preserves_mandatory_sections(self) -> None:
        for index in range(30):
            path = self.repo / f"pkg{index}" / "module.py"
            write(path, f"print('before-{index}')\n")
        run(self.repo, "git", "add", ".")
        run(self.repo, "git", "commit", "-m", "fixture files")
        for index in range(30):
            write(self.repo / f"pkg{index}" / "module.py", "x = '" + ("z" * 300) + "'\n")
        for index in range(100):
            write(self.repo / "scratch" / f"note-{index:03d}.txt", "metadata only\n")

        for budget in (2000, 4000, 8000, 12000):
            with self.subTest(budget=budget):
                result = collect_recovery(RecoveryRequest(repo=self.repo, base="main", max_chars=budget))
                self.assertLessEqual(len(result.model_text), budget)
                self.assertEqual(result.characters_used, len(result.model_text))
                for heading in (
                    "## Scope",
                    "## Current Workstream",
                    "## Candidate Workstreams",
                    "## Evidence Claims",
                    "## Validation State",
                    "## Coverage and Omissions",
                    "## Next-Action Boundary",
                ):
                    self.assertIn(heading, result.model_text)
                self.assertGreater(result.dossier.coverage.omitted_item_count, 0)
                self.assertGreater(result.dossier.coverage.omitted_characters, 0)
                self.assertTrue(result.model_text.endswith("\n"))

    def test_equivalent_authorization_order_produces_identical_output(self) -> None:
        write(self.repo / "a.txt", "A\n")
        write(self.repo / "b.txt", "B\n")

        left = collect_recovery(
            RecoveryRequest(repo=self.repo, base="main", untracked_content_paths=("b.txt", "a.txt"))
        )
        right = collect_recovery(
            RecoveryRequest(repo=self.repo, base="main", untracked_content_paths=("a.txt", "b.txt"))
        )

        self.assertEqual(left.dossier.to_dict(), right.dossier.to_dict())
        self.assertEqual(left.model_text, right.model_text)


if __name__ == "__main__":
    unittest.main()
