from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.preparing_branch_handoff.repo_factory import commit_all, init_repo, run, write

MODULE_PATH = (
    Path(__file__).parents[2]
    / "skills"
    / "branch-handoff"
    / "scripts"
    / "collect_diff.py"
)
SPEC = importlib.util.spec_from_file_location("collect_diff", MODULE_PATH)
assert SPEC and SPEC.loader
collect_diff = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = collect_diff
SPEC.loader.exec_module(collect_diff)


class CollectorTest(unittest.TestCase):
    def test_resolve_ref_rejects_missing_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = init_repo(Path(tmp) / "repo")
            with self.assertRaises(collect_diff.CollectorError):
                collect_diff.resolve_ref(repo, "missing")

    def test_fetch_uses_fresh_remote_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            run(root, "git", "init", "--bare", str(remote))
            seed = init_repo(root / "seed")
            run(seed, "git", "remote", "add", "origin", str(remote))
            run(seed, "git", "push", "-u", "origin", "main")
            run(root, "git", "clone", "--branch", "main", str(remote), str(root / "work"))
            work = root / "work"
            run(work, "git", "config", "user.email", "fixture@example.invalid")
            run(work, "git", "config", "user.name", "Fixture")
            run(work, "git", "switch", "-c", "feature")
            write(work / "feature.txt", "feature\n")
            commit_all(work, "feature")
            write(seed / "remote-main.txt", "new base\n")
            fresh_sha = commit_all(seed, "advance main")
            run(seed, "git", "push", "origin", "main")

            resolution = collect_diff.resolve_base(
                work, "main", offline=False, remote="origin", fetch_timeout=20
            )

            self.assertEqual(fresh_sha, resolution.sha)
            self.assertEqual("fetched", resolution.source)
            self.assertEqual("verified", resolution.freshness)

    def test_fetch_uses_required_flags_and_configured_timeout(self) -> None:
        calls: list[tuple[tuple[str, ...], int]] = []

        def recording_git(
            repo: Path, *args: str, timeout: int = collect_diff.DEFAULT_FETCH_TIMEOUT
        ) -> bytes:
            calls.append((args, timeout))
            if args == ("remote",):
                return b"origin\n"
            if args[:3] == ("show-ref", "--verify", "--quiet"):
                raise collect_diff.CollectorError("not a tag")
            if args[0] == "fetch":
                return b""
            if args[:3] == ("rev-parse", "--verify", "--end-of-options"):
                return b"fresh-sha\n"
            raise AssertionError(f"unexpected Git call: {args}")

        with patch.object(collect_diff, "git", side_effect=recording_git):
            resolution = collect_diff.resolve_base(
                Path("/fixture"),
                "main",
                offline=False,
                remote="origin",
                fetch_timeout=7,
            )

        self.assertEqual("fetched", resolution.source)
        fetch_calls = [(args, timeout) for args, timeout in calls if args[0] == "fetch"]
        self.assertEqual(
            [
                (
                    (
                        "fetch",
                        "--quiet",
                        "--no-tags",
                        "--no-recurse-submodules",
                        "origin",
                        "main",
                    ),
                    7,
                )
            ],
            fetch_calls,
        )

    def test_full_remote_ref_is_fetched_by_branch_name(self) -> None:
        calls: list[tuple[str, ...]] = []

        def recording_git(
            repo: Path, *args: str, timeout: int = collect_diff.DEFAULT_FETCH_TIMEOUT
        ) -> bytes:
            calls.append(args)
            if args == ("remote",):
                return b"origin\n"
            if args[0] == "fetch":
                return b""
            if args[:3] == ("rev-parse", "--verify", "--end-of-options"):
                return b"fresh-sha\n"
            raise AssertionError(f"unexpected Git call: {args}")

        with patch.object(collect_diff, "git", side_effect=recording_git):
            resolution = collect_diff.resolve_base(
                Path("/fixture"), "refs/remotes/origin/main",
                offline=False, remote=None, fetch_timeout=7,
            )

        self.assertEqual("fetched", resolution.source)
        self.assertIn(
            (
                "fetch", "--quiet", "--no-tags", "--no-recurse-submodules",
                "origin", "main",
            ),
            calls,
        )

    def test_offline_uses_cached_remote_and_marks_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = init_repo(Path(tmp) / "repo")
            sha = run(repo, "git", "rev-parse", "main")
            run(repo, "git", "update-ref", "refs/remotes/origin/main", sha)

            resolution = collect_diff.resolve_base(
                repo, "main", offline=True, remote="origin", fetch_timeout=20
            )

            self.assertEqual(sha, resolution.sha)
            self.assertEqual("cached-remote", resolution.source)
            self.assertEqual("unverified", resolution.freshness)

    def test_local_head_ref_skips_remote_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = init_repo(Path(tmp) / "repo")
            run(repo, "git", "remote", "add", "origin", str(Path(tmp) / "missing.git"))

            resolution = collect_diff.resolve_base(
                repo, "HEAD", offline=False, remote="origin", fetch_timeout=20
            )

            self.assertEqual("local", resolution.source)
            self.assertEqual("unverified", resolution.freshness)
            self.assertIsNone(resolution.warning)

    def test_remote_selection_prefers_request_then_upstream_then_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = init_repo(Path(tmp) / "repo")
            run(repo, "git", "remote", "add", "origin", str(Path(tmp) / "origin.git"))
            run(repo, "git", "remote", "add", "upstream", str(Path(tmp) / "upstream.git"))
            run(repo, "git", "config", "branch.main.remote", "upstream")

            self.assertEqual("origin", collect_diff.select_remote(repo, "main", "origin"))
            self.assertEqual("upstream", collect_diff.select_remote(repo, "main", None))

    def test_name_status_parser_preserves_rename_and_unicode(self) -> None:
        raw = b"M\x00src/caf\xc3\xa9.py\x00R100\x00old name.py\x00new name.py\x00"
        self.assertEqual(
            [
                ("M", None, "src/café.py"),
                ("R100", "old name.py", "new name.py"),
            ],
            collect_diff.parse_name_status_z(raw),
        )

    def test_numstat_parser_handles_binary_and_rename(self) -> None:
        raw = b"3\t1\tsrc/app.py\x00-\t-\timage.png\x005\t2\t\x00old.py\x00new.py\x00"
        self.assertEqual(
            {
                (None, "src/app.py"): (3, 1),
                (None, "image.png"): (None, None),
                ("old.py", "new.py"): (5, 2),
            },
            collect_diff.parse_numstat_z(raw),
        )

    def test_classification_never_drops_generated_or_lock_files(self) -> None:
        self.assertEqual(
            "lock",
            collect_diff.classify_path("package-lock.json", binary=False, attributes={}),
        )
        self.assertEqual(
            "generated",
            collect_diff.classify_path("dist/client.min.js", binary=False, attributes={}),
        )
        self.assertEqual(
            "binary",
            collect_diff.classify_path("assets/logo.png", binary=True, attributes={}),
        )

    def test_committed_ledger_covers_every_changed_path_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = init_repo(Path(tmp) / "repo")
            write(repo / "modified.py", "before = True\n")
            write(repo / "deleted.py", "delete me\n")
            write(repo / "old name.py", "renamed\n")
            (repo / "assets").mkdir()
            (repo / "assets" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\nold\x00")
            write(repo / "package-lock.json", '{"lockfileVersion": 1}\n')
            write(repo / "dist" / "client.min.js", "old();\n")
            base = commit_all(repo, "baseline files")

            write(repo / "modified.py", "after = True\n")
            (repo / "deleted.py").unlink()
            (repo / "old name.py").rename(repo / "new name.py")
            (repo / "assets" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\nnew\x00")
            write(repo / "package-lock.json", '{"lockfileVersion": 2}\n')
            write(repo / "dist" / "client.min.js", "new();\n")
            write(repo / "added.py", "added = True\n")
            write(repo / "vendor" / "library.py", "vendored = True\n")
            write(repo / "src" / "app.min.js", "minified();\n")
            head = commit_all(repo, "complete coverage changes")

            ledger, patch = collect_diff.collect_committed_changes(repo, base, head)
            expected_paths = [
                item.decode("utf-8", "surrogateescape")
                for item in collect_diff.git(
                    repo, "diff", "--name-only", "-z", base, head, "--"
                ).split(b"\x00")
                if item
            ]

            self.assertEqual(expected_paths, [change.path for change in ledger])
            self.assertEqual(len(expected_paths), len(set(change.path for change in ledger)))
            self.assertEqual("committed", {change.source_kind for change in ledger}.pop())
            self.assertIn("diff --git", patch)
            by_path = {change.path: change for change in ledger}
            self.assertEqual("D", by_path["deleted.py"].status)
            self.assertEqual("A", by_path["added.py"].status)
            self.assertEqual("old name.py", by_path["new name.py"].old_path)
            self.assertTrue(by_path["assets/logo.png"].binary)
            self.assertEqual("lock", by_path["package-lock.json"].classification)
            self.assertEqual("generated", by_path["dist/client.min.js"].classification)
            self.assertEqual("vendored", by_path["vendor/library.py"].classification)
            self.assertEqual("minified", by_path["src/app.min.js"].classification)

    def test_committed_ledger_preserves_copy_source_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = init_repo(Path(tmp) / "repo")
            original = "".join(f"line {index}\n" for index in range(20))
            copied = "changed line 0\n" + "".join(
                f"line {index}\n" for index in range(1, 20)
            )
            write(repo / "source.py", original)
            base = commit_all(repo, "copy baseline")

            write(repo / "source.py", copied)
            write(repo / "copy.py", copied)
            head = commit_all(repo, "copy source")

            ledger, _ = collect_diff.collect_committed_changes(repo, base, head)

            copied = next(change for change in ledger if change.path == "copy.py")
            self.assertTrue(copied.status.startswith("C"))
            self.assertEqual("source.py", copied.old_path)

    def test_redacts_only_added_secret_values(self) -> None:
        patch = (
            "diff --git a/.env b/.env\n"
            "--- a/.env\n+++ b/.env\n"
            "-API_KEY=old-visible-context\n"
            "+API_KEY=sk_live_1234567890abcdef\n"
            "+MODE=production\n"
        )
        redacted, changed = collect_diff.redact_added_lines(patch)
        self.assertTrue(changed)
        self.assertIn("+API_KEY=<REDACTED>", redacted)
        self.assertIn("-API_KEY=old-visible-context", redacted)
        self.assertIn("+MODE=production", redacted)

    def test_every_cluster_receives_evidence_before_expansion(self) -> None:
        changes = [
            collect_diff.FileChange("M", None, "src/auth/login.py", 2, 1, False, "text", "committed"),
            collect_diff.FileChange("M", None, "migrations/002.sql", 3, 0, False, "text", "committed"),
            collect_diff.FileChange("M", None, "src/ui/home.tsx", 2, 2, False, "text", "committed"),
        ]
        patch = "".join(
            f"diff --git a/{c.path} b/{c.path}\n--- a/{c.path}\n+++ b/{c.path}\n@@ -0,0 +1 @@\n+changed-{index}\n"
            for index, c in enumerate(changes)
        )
        dossier, updated, summary = collect_diff.build_dossier(changes, patch, 4096)
        self.assertLessEqual(len(dossier), 4096)
        self.assertEqual(3, len(summary["clusters"]))
        self.assertTrue(all(cluster["evidence_chars"] > 0 for cluster in summary["clusters"]))
        self.assertEqual(3, len(updated))

    def test_binary_and_generated_files_remain_in_summary_without_payload(self) -> None:
        changes = [
            collect_diff.FileChange("M", None, "assets/logo.png", None, None, True, "binary", "committed"),
            collect_diff.FileChange("M", None, "dist/app.min.js", 900, 900, False, "generated", "committed"),
        ]
        patch = (
            "diff --git a/assets/logo.png b/assets/logo.png\n"
            "Binary files a/assets/logo.png and b/assets/logo.png differ\n"
            "diff --git a/dist/app.min.js b/dist/app.min.js\n"
            "--- a/dist/app.min.js\n"
            "+++ b/dist/app.min.js\n"
            "@@ -0,0 +1 @@\n"
            "+generated()\n"
        )
        dossier, _, summary = collect_diff.build_dossier(changes, patch, 120_000)
        self.assertEqual(2, summary["file_count"])
        self.assertNotIn("binary payload", dossier)
        self.assertIn("assets/logo.png", dossier)
        self.assertIn("dist/app.min.js", dossier)

    def test_optional_sources_are_bounded_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp) / "ticket.txt"
            write(context, "Problem: checkout fails\nTOKEN=secret-value\n" + ("detail\n" * 2000))

            source = collect_diff.load_optional_source(context, "context", 1000)

            self.assertLessEqual(len(source.content), 1000)
            self.assertNotIn("secret-value", source.content)
            self.assertIn("<REDACTED>", source.content)
            self.assertTrue(source.redacted)
            self.assertTrue(source.truncated)

    def test_build_dossier_rejects_an_empty_patch_for_a_committed_ledger(self) -> None:
        changes = [
            collect_diff.FileChange("M", None, "src/app.py", 1, 0, False, "text", "committed"),
        ]

        with self.assertRaisesRegex(
            collect_diff.CollectorError, "patch/ledger section count mismatch"
        ):
            collect_diff.build_dossier(changes, "", 4096)

    def test_redacts_indented_added_private_key_blocks(self) -> None:
        patch = (
            "diff --git a/key.pem b/key.pem\n"
            "+  -----BEGIN PRIVATE KEY-----\n"
            "+  private-key-material\n"
            "+  -----END PRIVATE KEY-----\n"
        )

        redacted, changed = collect_diff.redact_added_lines(patch)

        self.assertTrue(changed)
        self.assertNotIn("private-key-material", redacted)
        self.assertIn("+  -----BEGIN PRIVATE KEY-----", redacted)
        self.assertIn("+  -----END PRIVATE KEY-----", redacted)

    def test_tight_budget_keeps_a_nonempty_optional_source_block(self) -> None:
        changes = [
            collect_diff.FileChange(
                "M", None, ("a" * 1900) + "/file.js", 1, 0, False, "generated", "committed"
            ),
        ]
        optional = collect_diff.OptionalSource(
            path="/ticket", kind="context", content="Z" + ("x" * 200), truncated=False, redacted=False
        )
        patch = (
            f"diff --git a/{changes[0].path} b/{changes[0].path}\n"
            f"--- a/{changes[0].path}\n"
            f"+++ b/{changes[0].path}\n"
            "@@ -0,0 +1 @@\n"
            "+generated\n"
        )

        dossier, _, _ = collect_diff.build_dossier(changes, patch, 4096, (optional,))

        self.assertIn("### context: /ticket", dossier)
        self.assertIn("Z", dossier)

    def test_build_dossier_reports_required_minimum_for_optional_source(self) -> None:
        changes = [
            collect_diff.FileChange(
                "M", None, ("a" * 1953) + "/file.js", 1, 0, False, "generated", "committed"
            ),
        ]
        optional = collect_diff.OptionalSource(
            path="/ticket", kind="context", content="keep", truncated=False, redacted=False
        )
        patch = (
            f"diff --git a/{changes[0].path} b/{changes[0].path}\n"
            f"--- a/{changes[0].path}\n"
            f"+++ b/{changes[0].path}\n"
            "@@ -0,0 +1 @@\n"
            "+generated\n"
        )

        with self.assertRaisesRegex(
            collect_diff.CollectorError, r"required minimum is [0-9]+ characters"
        ):
            collect_diff.build_dossier(changes, patch, 4096, (optional,))

    def test_build_dossier_rejects_a_budget_below_4096(self) -> None:
        with self.assertRaisesRegex(collect_diff.CollectorError, "at least 4096"):
            collect_diff.build_dossier([], "", 4095)

    def test_redacts_through_mismatched_end_until_matching_key_type(self) -> None:
        patch = (
            "diff --git a/key.pem b/key.pem\n"
            "+-----BEGIN RSA PRIVATE KEY-----\n"
            "+first-secret\n"
            "+-----END EC PRIVATE KEY-----\n"
            "+second-secret\n"
            "+-----END RSA PRIVATE KEY-----\n"
            "+safe-after-key\n"
        )

        redacted, changed = collect_diff.redact_added_lines(patch)

        self.assertTrue(changed)
        self.assertIn("+-----BEGIN RSA PRIVATE KEY-----", redacted)
        self.assertIn("+-----END RSA PRIVATE KEY-----", redacted)
        self.assertNotIn("first-secret", redacted)
        self.assertNotIn("second-secret", redacted)
        self.assertNotIn("-----END EC PRIVATE KEY-----", redacted)
        self.assertIn("+safe-after-key", redacted)

    def test_optional_unterminated_private_key_is_redacted_through_eof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp) / "key.pem"
            write(context, "-----BEGIN OPENSSH PRIVATE KEY-----\nprivate-key-material\n")

            source = collect_diff.load_optional_source(context, "context", 1000)

        self.assertTrue(source.redacted)
        self.assertIn("-----BEGIN OPENSSH PRIVATE KEY-----", source.content)
        self.assertNotIn("private-key-material", source.content)

    def test_empty_optional_source_emits_a_content_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp) / "empty.txt"
            write(context, "")
            source = collect_diff.load_optional_source(context, "context", 1000)

        dossier, _, _ = collect_diff.build_dossier([], "", 4096, (source,))

        self.assertIn("### context:", dossier)
        self.assertIn("<empty source>", dossier)

    def test_build_dossier_rejects_a_header_only_patch_section(self) -> None:
        changes = [
            collect_diff.FileChange("M", None, "src/app.py", 1, 0, False, "text", "committed"),
        ]
        patch = "diff --git a/src/app.py b/src/app.py\n"

        with self.assertRaisesRegex(collect_diff.CollectorError, "malformed patch section"):
            collect_diff.build_dossier(changes, patch, 4096)

    def test_build_dossier_accepts_git_pure_mode_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = init_repo(Path(tmp) / "repo")
            script = repo / "script.sh"
            write(script, "#!/bin/sh\necho fixture\n")
            base = commit_all(repo, "add script")
            script.chmod(0o755)
            head = commit_all(repo, "make script executable")

            changes, patch = collect_diff.collect_committed_changes(repo, base, head)
            dossier, _, _ = collect_diff.build_dossier(changes, patch, 4096)

        self.assertIn("script.sh", dossier)

    def test_build_dossier_accepts_git_quoted_space_unicode_and_tab_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = init_repo(Path(tmp) / "repo")
            path = "src/space name\tcafé.txt"
            write(repo / path, "before\n")
            base = commit_all(repo, "add quoted path")
            write(repo / path, "after\n")
            head = commit_all(repo, "modify quoted path")

            changes, patch = collect_diff.collect_committed_changes(repo, base, head)
            dossier, updated, _ = collect_diff.build_dossier(changes, patch, 4096)

        self.assertIn(path, dossier)
        self.assertEqual(path, updated[0].path)

    def test_build_dossier_accepts_git_unquoted_space_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = init_repo(Path(tmp) / "repo")
            path = "script file"
            write(repo / path, "before\n")
            base = commit_all(repo, "add space path")
            write(repo / path, "after\n")
            head = commit_all(repo, "modify space path")

            changes, patch = collect_diff.collect_committed_changes(repo, base, head)
            dossier, updated, _ = collect_diff.build_dossier(changes, patch, 4096)

        self.assertIn(path, dossier)
        self.assertEqual(path, updated[0].path)

    def test_empty_optional_source_requires_the_full_atomic_marker(self) -> None:
        changes = [
            collect_diff.FileChange(
                "M", None, ("a" * 1949) + "/file.js", 1, 0, False, "generated", "committed"
            ),
        ]
        optional = collect_diff.OptionalSource(
            path="/ticket", kind="context", content="", truncated=False, redacted=False, empty=True
        )
        patch = (
            f"diff --git a/{changes[0].path} b/{changes[0].path}\n"
            f"--- a/{changes[0].path}\n"
            f"+++ b/{changes[0].path}\n"
            "@@ -0,0 +1 @@\n"
            "+generated\n"
        )

        with self.assertRaisesRegex(
            collect_diff.CollectorError, r"required minimum is [0-9]+ characters"
        ):
            collect_diff.build_dossier(changes, patch, 4096, (optional,))

    def test_build_dossier_rejects_text_headers_without_a_hunk(self) -> None:
        changes = [
            collect_diff.FileChange("M", None, "src/app.py", 1, 0, False, "text", "committed"),
        ]
        patch = (
            "diff --git a/src/app.py b/src/app.py\n"
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
        )

        with self.assertRaisesRegex(collect_diff.CollectorError, "malformed patch section"):
            collect_diff.build_dossier(changes, patch, 4096)

    def test_worktree_sources_are_separate_and_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = init_repo(Path(tmp) / "repo")
            run(repo, "git", "switch", "-c", "feature")
            write(repo / "committed.py", "value = 1\n")
            commit_all(repo, "feature")
            write(repo / "staged.py", "staged = True\n")
            run(repo, "git", "add", "staged.py")
            write(repo / "committed.py", "value = 2\n")
            write(repo / "untracked.py", "untracked = True\n")

            collection = collect_diff.collect_worktree_changes(repo, "HEAD")

        self.assertEqual(
            {"staged", "unstaged", "untracked"},
            {change.source_kind for change in collection.changes},
        )
        self.assertTrue(collection.patch)
        self.assertTrue(any("dirty" in warning.lower() for warning in collection.warnings))

    def test_cli_consolidates_one_path_changed_in_multiple_layers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = init_repo(Path(tmp) / "repo")
            base = run(repo, "git", "rev-parse", "HEAD")
            run(repo, "git", "switch", "-c", "feature")
            write(repo / "layered.py", "value = 1\n")
            commit_all(repo, "committed layer")
            write(repo / "layered.py", "value = 2\n")
            run(repo, "git", "add", "layered.py")
            write(repo / "layered.py", "value = 3\n")
            out = Path(tmp) / "out"

            self.assertEqual(0, collect_diff.main([
                "--repo", str(repo), "--base", base, "--head", "HEAD", "--offline",
                "--include-worktree", "--output-dir", str(out),
            ]))
            manifest = json.loads((out / "manifest.json").read_text())
            ledger = [json.loads(line) for line in (out / "coverage-ledger.jsonl").read_text().splitlines()]

        self.assertEqual(1, manifest["file_count"])
        self.assertEqual(1, len(ledger))
        self.assertEqual("layered.py", ledger[0]["path"])
        self.assertEqual("committed+staged+unstaged", ledger[0]["source_kind"])

    def test_diff_disables_external_and_text_conversion_drivers(self) -> None:
        calls: list[tuple[str, ...]] = []

        def recording_git(
            repo: Path, *args: str, timeout: int = collect_diff.DEFAULT_FETCH_TIMEOUT,
            input_data: bytes | None = None,
        ) -> bytes:
            calls.append(args)
            return b""

        with patch.object(collect_diff, "git", side_effect=recording_git):
            changes, patch_text = collect_diff._collect_diff_with_args(
                Path("/fixture"), ("base", "head"), "committed"
            )

        self.assertEqual([], changes)
        self.assertEqual("", patch_text)
        diff_calls = [args for args in calls if args and args[0] == "diff"]
        self.assertEqual(3, len(diff_calls))
        self.assertTrue(all("--no-ext-diff" in args for args in diff_calls))
        self.assertTrue(all("--no-textconv" in args for args in diff_calls))

    def test_cli_writes_manifest_ledger_and_bounded_dossier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = init_repo(Path(tmp) / "repo")
            base = run(repo, "git", "rev-parse", "main")
            run(repo, "git", "switch", "-c", "feature")
            write(repo / "src/api/orders.py", "def complete():\n    return True\n")
            commit_all(repo, "feature")
            out = Path(tmp) / "out"

            code = collect_diff.main([
                "--repo", str(repo), "--base", base, "--head", "HEAD", "--offline",
                "--output-dir", str(out), "--max-patch-chars", "5000",
            ])

            self.assertEqual(0, code)
            manifest = json.loads((out / "manifest.json").read_text())
            ledger = (out / "coverage-ledger.jsonl").read_text().splitlines()
            dossier = (out / "model-dossier.md").read_text()
        self.assertEqual(1, len(ledger))
        self.assertLessEqual(manifest["dossier_chars"], 5000)
        self.assertIn("api", dossier)

    def test_cli_excludes_dirty_worktree_by_default_and_warns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = init_repo(Path(tmp) / "repo")
            base = run(repo, "git", "rev-parse", "HEAD")
            write(repo / "dirty.py", "value = True\n")
            out = Path(tmp) / "out"

            self.assertEqual(0, collect_diff.main([
                "--repo", str(repo), "--base", base, "--offline", "--output-dir", str(out),
            ]))
            manifest = json.loads((out / "manifest.json").read_text())
            ledger = (out / "coverage-ledger.jsonl").read_text()

        self.assertEqual("", ledger)
        self.assertTrue(manifest["dirty"])
        self.assertTrue(any("dirty" in warning.lower() for warning in manifest["warnings"]))

    def test_cli_returns_empty_committed_diff_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = init_repo(Path(tmp) / "repo")
            base = run(repo, "git", "rev-parse", "HEAD")
            out = Path(tmp) / "out"

            self.assertEqual(0, collect_diff.main([
                "--repo", str(repo), "--base", base, "--offline", "--output-dir", str(out),
            ]))
            manifest = json.loads((out / "manifest.json").read_text())

        self.assertEqual(0, manifest["file_count"])
        self.assertIn("no committed changes found", manifest["warnings"])

    def test_cli_supports_detached_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = init_repo(Path(tmp) / "repo")
            base = run(repo, "git", "rev-parse", "HEAD")
            run(repo, "git", "switch", "--detach")
            write(repo / "detached.py", "value = True\n")
            commit_all(repo, "detached change")
            out = Path(tmp) / "out"

            self.assertEqual(0, collect_diff.main([
                "--repo", str(repo), "--base", base, "--head", "HEAD", "--offline",
                "--output-dir", str(out),
            ]))
            manifest = json.loads((out / "manifest.json").read_text())
            head = run(repo, "git", "rev-parse", "HEAD")

        self.assertEqual(head, manifest["head_sha"])

    def test_cli_returns_two_for_an_invalid_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = init_repo(Path(tmp) / "repo")

            code = collect_diff.main([
                "--repo", str(repo), "--base", "missing-ref", "--offline",
            ])

        self.assertEqual(2, code)

    def test_cli_warns_and_continues_when_optional_file_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = init_repo(Path(tmp) / "repo")
            base = run(repo, "git", "rev-parse", "HEAD")
            write(repo / "changed.py", "value = True\n")
            commit_all(repo, "change")
            out = Path(tmp) / "out"
            missing = Path(tmp) / "missing.txt"

            self.assertEqual(0, collect_diff.main([
                "--repo", str(repo), "--base", base, "--offline", "--output-dir", str(out),
                "--context-file", str(missing),
            ]))
            manifest = json.loads((out / "manifest.json").read_text())

        self.assertTrue(any("optional" in warning.lower() for warning in manifest["warnings"]))

    def test_cli_returns_two_before_collection_for_a_budget_below_4096(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = init_repo(Path(tmp) / "repo")

            code = collect_diff.main([
                "--repo", str(repo), "--offline", "--max-patch-chars", "4095",
            ])

        self.assertEqual(2, code)

    def test_cli_rejects_output_inside_repo_through_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = init_repo(root / "repo")
            linked_out = root / "linked-out"
            linked_out.symlink_to(repo, target_is_directory=True)

            code = collect_diff.main([
                "--repo", str(repo), "--offline", "--output-dir", str(linked_out / "artifacts"),
            ])

        self.assertEqual(2, code)

    def test_cli_artifacts_are_deterministic_after_path_fields_are_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = init_repo(root / "repo")
            base = run(repo, "git", "rev-parse", "HEAD")
            write(repo / "src/api/orders.py", "def complete():\n    return True\n")
            commit_all(repo, "change")
            first, second = root / "out-one", root / "out-two"
            args = ["--repo", str(repo), "--base", base, "--offline", "--max-patch-chars", "5000"]

            self.assertEqual(0, collect_diff.main([*args, "--output-dir", str(first)]))
            self.assertEqual(0, collect_diff.main([*args, "--output-dir", str(second)]))
            first_manifest = json.loads((first / "manifest.json").read_text())
            second_manifest = json.loads((second / "manifest.json").read_text())
            for manifest in (first_manifest, second_manifest):
                manifest.pop("artifact_paths", None)
                manifest.pop("elapsed_seconds", None)

            self.assertEqual(
                (first / "coverage-ledger.jsonl").read_text(),
                (second / "coverage-ledger.jsonl").read_text(),
            )
            self.assertEqual((first / "model-dossier.md").read_text(), (second / "model-dossier.md").read_text())
            self.assertEqual(first_manifest, second_manifest)

    def test_worktree_reads_untracked_text_with_a_fixed_bounded_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = init_repo(Path(tmp) / "repo")
            payload = repo / "large-untracked.txt"
            write(payload, "x" * (collect_diff.UNTRACKED_TEXT_CHAR_LIMIT * 3))
            original_open = Path.open
            original_fdopen = os.fdopen
            read_sizes: list[int] = []

            class RecordingReader:
                def __init__(self, handle) -> None:
                    self.handle = handle

                def read(self, size: int = -1) -> bytes:
                    read_sizes.append(size)
                    return self.handle.read(size)

                def __enter__(self):
                    return self

                def __exit__(self, *args) -> None:
                    self.handle.close()

            def recording_open(path: Path, mode: str = "r", *args, **kwargs):
                handle = original_open(path, mode, *args, **kwargs)
                return RecordingReader(handle) if path == payload and mode == "rb" else handle

            def recording_fdopen(fd: int, mode: str = "r", *args, **kwargs):
                return RecordingReader(original_fdopen(fd, mode, *args, **kwargs))

            with patch.object(Path, "open", new=recording_open), patch.object(
                collect_diff.os, "fdopen", new=recording_fdopen
            ):
                collection = collect_diff.collect_worktree_changes(repo, "HEAD")

        self.assertEqual([collect_diff.UNTRACKED_TEXT_READ_BYTES + 1], read_sizes)
        self.assertIn("large-untracked.txt", collection.patch)

    def test_optional_source_reads_only_requested_content_and_detection_byte(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "large-context.txt"
            write(payload, "x" * 10000)
            original_open = Path.open
            read_sizes: list[int] = []

            class RecordingReader:
                def __init__(self, handle) -> None:
                    self.handle = handle

                def read(self, size: int = -1) -> bytes:
                    read_sizes.append(size)
                    return self.handle.read(size)

                def __enter__(self):
                    return self

                def __exit__(self, *args) -> None:
                    self.handle.close()

            def recording_open(path: Path, mode: str = "r", *args, **kwargs):
                handle = original_open(path, mode, *args, **kwargs)
                return RecordingReader(handle) if path == payload and mode == "rb" else handle

            with patch.object(Path, "open", new=recording_open):
                source = collect_diff.load_optional_source(payload, "context", 64)

        self.assertEqual([257], read_sizes)
        self.assertTrue(source.truncated)
        self.assertLessEqual(len(source.content), 64)

    def test_worktree_skips_untracked_symlinks_without_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = init_repo(root / "repo")
            outside = root / "outside.txt"
            write(outside, "not for the dossier\n")
            (repo / "outside-link").symlink_to(outside)

            collection = collect_diff.collect_worktree_changes(repo, "HEAD")

        self.assertTrue(any("not a regular file" in warning for warning in collection.warnings))
        self.assertNotIn("not for the dossier", collection.patch)

    def test_worktree_omits_a_path_swapped_to_an_external_symlink_at_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = init_repo(root / "repo")
            payload = repo / "swap-me.txt"
            external = root / "external.txt"
            write(payload, "safe local content\n")
            write(external, "EXTERNAL CONTENT MUST NOT APPEAR\n")
            original_path_open = Path.open
            original_os_open = os.open
            swapped = False

            def swap_payload() -> None:
                nonlocal swapped
                if not swapped:
                    payload.unlink()
                    payload.symlink_to(external)
                    swapped = True

            def swapping_path_open(path: Path, mode: str = "r", *args, **kwargs):
                if path == payload and mode == "rb":
                    swap_payload()
                return original_path_open(path, mode, *args, **kwargs)

            def swapping_os_open(path, flags, mode=0o777):
                if Path(path) == payload:
                    swap_payload()
                return original_os_open(path, flags, mode)

            with patch.object(Path, "open", new=swapping_path_open), patch.object(
                collect_diff.os, "open", new=swapping_os_open
            ):
                collection = collect_diff.collect_worktree_changes(repo, "HEAD")

        self.assertNotIn("EXTERNAL CONTENT MUST NOT APPEAR", collection.patch)
        self.assertTrue(any("omitted" in warning for warning in collection.warnings))
