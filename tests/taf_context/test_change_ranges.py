"""Tests for changed line ranges derived from a resolved Git base."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from taf_context import change_ranges
from taf_context.change_ranges import (
    MAXIMUM_CHANGED_PATHS,
    MAXIMUM_RANGES_PER_PATH,
    ChangeBase,
    ChangedPath,
    changed_ranges,
    parse_unified_diff_ranges,
    resolve_change_base,
    staged_change_base,
)
from taf_context.git_snapshot import collect_snapshot
from taf_context.recovery import resolve_recovery_base

from .repo_factory import commit_all, init_repo, run, write


def _numbered(prefix: str, count: int) -> str:
    return "".join(f"{prefix}{index}\n" for index in range(1, count + 1))


def _snapshot_stub(*untracked: str) -> SimpleNamespace:
    return SimpleNamespace(untracked_paths=tuple(untracked))


class ParseUnifiedDiffRangesTests(unittest.TestCase):
    def test_empty_text_has_no_paths(self) -> None:
        self.assertEqual(parse_unified_diff_ranges(""), {})

    def test_modification_hunks_become_new_side_ranges(self) -> None:
        text = (
            "diff --git a/a.py b/a.py\n"
            "index 4f11e05..ea78809 100644\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -3,2 +3,2 @@ a2\n"
            "-a3\n"
            "-a4\n"
            "+A3\n"
            "+A4\n"
            "@@ -9 +9 @@ a8\n"
            "-a9\n"
            "+A9\n"
        )

        self.assertEqual(parse_unified_diff_ranges(text), {"a.py": [(3, 4), (9, 9)]})

    def test_added_file_is_a_whole_file_entry(self) -> None:
        text = (
            "diff --git a/b.py b/b.py\n"
            "new file mode 100644\n"
            "index 0000000..9b89cd5\n"
            "--- /dev/null\n"
            "+++ b/b.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+b1\n"
            "+b2\n"
        )

        self.assertEqual(parse_unified_diff_ranges(text), {"b.py": None})

    def test_added_empty_file_is_a_whole_file_entry(self) -> None:
        text = (
            "diff --git a/empty.py b/empty.py\n"
            "new file mode 100644\n"
            "index 0000000..e69de29\n"
        )

        self.assertEqual(parse_unified_diff_ranges(text), {"empty.py": None})

    def test_binary_change_is_a_whole_file_entry(self) -> None:
        text = (
            "diff --git a/bin.dat b/bin.dat\n"
            "index 96eb299..3983f0a 100644\n"
            "Binary files a/bin.dat and b/bin.dat differ\n"
        )

        self.assertEqual(parse_unified_diff_ranges(text), {"bin.dat": None})

    def test_pure_deletion_uses_the_adjacent_new_side_line(self) -> None:
        text = (
            "diff --git a/c.py b/c.py\n"
            "index 6693908..8600165 100644\n"
            "--- a/c.py\n"
            "+++ b/c.py\n"
            "@@ -6,2 +5,0 @@ c5\n"
            "-c6\n"
            "-c7\n"
        )

        self.assertEqual(parse_unified_diff_ranges(text), {"c.py": [(5, 5)]})

    def test_deleted_file_uses_the_first_line(self) -> None:
        text = (
            "diff --git a/e.py b/e.py\n"
            "deleted file mode 100644\n"
            "index 7e1cd11..0000000\n"
            "--- a/e.py\n"
            "+++ /dev/null\n"
            "@@ -1,3 +0,0 @@\n"
            "-e1\n"
            "-e2\n"
            "-e3\n"
        )

        self.assertEqual(parse_unified_diff_ranges(text), {"e.py": [(1, 1)]})

    def test_adjacent_and_overlapping_ranges_merge_and_sort(self) -> None:
        text = (
            "diff --git a/m.py b/m.py\n"
            "--- a/m.py\n"
            "+++ b/m.py\n"
            "@@ -20 +20,3 @@\n"
            "@@ -5,0 +5,2 @@\n"
            "@@ -8 +7 @@\n"
            "@@ -21,0 +22,4 @@\n"
        )

        self.assertEqual(parse_unified_diff_ranges(text), {"m.py": [(5, 7), (20, 25)]})

    def test_content_lines_are_never_read_as_headers(self) -> None:
        text = (
            "diff --git a/x.py b/x.py\n"
            "--- a/x.py\n"
            "+++ b/x.py\n"
            "@@ -1 +1 @@\n"
            "--- /dev/null\n"
            "+++ b/y.py\n"
            "-Binary files a/z and b/z differ\n"
            "\\ No newline at end of file\n"
        )

        self.assertEqual(parse_unified_diff_ranges(text), {"x.py": [(1, 1)]})

    def test_unparsable_hunk_header_collapses_to_the_whole_file(self) -> None:
        text = (
            "diff --git a/x.py b/x.py\n"
            "--- a/x.py\n"
            "+++ b/x.py\n"
            "@@@ -1,1 -1,1 +1,1 @@@\n"
        )

        self.assertEqual(parse_unified_diff_ranges(text), {"x.py": None})

    def test_paths_with_spaces_are_recovered_from_the_header(self) -> None:
        text = (
            "diff --git a/two words.dat b/two words.dat\n"
            "index 96eb299..3983f0a 100644\n"
            "Binary files a/two words.dat and b/two words.dat differ\n"
        )

        self.assertEqual(parse_unified_diff_ranges(text), {"two words.dat": None})

    def test_quoted_paths_are_unquoted(self) -> None:
        text = (
            'diff --git "a/\\303\\274nicode.py" "b/\\303\\274nicode.py"\n'
            "--- \"a/\\303\\274nicode.py\"\n"
            "+++ \"b/\\303\\274nicode.py\"\n"
            "@@ -2 +2 @@\n"
        )

        self.assertEqual(parse_unified_diff_ranges(text), {"ünicode.py": [(2, 2)]})

    def test_quoted_backslash_path_keeps_the_backslash(self) -> None:
        text = (
            'diff --git "a/weird\\\\back.py" "b/weird\\\\back.py"\n'
            "--- /dev/null\n"
            '+++ "b/weird\\\\back.py"\n'
            "@@ -0,0 +1 @@\n"
        )

        self.assertEqual(parse_unified_diff_ranges(text), {"weird\\back.py": None})

    def test_mode_only_change_contributes_no_path(self) -> None:
        text = (
            "diff --git a/tool.sh b/tool.sh\n"
            "old mode 100644\n"
            "new mode 100755\n"
        )

        self.assertEqual(parse_unified_diff_ranges(text), {})


class ResolveChangeBaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_explicit_base_matches_the_recovery_resolution(self) -> None:
        repo = init_repo(self.root / "repo")
        write(repo / "a.py", "a\n")
        first = commit_all(repo, "first")

        base = resolve_change_base(repo, first)
        recovery = resolve_recovery_base(repo, first)

        self.assertIsInstance(base, ChangeBase)
        self.assertEqual(
            (base.requested, base.ref, base.sha, base.source, base.warning),
            (recovery.requested, recovery.ref, recovery.sha, recovery.source, recovery.warning),
        )
        self.assertEqual(base.source, "explicit")

    def test_unresolved_base_reports_the_warning(self) -> None:
        repo = init_repo(self.root / "repo")
        write(repo / "a.py", "a\n")
        commit_all(repo, "first")
        run(repo, "git", "branch", "-m", "work")

        base = resolve_change_base(repo, None)

        self.assertEqual((base.ref, base.sha, base.source), (None, None, "unknown"))
        self.assertEqual(base.warning, "base-unresolved")

    def test_invalid_base_is_rejected(self) -> None:
        repo = init_repo(self.root / "repo")
        write(repo / "a.py", "a\n")
        commit_all(repo, "first")

        with self.assertRaises(ValueError):
            resolve_change_base(repo, "no-such-ref")

    def test_an_explicit_root_yields_the_same_resolution_as_deriving_it(self) -> None:
        repo = init_repo(self.root / "repo")
        write(repo / "a.py", "a\n")
        first = commit_all(repo, "first")

        derived = resolve_change_base(repo, first)
        explicit = resolve_change_base(repo, first, root=repo)

        self.assertEqual(derived, explicit)

    def test_an_explicit_root_skips_the_repository_root_lookup(self) -> None:
        repo = init_repo(self.root / "repo")
        write(repo / "a.py", "a\n")
        first = commit_all(repo, "first")

        with patch("taf_context.change_ranges._repository_root") as lookup:
            resolve_change_base(repo, first, root=repo)

        lookup.assert_not_called()


class ChangedRangesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _fixture(self) -> tuple[Path, str]:
        """Base commit, a second commit, staged and unstaged edits, and an untracked file."""
        repo = init_repo(self.root / "repo")
        write(repo / "a.py", _numbered("a", 10))
        write(repo / "c.py", _numbered("c", 10))
        write(repo / "e.py", _numbered("e", 3))
        (repo / "bin.dat").write_bytes(bytes(range(0, 64)))
        first = commit_all(repo, "first")

        lines = (repo / "a.py").read_text(encoding="utf-8").splitlines(True)
        lines[2], lines[3] = "A3\n", "A4\n"
        write(repo / "a.py", "".join(lines))
        write(repo / "b.py", _numbered("b", 2))
        run(repo, "git", "mv", "e.py", "f.py")
        commit_all(repo, "second")

        lines = (repo / "a.py").read_text(encoding="utf-8").splitlines(True)
        lines[8] = "A9\n"
        write(repo / "a.py", "".join(lines))
        lines = (repo / "c.py").read_text(encoding="utf-8").splitlines(True)
        del lines[5:7]
        write(repo / "c.py", "".join(lines))
        run(repo, "git", "add", "c.py")
        (repo / "bin.dat").write_bytes(bytes(range(64, 128)))
        write(repo / "d.py", _numbered("d", 1))
        return repo, first

    def test_committed_staged_unstaged_untracked_and_binary_changes(self) -> None:
        repo, first = self._fixture()
        base = resolve_change_base(repo, first)
        snapshot = collect_snapshot(repo)

        paths, warnings = changed_ranges(repo, base, snapshot)

        self.assertEqual(warnings, [])
        self.assertEqual(
            paths,
            (
                ChangedPath("a.py", ((3, 4), (9, 9))),
                ChangedPath("b.py", ()),
                ChangedPath("bin.dat", ()),
                ChangedPath("c.py", ((5, 5),)),
                ChangedPath("d.py", ()),
                ChangedPath("e.py", ((1, 1),)),
                ChangedPath("f.py", ()),
            ),
        )
        self.assertEqual([entry.path for entry in paths], sorted(entry.path for entry in paths))

    def test_unresolved_base_reports_only_uncommitted_changes(self) -> None:
        repo, _ = self._fixture()
        run(repo, "git", "branch", "-m", "work")
        base = resolve_change_base(repo, None)
        snapshot = collect_snapshot(repo)

        paths, warnings = changed_ranges(repo, base, snapshot)

        self.assertEqual(warnings, ["base-unresolved"])
        self.assertEqual(
            paths,
            (
                ChangedPath("a.py", ((9, 9),)),
                ChangedPath("bin.dat", ()),
                ChangedPath("c.py", ((5, 5),)),
                ChangedPath("d.py", ()),
            ),
        )

    def test_unsafe_paths_are_dropped_with_a_warning(self) -> None:
        repo, first = self._fixture()
        base = resolve_change_base(repo, first)

        paths, warnings = changed_ranges(
            repo, base, _snapshot_stub("d.py", "../outside.py", "weird\\back.py", "/absolute.py")
        )

        self.assertEqual(warnings, ["changed-path-unsafe"])
        self.assertEqual(
            [entry.path for entry in paths],
            ["a.py", "b.py", "bin.dat", "c.py", "d.py", "e.py", "f.py"],
        )

    def test_too_many_ranges_collapse_to_the_whole_file(self) -> None:
        repo = init_repo(self.root / "repo")
        write(repo / "wide.py", _numbered("w", 3 * (MAXIMUM_RANGES_PER_PATH + 6)))
        first = commit_all(repo, "first")
        lines = (repo / "wide.py").read_text(encoding="utf-8").splitlines(True)
        for index in range(MAXIMUM_RANGES_PER_PATH + 6):
            lines[index * 3] = f"W{index}\n"
        write(repo / "wide.py", "".join(lines))
        base = resolve_change_base(repo, first)

        paths, warnings = changed_ranges(repo, base, _snapshot_stub())

        self.assertEqual(warnings, ["changed-ranges-collapsed"])
        self.assertEqual(paths, (ChangedPath("wide.py", ()),))

    def test_exactly_the_range_limit_is_kept(self) -> None:
        repo = init_repo(self.root / "repo")
        write(repo / "wide.py", _numbered("w", 3 * MAXIMUM_RANGES_PER_PATH))
        first = commit_all(repo, "first")
        lines = (repo / "wide.py").read_text(encoding="utf-8").splitlines(True)
        for index in range(MAXIMUM_RANGES_PER_PATH):
            lines[index * 3] = f"W{index}\n"
        write(repo / "wide.py", "".join(lines))
        base = resolve_change_base(repo, first)

        paths, warnings = changed_ranges(repo, base, _snapshot_stub())

        self.assertEqual(warnings, [])
        self.assertEqual(len(paths), 1)
        self.assertEqual(len(paths[0].ranges), MAXIMUM_RANGES_PER_PATH)

    def test_too_many_paths_keep_the_first_paths_in_byte_order(self) -> None:
        repo = init_repo(self.root / "repo")
        total = MAXIMUM_CHANGED_PATHS + 5
        for index in range(total):
            write(repo / f"p{index:03d}.py", "one\n")
        first = commit_all(repo, "first")
        for index in range(total):
            write(repo / f"p{index:03d}.py", "two\n")
        base = resolve_change_base(repo, first)

        paths, warnings = changed_ranges(repo, base, _snapshot_stub())

        self.assertEqual(warnings, ["changed-paths-limit"])
        self.assertEqual(len(paths), MAXIMUM_CHANGED_PATHS)
        self.assertEqual(paths[0].path, "p000.py")
        self.assertEqual(paths[-1].path, f"p{MAXIMUM_CHANGED_PATHS - 1:03d}.py")

    def test_git_failure_yields_an_empty_result_with_a_warning(self) -> None:
        repo, first = self._fixture()
        base = resolve_change_base(repo, first)

        with patch("taf_context.change_ranges._git", return_value=None):
            paths, warnings = changed_ranges(repo, base, _snapshot_stub("d.py"))

        self.assertEqual(paths, ())
        self.assertEqual(warnings, ["changed-diff-unavailable"])

    def test_unresolved_base_and_git_failure_warnings_co_occur(self) -> None:
        repo, _ = self._fixture()
        run(repo, "git", "branch", "-m", "work")
        base = resolve_change_base(repo, None)

        with patch("taf_context.change_ranges._git", return_value=None):
            paths, warnings = changed_ranges(repo, base, _snapshot_stub())

        self.assertEqual(paths, ())
        self.assertEqual(warnings, ["base-unresolved", "changed-diff-unavailable"])

    def test_diff_over_the_byte_cap_is_treated_as_unavailable(self) -> None:
        repo, first = self._fixture()
        base = resolve_change_base(repo, first)
        oversized = b"x" * (change_ranges._MAXIMUM_DIFF_BYTES + 1)

        with patch("taf_context.change_ranges._git", return_value=oversized):
            paths, warnings = changed_ranges(repo, base, _snapshot_stub())

        self.assertEqual(paths, ())
        self.assertEqual(warnings, ["changed-diff-unavailable"])

    def test_diff_at_exactly_the_byte_cap_is_parsed_normally(self) -> None:
        repo, first = self._fixture()
        base = resolve_change_base(repo, first)
        text = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a1\n+A1\n"
        encoded = text.encode("utf-8")
        padded = encoded + b" " * (change_ranges._MAXIMUM_DIFF_BYTES - len(encoded))
        self.assertEqual(len(padded), change_ranges._MAXIMUM_DIFF_BYTES)

        with patch("taf_context.change_ranges._git", return_value=padded):
            paths, warnings = changed_ranges(repo, base, _snapshot_stub())

        self.assertEqual(warnings, [])
        self.assertEqual(paths, (ChangedPath("a.py", ((1, 1),)),))

    def test_mnemonic_prefix_config_does_not_corrupt_the_path(self) -> None:
        """A repo-local `diff.mnemonicPrefix` must not survive into the reported path."""
        repo = init_repo(self.root / "repo")
        write(repo / "a.py", _numbered("a", 3))
        first = commit_all(repo, "first")
        run(repo, "git", "config", "diff.mnemonicPrefix", "true")
        lines = (repo / "a.py").read_text(encoding="utf-8").splitlines(True)
        lines[1] = "A2\n"
        write(repo / "a.py", "".join(lines))
        base = resolve_change_base(repo, first)

        paths, warnings = changed_ranges(repo, base, _snapshot_stub())

        self.assertEqual(warnings, [])
        self.assertEqual(paths, (ChangedPath("a.py", ((2, 2),)),))

    def test_noprefix_config_does_not_corrupt_a_path_under_a_directory_named_a(self) -> None:
        """A repo-local `diff.noprefix`, with a real top-level `a/` directory, must not
        corrupt the reported path."""
        repo = init_repo(self.root / "repo")
        write(repo / "a" / "foo.py", _numbered("f", 3))
        first = commit_all(repo, "first")
        run(repo, "git", "config", "diff.noprefix", "true")
        (repo / "a" / "foo.py").unlink()
        base = resolve_change_base(repo, first)

        paths, warnings = changed_ranges(repo, base, _snapshot_stub())

        self.assertEqual(warnings, [])
        self.assertEqual(paths, (ChangedPath("a/foo.py", ((1, 1),)),))

    def test_warnings_are_unique_and_ordered(self) -> None:
        repo, _ = self._fixture()
        run(repo, "git", "branch", "-m", "work")
        base = resolve_change_base(repo, None)

        paths, warnings = changed_ranges(repo, base, _snapshot_stub("../outside.py", "d.py"))

        self.assertEqual(warnings, ["base-unresolved", "changed-path-unsafe"])
        self.assertEqual([entry.path for entry in paths], ["a.py", "bin.dat", "c.py", "d.py"])

    def test_an_explicit_root_yields_the_same_result_as_deriving_it(self) -> None:
        repo, first = self._fixture()
        base = resolve_change_base(repo, first)
        snapshot = collect_snapshot(repo)

        derived = changed_ranges(repo, base, snapshot)
        explicit = changed_ranges(repo, base, snapshot, root=repo)

        self.assertEqual(derived, explicit)

    def test_an_explicit_root_skips_the_repository_root_lookup(self) -> None:
        repo, first = self._fixture()
        base = resolve_change_base(repo, first)
        snapshot = collect_snapshot(repo)

        with patch("taf_context.change_ranges._repository_root") as lookup:
            changed_ranges(repo, base, snapshot, root=repo)

        lookup.assert_not_called()


class StagedChangeRangesTests(unittest.TestCase):
    """`changed_ranges(..., staged=True)` measures the index against HEAD."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _fixture(self) -> Path:
        """Base commit; a staged edit, a staged deletion, and a staged new
        file, then an unstaged edit to the already-staged file and an
        untracked file, neither of which is part of the staged content."""
        repo = init_repo(self.root / "repo")
        write(repo / "a.py", _numbered("a", 10))
        write(repo / "c.py", _numbered("c", 10))
        commit_all(repo, "first")

        lines = (repo / "a.py").read_text(encoding="utf-8").splitlines(True)
        lines[2], lines[3] = "A3\n", "A4\n"
        write(repo / "a.py", "".join(lines))
        lines = (repo / "c.py").read_text(encoding="utf-8").splitlines(True)
        del lines[5:7]
        write(repo / "c.py", "".join(lines))
        write(repo / "b.py", _numbered("b", 2))
        run(repo, "git", "add", "a.py", "c.py", "b.py")

        # Staged and committed here; the working tree still moves on, but
        # only the index matters to the staged change set.
        lines = (repo / "a.py").read_text(encoding="utf-8").splitlines(True)
        lines[8] = "A9\n"
        write(repo / "a.py", "".join(lines))
        write(repo / "untracked.py", _numbered("u", 2))
        return repo

    def test_staged_content_ignores_the_unstaged_edit_and_the_untracked_file(self) -> None:
        repo = self._fixture()
        base = staged_change_base(repo)
        snapshot = collect_snapshot(repo)

        paths, warnings = changed_ranges(repo, base, snapshot, staged=True)

        self.assertEqual(warnings, [])
        self.assertEqual(
            paths,
            (
                # The staged edit, not the later unstaged one at line 9.
                ChangedPath("a.py", ((3, 4),)),
                # A staged new file is a whole-file entry.
                ChangedPath("b.py", ()),
                # A staged deletion is the adjacent new-side line.
                ChangedPath("c.py", ((5, 5),)),
            ),
        )

    def test_an_unborn_repository_reports_no_head_and_an_empty_change_set(self) -> None:
        repo = init_repo(self.root / "repo")

        base = staged_change_base(repo)
        paths, warnings = changed_ranges(repo, base, _snapshot_stub(), staged=True)

        self.assertIsNone(base.sha)
        self.assertEqual(paths, ())
        self.assertEqual(warnings, ["no-head"])

    def test_the_staged_base_is_head_itself_not_the_recovery_priority(self) -> None:
        repo = init_repo(self.root / "repo")
        write(repo / "a.py", "a\n")
        commit_all(repo, "first")
        head = run(repo, "git", "rev-parse", "HEAD")
        # No upstream, no origin/HEAD, and the branch is not named main or
        # master either, so the recovery priority would resolve to nothing.
        run(repo, "git", "branch", "-m", "work")

        base = staged_change_base(repo)

        self.assertEqual(
            (base.requested, base.ref, base.sha, base.source, base.warning),
            (None, "HEAD", head, "staged", None),
        )


if __name__ == "__main__":  # pragma: no cover - manual execution
    unittest.main()
