"""Behavioral tests for bounded Level 0 Git repository snapshots."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from taf_context import git_snapshot as gs
from taf_context.git_snapshot import (
    SnapshotError,
    _content_descriptor,
    _index_gitlinks,
    collect_snapshot,
    manifest_from_snapshot,
)
from taf_context.models import BackgroundState
from tests.taf_context.repo_factory import (
    commit_all,
    init_committed_repo,
    init_repo,
    run,
    write,
)


class RepositoryIdentityTests(unittest.TestCase):
    def test_repository_root_preserves_path_whitespace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = init_committed_repo(Path(directory) / " repository ")

            try:
                snapshot = collect_snapshot(repo)
            except SnapshotError:
                self.fail("valid repository-root whitespace must be preserved")

            self.assertEqual(snapshot.canonical_root, str(repo.resolve()))
            self.assertEqual(snapshot.tracked_paths, ("tracked.txt",))

    def test_subdirectory_collection_keeps_repository_relative_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = init_committed_repo(Path(directory) / "repo")
            write(repo / "nested/deep/module.py", "print('nested')\n")
            commit_all(repo, "add nested fixture")

            from_root = collect_snapshot(repo)
            from_nested = collect_snapshot(repo / "nested/deep")

            self.assertEqual(from_root, from_nested)

    def test_linked_worktrees_share_repository_but_not_worktree_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = init_committed_repo(root / "primary")
            linked = root / "linked"
            run(primary, "git", "worktree", "add", "-b", "linked", str(linked), "HEAD")

            primary_snapshot = collect_snapshot(primary)
            linked_snapshot = collect_snapshot(linked)

            self.assertEqual(
                primary_snapshot.repository_identity,
                linked_snapshot.repository_identity,
            )
            self.assertEqual(
                primary_snapshot.git_common_dir_fingerprint,
                linked_snapshot.git_common_dir_fingerprint,
            )
            self.assertNotEqual(
                primary_snapshot.worktree_identity,
                linked_snapshot.worktree_identity,
            )
            self.assertEqual(primary_snapshot.head_sha, linked_snapshot.head_sha)

    def test_moving_clone_changes_only_local_root_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = init_committed_repo(root / "source")
            original = root / "clone-original"
            moved = root / "clone-moved"
            run(root, "git", "clone", "--quiet", str(source), str(original))
            before = collect_snapshot(original)

            original.rename(moved)
            after = collect_snapshot(moved)

            self.assertEqual(before.repository_identity, after.repository_identity)
            self.assertNotEqual(
                before.canonical_root_fingerprint,
                after.canonical_root_fingerprint,
            )

    def test_clean_worktrees_share_dirty_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = init_committed_repo(root / "source")
            clone = root / "clone"
            run(root, "git", "clone", "--quiet", str(source), str(clone))

            self.assertEqual(
                collect_snapshot(source).dirty_fingerprint,
                collect_snapshot(clone).dirty_fingerprint,
            )


class DirtyOverlayTests(unittest.TestCase):
    def test_rename_discovery_disables_similarity_and_never_reads_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = init_repo(Path(directory) / "repo")
            source = repo / "source.txt"
            source.write_bytes(b"sensitive oversized payload")
            commit_all(repo, "track source")
            run(repo, "git", "config", "diff.renames", "true")
            run(repo, "git", "config", "status.renames", "true")
            run(repo, "git", "mv", "source.txt", "renamed.txt")
            helper_marker = repo / "external-diff-ran"
            helper = repo / "external-diff"
            write(helper, f"#!/bin/sh\ntouch '{helper_marker}'\n")
            helper.chmod(0o755)
            run(repo, "git", "config", "diff.external", str(helper))
            opened_payloads: list[object] = []
            real_open = os.open
            real_run = subprocess.run
            discovery_commands: list[tuple[str, ...]] = []

            def recording_open(
                path: object, flags: int, *args: object, **kwargs: object
            ) -> int:
                if path in {"source.txt", "renamed.txt"}:
                    opened_payloads.append(path)
                return real_open(path, flags, *args, **kwargs)

            def recording_run(argv: list[str], **kwargs: object):
                command = tuple(argv[1:])
                if command and command[0] in {"diff", "status"}:
                    discovery_commands.append(command)
                return real_run(argv, **kwargs)

            with mock.patch(
                "taf_context.git_snapshot.os.open", side_effect=recording_open
            ), mock.patch(
                "taf_context.git_snapshot.subprocess.run", side_effect=recording_run
            ):
                snapshot = collect_snapshot(repo, max_dirty_file_bytes=8)

            self.assertFalse(helper_marker.exists())
            self.assertEqual(opened_payloads, [])
            self.assertTrue(discovery_commands)
            self.assertTrue(
                all("--no-renames" in command for command in discovery_commands)
            )
            self.assertEqual(
                set(snapshot.staged_paths), {"source.txt", "renamed.txt"}
            )
            self.assertIn("dirty-diff-statistics-incomplete", snapshot.warnings)

    def test_excluded_dirty_content_uses_metadata_only_and_warns(self) -> None:
        fixtures = {
            "vendor/library.py": b"vendored secret",
            "build/output.js": b"generated secret",
            ".cache/state.json": b"cache secret",
            ".env": b"credential secret",
            "keys/deploy.pem": b"private key secret",
            "assets/logo.png": b"\x89PNG binary secret",
            "data/cache.sqlite": b"SQLite format 3\x00secret",
        }
        with tempfile.TemporaryDirectory() as directory:
            repo = init_committed_repo(Path(directory) / "repo")
            for relative, content in fixtures.items():
                path = repo / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)

            opened_excluded: list[Path] = []
            real_open = os.open

            def recording_open(
                path: object, flags: int, *args: object, **kwargs: object
            ) -> int:
                candidate = (
                    Path(os.fsdecode(path))
                    if isinstance(path, (str, bytes, os.PathLike))
                    else None
                )
                excluded_names = {
                    "library.py", "output.js", "state.json", ".env",
                    "deploy.pem", "logo.png", "cache.sqlite",
                }
                if candidate is not None and candidate.name in excluded_names:
                    opened_excluded.append(candidate)
                return real_open(path, flags, *args, **kwargs)

            with mock.patch(
                "taf_context.git_snapshot.os.open", side_effect=recording_open
            ):
                snapshot = collect_snapshot(repo)

            self.assertFalse(snapshot.dirty_fingerprint_complete)
            self.assertEqual(snapshot.dirty_bytes_hashed, 0)
            self.assertEqual(snapshot.binary_file_count, 2)
            self.assertEqual(opened_excluded, [])
            self.assertIn("dirty-content-excluded", snapshot.warnings)
            self.assertIn("dirty-credential-content-excluded", snapshot.warnings)
            self.assertIn("dirty-generated-or-vendored-content-excluded", snapshot.warnings)
            self.assertIn("dirty-binary-content-excluded", snapshot.warnings)

    def test_dirty_reader_is_ceiling_bounded_and_detects_growth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = init_committed_repo(Path(directory) / "repo")
            write(repo / "tracked.txt", "12345678")
            real_read = os.read
            requested: list[int] = []
            returned: list[int] = []

            def grow_after_first_read(descriptor: int, amount: int) -> bytes:
                requested.append(amount)
                value = real_read(descriptor, amount)
                returned.append(len(value))
                if len(requested) == 1:
                    with (repo / "tracked.txt").open("ab") as target:
                        target.write(b"growth")
                return value

            with mock.patch(
                "taf_context.git_snapshot.os.read", side_effect=grow_after_first_read
            ):
                descriptor = _content_descriptor(repo, "tracked.txt", 64)

            self.assertTrue(all(amount <= 64 for amount in requested))
            self.assertLessEqual(sum(returned), 64)
            self.assertFalse(descriptor[3])
            self.assertEqual(descriptor[4], "dirty-file-changed-during-read")

    def test_dirty_reader_returns_at_most_the_exact_configured_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = init_committed_repo(Path(directory) / "repo")
            write(repo / "tracked.txt", "12345678")
            real_read = os.read
            requested: list[int] = []
            returned: list[int] = []

            def recording_read(descriptor: int, amount: int) -> bytes:
                requested.append(amount)
                value = real_read(descriptor, amount)
                returned.append(len(value))
                return value

            with mock.patch(
                "taf_context.git_snapshot.os.read", side_effect=recording_read
            ):
                descriptor = _content_descriptor(repo, "tracked.txt", 8)

            self.assertEqual(requested, [8])
            self.assertEqual(returned, [8])
            self.assertEqual(descriptor[1], 8)
            self.assertTrue(descriptor[3])

    def test_symlink_replacement_never_reads_outside_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            outside = root / "outside.txt"
            outside.write_text("outside secret", encoding="utf-8")
            write(repo / "tracked.txt", "dirty")
            real_open = os.open
            replaced = False

            def replace_before_leaf_open(
                path: object, flags: int, *args: object, **kwargs: object
            ) -> int:
                nonlocal replaced
                if path == "tracked.txt" and kwargs.get("dir_fd") is not None and not replaced:
                    replaced = True
                    (repo / "tracked.txt").unlink()
                    (repo / "tracked.txt").symlink_to(outside)
                return real_open(path, flags, *args, **kwargs)

            with mock.patch(
                "taf_context.git_snapshot.os.open", side_effect=replace_before_leaf_open
            ):
                descriptor = _content_descriptor(repo, "tracked.txt", 1024)

            self.assertEqual(descriptor[1], 0)
            self.assertFalse(descriptor[3])
            self.assertEqual(descriptor[4], "dirty-path-unsafe")

    def test_every_dirty_source_and_path_kind_changes_fingerprint_deterministically(
        self,
    ) -> None:
        mutations = {
            "staged": self._stage_change,
            "unstaged": self._unstaged_change,
            "untracked": self._untracked_change,
            "deleted": self._delete_change,
            "renamed": self._rename_change,
            "unicode": self._unicode_change,
            "spaces": self._spaces_change,
            "symlink": self._symlink_change,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, mutate in mutations.items():
                with self.subTest(name=name):
                    repo = init_committed_repo(root / name)
                    clean = collect_snapshot(repo).dirty_fingerprint
                    expected_path = mutate(repo)

                    first = collect_snapshot(repo)
                    second = collect_snapshot(repo)

                    self.assertNotEqual(clean, first.dirty_fingerprint)
                    self.assertEqual(first.dirty_fingerprint, second.dirty_fingerprint)
                    self.assertIn(
                        expected_path,
                        first.staged_paths
                        + first.unstaged_paths
                        + first.untracked_paths,
                    )

    def test_ignored_files_do_not_enter_dirty_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = init_committed_repo(Path(directory) / "repo")
            write(repo / ".gitignore", "ignored/**\n")
            commit_all(repo, "ignore fixture")
            clean = collect_snapshot(repo)
            write(repo / "ignored/private.txt", "must stay ignored\n")

            dirty = collect_snapshot(repo)

            self.assertEqual(clean.dirty_fingerprint, dirty.dirty_fingerprint)
            self.assertFalse(
                any(path.startswith("ignored/") for path in dirty.untracked_paths)
            )
            self.assertGreaterEqual(dirty.ignored_entry_count, 1)

    def test_status_flags_distinguish_same_content_staged_and_unstaged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = init_committed_repo(root / "source")
            staged = root / "staged"
            unstaged = root / "unstaged"
            run(root, "git", "clone", "--quiet", str(source), str(staged))
            run(root, "git", "clone", "--quiet", str(source), str(unstaged))
            write(staged / "tracked.txt", "same dirty content\n")
            write(unstaged / "tracked.txt", "same dirty content\n")
            run(staged, "git", "add", "tracked.txt")

            staged_snapshot = collect_snapshot(staged)
            unstaged_snapshot = collect_snapshot(unstaged)

            self.assertEqual(
                staged_snapshot.repository_identity,
                unstaged_snapshot.repository_identity,
            )
            self.assertNotEqual(
                staged_snapshot.dirty_fingerprint,
                unstaged_snapshot.dirty_fingerprint,
            )

    def test_tracked_dirty_state_never_invokes_worktree_numstat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = init_committed_repo(Path(directory) / "repo")
            write(repo / "tracked.txt", "staged\n")
            run(repo, "git", "add", "tracked.txt")
            write(repo / "tracked.txt", "staged\nunstaged\n")
            real_run = subprocess.run
            numstat_invocations: list[tuple[str, ...]] = []

            def recording_run(argv: list[str], **kwargs: object):
                command = tuple(argv[1:])
                if "--numstat" in command:
                    numstat_invocations.append(command)
                return real_run(argv, **kwargs)

            with mock.patch(
                "taf_context.git_snapshot.subprocess.run", side_effect=recording_run
            ):
                snapshot = collect_snapshot(repo)

            self.assertEqual(numstat_invocations, [])
            self.assertEqual((snapshot.insertions, snapshot.deletions), (0, 0))
            self.assertIn("dirty-diff-statistics-incomplete", snapshot.warnings)

    def test_numstat_is_skipped_for_ineligible_tracked_dirty_content(self) -> None:
        fixtures = {
            "large.txt": b"123456789",
            "assets/logo.png": b"\x89PNG dirty binary",
            ".env": b"dirty credential",
            "generated/output.txt": b"dirty generated output",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, (relative, content) in enumerate(fixtures.items()):
                with self.subTest(relative=relative):
                    repo = init_committed_repo(root / f"repo-{index}")
                    path = repo / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"initial")
                    commit_all(repo, "track protected fixture")
                    path.write_bytes(content)
                    real_run = subprocess.run
                    numstat_invocations: list[tuple[str, ...]] = []

                    def recording_run(argv: list[str], **kwargs: object):
                        command = tuple(argv[1:])
                        if "--numstat" in command:
                            numstat_invocations.append(command)
                        return real_run(argv, **kwargs)

                    with mock.patch(
                        "taf_context.git_snapshot.subprocess.run",
                        side_effect=recording_run,
                    ):
                        snapshot = collect_snapshot(
                            repo, max_dirty_file_bytes=8
                        )

                    self.assertEqual(numstat_invocations, [])
                    self.assertEqual((snapshot.insertions, snapshot.deletions), (0, 0))
                    self.assertIn(
                        "dirty-diff-statistics-incomplete", snapshot.warnings
                    )

    @staticmethod
    def _stage_change(repo: Path) -> str:
        write(repo / "tracked.txt", "staged\n")
        run(repo, "git", "add", "tracked.txt")
        return "tracked.txt"

    @staticmethod
    def _unstaged_change(repo: Path) -> str:
        write(repo / "tracked.txt", "unstaged\n")
        return "tracked.txt"

    @staticmethod
    def _untracked_change(repo: Path) -> str:
        write(repo / "new.txt", "untracked\n")
        return "new.txt"

    @staticmethod
    def _delete_change(repo: Path) -> str:
        (repo / "tracked.txt").unlink()
        return "tracked.txt"

    @staticmethod
    def _rename_change(repo: Path) -> str:
        run(repo, "git", "mv", "tracked.txt", "renamed file.txt")
        return "renamed file.txt"

    @staticmethod
    def _unicode_change(repo: Path) -> str:
        write(repo / "İstanbul-ç.txt", "unicode\n")
        return "İstanbul-ç.txt"

    @staticmethod
    def _spaces_change(repo: Path) -> str:
        write(repo / "path with spaces.txt", "spaces\n")
        return "path with spaces.txt"

    @staticmethod
    def _symlink_change(repo: Path) -> str:
        (repo / "linked.txt").symlink_to("tracked.txt")
        return "linked.txt"


class InventoryAndManifestTests(unittest.TestCase):
    def test_inventory_uses_only_bounded_path_metadata_rules(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = init_repo(Path(directory) / "repo")
            fixtures = {
                "AGENTS.md": "agents\n",
                "CLAUDE.md": "claude\n",
                "README.rst": "readme\n",
                "docs/superpowers/specs/design.md": "spec\n",
                "docs/superpowers/plans/plan.md": "plan\n",
                "reports/test-results.json": "{}\n",
                "reports/benchmark-result.md": "bench\n",
                ".taf/context/registration.json": "{}\n",
                ".taf/context/manifest.json": "{}\n",
                "src/app.py": "print('ok')\n",
                "ui/main.ts": "export {}\n",
                "vendor/lib.py": "print('generated')\n",
                "unknown.datax": "unknown\n",
            }
            for relative, content in fixtures.items():
                write(repo / relative, content)
            commit_all(repo)
            write(repo / "new.py", "print('new')\n")

            snapshot = collect_snapshot(repo)

            self.assertEqual(snapshot.tracked_paths, tuple(sorted(fixtures)))
            self.assertEqual(snapshot.untracked_paths, ("new.py",))
            self.assertEqual(
                snapshot.candidate_artifacts,
                (
                    "AGENTS.md",
                    "CLAUDE.md",
                    "README.rst",
                    "docs/superpowers/plans/plan.md",
                    "docs/superpowers/specs/design.md",
                    "reports/benchmark-result.md",
                    "reports/test-results.json",
                ),
            )
            self.assertEqual(
                snapshot.provider_markers,
                (
                    ".taf/context/manifest.json",
                    ".taf/context/registration.json",
                ),
            )
            self.assertEqual(snapshot.generated_or_vendored_count, 1)
            languages = dict(snapshot.language_counts)
            self.assertEqual(languages["Python"], 3)
            self.assertEqual(languages["TypeScript"], 1)
            self.assertEqual(languages["Other"], 1)

    def test_manifest_carries_snapshot_identity_and_level_zero_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = init_committed_repo(Path(directory) / "repo")
            write(repo / "new.py", "print('new')\n")
            snapshot = collect_snapshot(repo)

            first = manifest_from_snapshot(
                snapshot, "2026-08-25T00:00:00Z", storage_bytes=123
            )
            second = manifest_from_snapshot(
                snapshot, "2026-08-25T00:00:00Z", storage_bytes=123
            )

            self.assertEqual(first, second)
            self.assertEqual(first.repository_identity, snapshot.repository_identity)
            self.assertEqual(first.provider_name, "taf-context")
            self.assertEqual(first.provider_version, "0.5.0")
            self.assertEqual(first.provider_schema_version, "1")
            self.assertEqual(first.index_levels, ("level0",))
            self.assertEqual(first.capabilities, ("repository-map", "status"))
            self.assertEqual(first.path_coverage, 1.0)
            self.assertEqual(first.tracked_file_count, 1)
            self.assertEqual(first.indexed_file_count, 2)
            self.assertEqual(first.skipped_file_count, 0)
            self.assertEqual(first.storage_bytes, 123)
            self.assertIs(first.background_state, BackgroundState.READY)
            self.assertEqual(first.created_at, first.updated_at)
            self.assertTrue(first.provider_index_id.startswith("sha256:"))


class BoundedGitTests(unittest.TestCase):
    def test_submodule_local_helpers_are_not_invoked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = init_committed_repo(root / "child")
            repo = init_committed_repo(root / "repo")
            run(
                repo,
                "git",
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "add",
                "--quiet",
                str(child),
                "modules/child",
            )
            commit_all(repo, "add submodule")
            marker = repo / "submodule-helper-ran"
            helper = repo / "submodule-fsmonitor"
            write(helper, f"#!/bin/sh\ntouch '{marker}'\nprintf '0\\n'\n")
            helper.chmod(0o755)
            submodule = repo / "modules/child"
            run(submodule, "git", "config", "core.fsmonitor", str(helper))
            write(submodule / "tracked.txt", "dirty submodule\n")

            snapshot = collect_snapshot(repo)

            self.assertFalse(marker.exists())
            self.assertEqual(
                snapshot.tracked_paths,
                (".gitmodules", "modules/child", "tracked.txt"),
            )
            self.assertFalse(snapshot.dirty_fingerprint_complete)
            self.assertIn(
                "submodule-worktree-state-uninspected", snapshot.warnings
            )

    def test_staged_gitlink_sha_change_is_fingerprinted_without_nested_helper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = init_committed_repo(root / "child")
            first_sha = run(child, "git", "rev-parse", "HEAD").strip()
            write(child / "tracked.txt", "second child revision\n")
            commit_all(child, "second child revision")
            second_sha = run(child, "git", "rev-parse", "HEAD").strip()
            run(child, "git", "checkout", "--quiet", first_sha)

            repo = init_committed_repo(root / "repo")
            run(
                repo,
                "git",
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "add",
                "--quiet",
                str(child),
                "modules/child",
            )
            commit_all(repo, "add submodule")
            baseline = collect_snapshot(repo)

            submodule = repo / "modules/child"
            marker = repo / "nested-helper-ran"
            helper = repo / "nested-fsmonitor"
            write(helper, f"#!/bin/sh\ntouch '{marker}'\nprintf '0\\n'\n")
            helper.chmod(0o755)
            run(submodule, "git", "config", "core.fsmonitor", str(helper))
            run(submodule, "git", "checkout", "--quiet", second_sha)
            run(repo, "git", "add", "modules/child")
            marker.unlink(missing_ok=True)

            changed = collect_snapshot(repo)

            self.assertFalse(marker.exists())
            self.assertIn("modules/child", changed.staged_paths)
            self.assertNotEqual(baseline.dirty_fingerprint, changed.dirty_fingerprint)
            self.assertIn("dirty-diff-statistics-incomplete", changed.warnings)

    def test_git_boundary_disables_optional_locks_and_configured_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = init_committed_repo(Path(directory) / "repo")
            marker = repo / "helper-ran"
            helper = repo / "fsmonitor-helper"
            write(helper, f"#!/bin/sh\ntouch '{marker}'\nprintf '0\\n'\n")
            helper.chmod(0o755)
            run(repo, "git", "config", "core.fsmonitor", str(helper))
            index = Path(run(repo, "git", "rev-parse", "--git-path", "index"))
            if not index.is_absolute():
                index = repo / index
            before = index.stat().st_mtime_ns

            real_run = subprocess.run
            environments: list[dict[str, str]] = []

            def recording_run(argv: list[str], **kwargs: object):
                environments.append(dict(kwargs.get("env", {})))
                return real_run(argv, **kwargs)

            with mock.patch(
                "taf_context.git_snapshot.subprocess.run", side_effect=recording_run
            ):
                collect_snapshot(repo)

            self.assertFalse(marker.exists())
            self.assertEqual(index.stat().st_mtime_ns, before)
            self.assertTrue(environments)
            self.assertTrue(all(env.get("GIT_OPTIONAL_LOCKS") == "0" for env in environments))
            self.assertTrue(all(env.get("GIT_CONFIG_NOSYSTEM") == "1" for env in environments))
            self.assertTrue(
                all(
                    env.get("GIT_CONFIG_KEY_5") == "diff.renames"
                    and env.get("GIT_CONFIG_VALUE_5") == "false"
                    and env.get("GIT_CONFIG_KEY_6") == "status.renames"
                    and env.get("GIT_CONFIG_VALUE_6") == "false"
                    for env in environments
                )
            )

    def test_every_git_command_uses_the_twenty_second_timeout(self) -> None:
        real_run = subprocess.run
        observed_timeouts: list[object] = []

        def recording_run(
            argv: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            observed_timeouts.append(kwargs.get("timeout"))
            return real_run(argv, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            repo = init_committed_repo(Path(directory) / "repo")
            with mock.patch(
                "taf_context.git_snapshot.subprocess.run", side_effect=recording_run
            ):
                collect_snapshot(repo)

        self.assertTrue(observed_timeouts)
        self.assertEqual(set(observed_timeouts), {20})

    def test_collector_uses_only_local_allowlisted_git_commands(self) -> None:
        allowed = {
            ("rev-parse", "--show-toplevel"),
            (
                "config", "--local", "--includes", "--name-only", "--get-regexp",
                r"^filter\..*\.(clean|smudge|process)$",
            ),
            ("rev-parse", "--absolute-git-dir", "--git-common-dir", "--verify", "HEAD"),
            ("rev-parse", "--absolute-git-dir", "--git-common-dir"),
            ("symbolic-ref", "--short", "-q", "HEAD"),
            ("rev-list", "--max-parents=0", "HEAD"),
            ("ls-files", "-z"),
            ("ls-files", "--stage", "-z"),
            ("diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--ignore-submodules=dirty", "--cached", "--name-only", "-z"),
            ("diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--ignore-submodules=all", "--name-only", "-z"),
            ("ls-files", "--others", "--exclude-standard", "-z"),
            (
                "status",
                "--no-renames",
                "--ignore-submodules=all",
                "--porcelain=v1",
                "-z",
                "--ignored=matching",
                "--untracked-files=normal",
            ),
        }
        real_run = subprocess.run
        invocations: list[tuple[str, ...]] = []

        def recording_run(
            argv: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            invocations.append(tuple(argv[1:]))
            return real_run(argv, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            repo = init_committed_repo(Path(directory) / "repo")
            with mock.patch(
                "taf_context.git_snapshot.subprocess.run", side_effect=recording_run
            ):
                collect_snapshot(repo)

        self.assertTrue(invocations)
        self.assertTrue(set(invocations) <= allowed)
        self.assertFalse(
            any(command[0] in {"fetch", "remote"} for command in invocations)
        )


    def test_rejects_non_git_input_and_invalid_byte_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(SnapshotError):
                collect_snapshot(root)

            repo = init_repo(root / "repo")
            with self.assertRaises(SnapshotError):
                collect_snapshot(repo, max_dirty_file_bytes=-1)

    def test_rejects_non_integer_byte_ceiling_and_git_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = init_repo(Path(directory) / "repo")
            try:
                collect_snapshot(
                    repo, max_dirty_file_bytes="8"  # type: ignore[arg-type]
                )
            except TypeError:
                self.fail("invalid byte ceilings must raise SnapshotError")
            except SnapshotError:
                pass

            with mock.patch(
                "taf_context.git_snapshot.subprocess.run",
                side_effect=subprocess.TimeoutExpired(["git"], 10),
            ):
                with self.assertRaises(SnapshotError):
                    collect_snapshot(repo)

    def test_rev_parse_fallback_recovers_head_when_combined_call_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = init_committed_repo(Path(directory) / "repo")
            expected_head = run(repo, "git", "rev-parse", "HEAD")
            real_run = subprocess.run
            combined_rejected = (
                "rev-parse", "--absolute-git-dir", "--git-common-dir", "--verify", "HEAD",
            )

            def recording_run(argv: list[str], **kwargs: object):
                if tuple(argv[1:]) == combined_rejected:
                    return subprocess.CompletedProcess(
                        argv, returncode=128, stdout=b"", stderr=b"fatal: simulated rejection\n"
                    )
                return real_run(argv, **kwargs)

            with mock.patch(
                "taf_context.git_snapshot.subprocess.run", side_effect=recording_run
            ):
                snapshot = collect_snapshot(repo)

            self.assertEqual(snapshot.head_sha, expected_head)
            self.assertNotEqual(snapshot.repository_identity, gs._fingerprint([]))

    def test_rejects_malformed_git_output(self) -> None:
        malformed = subprocess.CompletedProcess(
            ["git", "rev-parse", "--show-toplevel"],
            returncode=0,
            stdout=b"invalid\x00root\n",
            stderr=b"",
        )
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch(
                "taf_context.git_snapshot.subprocess.run", return_value=malformed
            ):
                with self.assertRaises(SnapshotError):
                    collect_snapshot(Path(directory))


class IndexEntryParserTests(unittest.TestCase):
    OID = b"1" * 40

    def test_every_index_field_is_validated_before_gitlink_classification(self) -> None:
        malformed = {
            "mode syntax": b"10064 " + self.OID + b" 0\tfile.txt\x00",
            "unsupported mode": b"100600 " + self.OID + b" 0\tfile.txt\x00",
            "object id": b"100644 " + (b"1" * 39) + b" 0\tfile.txt\x00",
            "stage": b"100644 " + self.OID + b" 4\tfile.txt\x00",
            "path": b"100644 " + self.OID + b" 0\t../escape.txt\x00",
        }
        for field, raw in malformed.items():
            with self.subTest(field=field):
                with self.assertRaises(SnapshotError):
                    _index_gitlinks(raw)

    def test_gitlink_conflict_stages_are_retained_deterministically(self) -> None:
        raw = b"".join(
            b"160000 " + bytes(str(stage), "ascii") * 40 + b" "
            + bytes(str(stage), "ascii") + b"\tmodules/child\x00"
            for stage in (3, 1, 2)
        )
        regular = b"100644 " + self.OID + b" 0\tregular.txt\x00"

        parsed = _index_gitlinks(regular + raw)

        self.assertEqual(
            parsed,
            {
                "modules/child": (
                    "160000:" + "1" * 40 + ":1",
                    "160000:" + "2" * 40 + ":2",
                    "160000:" + "3" * 40 + ":3",
                )
            },
        )


class DeferredEdgeCaseTests(unittest.TestCase):
    def test_oversized_file_uses_metadata_without_reading_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = init_committed_repo(Path(directory) / "repo")
            large_file = repo / "large.bin"
            large_file.write_bytes(b"0123456789abcdef")
            real_open = Path.open
            opened_large_file: list[Path] = []

            def recording_open(path: Path, *args: object, **kwargs: object):
                if path.resolve() == large_file.resolve():
                    opened_large_file.append(path)
                return real_open(path, *args, **kwargs)

            with mock.patch.object(Path, "open", recording_open):
                snapshot = collect_snapshot(repo, max_dirty_file_bytes=8)

            self.assertEqual(opened_large_file, [])
            self.assertFalse(snapshot.dirty_fingerprint_complete)
            self.assertEqual(snapshot.oversized_file_count, 1)
            self.assertEqual(snapshot.dirty_bytes_hashed, 0)
            self.assertTrue(snapshot.warnings)
            manifest = manifest_from_snapshot(snapshot, "2026-08-25T00:00:00Z")
            self.assertIn("dirty-fingerprint-incomplete", manifest.warnings)

    def test_binary_dirty_file_is_counted_without_reading_or_exposing_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = init_committed_repo(Path(directory) / "repo")
            secret = b"binary-secret\x00payload"
            (repo / "binary.dat").write_bytes(secret)

            snapshot = collect_snapshot(repo)

            self.assertEqual(snapshot.binary_file_count, 1)
            self.assertNotIn("binary-secret", repr(snapshot))
            self.assertEqual(snapshot.dirty_bytes_hashed, 0)
            self.assertFalse(snapshot.dirty_fingerprint_complete)
            self.assertIn("dirty-binary-content-excluded", snapshot.warnings)

    def test_unborn_repository_has_deterministic_empty_head_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = init_repo(Path(directory) / "repo")
            try:
                first = collect_snapshot(repo)
                second = collect_snapshot(repo)
            except SnapshotError:
                self.fail("an unborn repository must produce a snapshot")

            self.assertIsNone(first.head_sha)
            self.assertEqual(first.repository_identity, second.repository_identity)
            self.assertEqual(first.dirty_fingerprint, second.dirty_fingerprint)
            self.assertEqual(first.tracked_paths, ())

    def test_unborn_staged_state_warns_that_diff_statistics_are_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = init_repo(Path(directory) / "repo")
            write(repo / "staged.txt", "staged before first commit\n")
            run(repo, "git", "add", "staged.txt")

            snapshot = collect_snapshot(repo)

            self.assertEqual(snapshot.staged_paths, ("staged.txt",))
            self.assertEqual((snapshot.insertions, snapshot.deletions), (0, 0))
            self.assertIn("dirty-diff-statistics-incomplete", snapshot.warnings)


class ConcurrentSchedulingTests(unittest.TestCase):
    """The concurrent collector keeps the sequential error contract."""

    def test_first_failing_git_command_still_raises_snapshot_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = init_committed_repo(Path(directory) / "repo")
            real_run = subprocess.run

            def failing_ls_files(argv: list[str], **kwargs: object):
                if tuple(argv[1:3]) == ("ls-files", "-z"):
                    raise OSError("simulated ls-files failure")
                return real_run(argv, **kwargs)

            with mock.patch("taf_context.git_snapshot.subprocess.run", side_effect=failing_ls_files):
                with self.assertRaises(SnapshotError):
                    collect_snapshot(repo)

    def test_thread_start_failure_falls_back_to_sequential_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = init_committed_repo(Path(directory) / "repo")
            expected = collect_snapshot(repo)

            with mock.patch(
                "taf_context.git_snapshot.threading.Thread.start",
                side_effect=RuntimeError("simulated thread exhaustion"),
            ):
                actual = collect_snapshot(repo)

            self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
