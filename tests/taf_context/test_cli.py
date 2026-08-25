"""End-to-end tests for Level 0 snapshot and status commands."""

from __future__ import annotations

from datetime import datetime, timezone
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from taf_context.cli import main
from taf_context.models import ContextManifest

from .repo_factory import commit_all, init_committed_repo, write


FIXED_NOW = datetime(2026, 8, 25, 12, 34, 56, tzinfo=timezone.utc)
FIXED_TIMESTAMP = "2026-08-25T12:34:56Z"


def invoke(*argv: str) -> tuple[int, str, str]:
    stdout = StringIO()
    stderr = StringIO()
    code = main(
        list(argv),
        stdout=stdout,
        stderr=stderr,
        utc_clock=lambda: FIXED_NOW,
    )
    return code, stdout.getvalue(), stderr.getvalue()


def decoded_stdout(stdout: str) -> dict[str, object]:
    value = json.loads(stdout)
    if not isinstance(value, dict):
        raise AssertionError("stdout was not a JSON object")
    return value


class SnapshotCommandTests(unittest.TestCase):
    def test_snapshot_writes_ready_artifacts_with_exact_compact_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            write(repo / "README.md", "private source bytes\n")
            commit_all(repo, "add candidate")
            output = root / "artifacts"

            code, stdout, stderr = invoke(
                "snapshot", "--repo", str(repo), "--output-dir", str(output)
            )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            summary = decoded_stdout(stdout)
            self.assertEqual(
                set(summary),
                {
                    "artifacts",
                    "freshness",
                    "path_coverage",
                    "storage_bytes",
                    "dossier_characters",
                },
            )
            self.assertEqual(
                stdout,
                json.dumps(
                    summary,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
            )
            artifacts = summary["artifacts"]
            self.assertEqual(
                artifacts,
                {
                    "dossier": str((output / "dossier.md").resolve()),
                    "manifest": str((output / "manifest.json").resolve()),
                    "snapshot": str((output / "snapshot.json").resolve()),
                },
            )
            manifest = ContextManifest.from_dict(
                json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            )
            dossier = (output / "dossier.md").read_text(encoding="utf-8")
            snapshot_bytes = (output / "snapshot.json").read_bytes()
            self.assertEqual(manifest.created_at, FIXED_TIMESTAMP)
            self.assertEqual(manifest.updated_at, FIXED_TIMESTAMP)
            self.assertEqual(manifest.storage_bytes, len(snapshot_bytes) + len(dossier.encode()))
            self.assertEqual(summary["storage_bytes"], manifest.storage_bytes)
            self.assertEqual(summary["dossier_characters"], len(dossier))
            self.assertEqual(summary["freshness"], "exact")
            self.assertEqual(summary["path_coverage"], 1.0)
            self.assertNotIn("private source bytes", dossier)

    def test_empty_output_directory_is_allowed_and_manifest_is_installed_last(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            output = root / "empty"
            output.mkdir()
            replacements: list[str] = []
            real_replace = os.replace

            def recording_replace(source: object, destination: object) -> None:
                replacements.append(Path(destination).name)
                real_replace(source, destination)

            with mock.patch("taf_context.cli.os.replace", side_effect=recording_replace):
                code, _stdout, stderr = invoke(
                    "snapshot", "--repo", str(repo), "--output-dir", str(output)
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertEqual(
                replacements, ["snapshot.json", "dossier.md", "manifest.json"]
            )

    def test_incomplete_dirty_fingerprint_is_partial_and_warned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            write(repo / "tracked.txt", "dirty content\n")
            output = root / "artifacts"

            code, stdout, stderr = invoke(
                "snapshot",
                "--repo",
                str(repo),
                "--output-dir",
                str(output),
                "--max-dirty-file-bytes",
                "0",
            )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertEqual(decoded_stdout(stdout)["freshness"], "partial")
            manifest = ContextManifest.from_dict(
                json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            )
            self.assertIn("dirty-fingerprint-incomplete", manifest.warnings)
            self.assertIn(
                "dirty-fingerprint-incomplete",
                (output / "dossier.md").read_text(encoding="utf-8"),
            )

    def test_rejects_non_git_input_with_one_concise_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nongit = root / "not-a-repo"
            nongit.mkdir()

            code, stdout, stderr = invoke(
                "snapshot",
                "--repo",
                str(nongit),
                "--output-dir",
                str(root / "artifacts"),
            )

            self.assertEqual(code, 2)
            self.assertEqual(stdout, "")
            self.assertEqual(len(stderr.rstrip("\n").splitlines()), 1)
            self.assertTrue(stderr.startswith("error: "))

    def test_rejects_output_inside_repository_after_symlink_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            inside = repo / "artifacts"
            inside.mkdir()
            alias = root / "outside-looking-link"
            alias.symlink_to(inside, target_is_directory=True)

            for output in (inside, alias):
                with self.subTest(output=output):
                    code, stdout, stderr = invoke(
                        "snapshot",
                        "--repo",
                        str(repo),
                        "--output-dir",
                        str(output),
                    )
                    self.assertEqual(code, 2)
                    self.assertEqual(stdout, "")
                    self.assertEqual(len(stderr.rstrip("\n").splitlines()), 1)
                    self.assertFalse((inside / "manifest.json").exists())

    def test_rejects_nonempty_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            output = root / "artifacts"
            output.mkdir()
            write(output / "keep.txt", "keep\n")

            code, stdout, stderr = invoke(
                "snapshot", "--repo", str(repo), "--output-dir", str(output)
            )

            self.assertEqual(code, 2)
            self.assertEqual(stdout, "")
            self.assertEqual(len(stderr.rstrip("\n").splitlines()), 1)
            self.assertEqual((output / "keep.txt").read_text(), "keep\n")

    def test_rejects_out_of_range_budgets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            cases = (
                ("--max-output-chars", "1023"),
                ("--max-output-chars", "12001"),
                ("--max-dirty-file-bytes", "-1"),
            )
            for index, (option, value) in enumerate(cases):
                with self.subTest(option=option, value=value):
                    code, stdout, stderr = invoke(
                        "snapshot",
                        "--repo",
                        str(repo),
                        "--output-dir",
                        str(root / f"artifacts-{index}"),
                        option,
                        value,
                    )
                    self.assertEqual(code, 2)
                    self.assertEqual(stdout, "")
                    self.assertEqual(len(stderr.rstrip("\n").splitlines()), 1)

    def test_write_failure_leaves_no_ready_manifest_or_run_temporary_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            output = root / "artifacts"
            real_replace = os.replace

            def fail_manifest(source: object, destination: object) -> None:
                if Path(destination).name == "manifest.json":
                    raise OSError("simulated artifact failure")
                real_replace(source, destination)

            with mock.patch("taf_context.cli.os.replace", side_effect=fail_manifest):
                code, stdout, stderr = invoke(
                    "snapshot", "--repo", str(repo), "--output-dir", str(output)
                )

            self.assertEqual(code, 2)
            self.assertEqual(stdout, "")
            self.assertEqual(len(stderr.rstrip("\n").splitlines()), 1)
            self.assertFalse((output / "manifest.json").exists())
            self.assertFalse(any(path.name.endswith(".tmp") for path in output.iterdir()))


class StatusCommandTests(unittest.TestCase):
    def _snapshot(self, repo: Path, output: Path) -> Path:
        code, _stdout, stderr = invoke(
            "snapshot", "--repo", str(repo), "--output-dir", str(output)
        )
        self.assertEqual((code, stderr), (0, ""))
        return output / "manifest.json"

    def test_status_of_unchanged_snapshot_has_exact_compact_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            manifest = self._snapshot(repo, root / "artifacts")

            code, stdout, stderr = invoke(
                "status", "--repo", str(repo), "--manifest", str(manifest)
            )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            summary = decoded_stdout(stdout)
            self.assertEqual(
                set(summary),
                {
                    "freshness",
                    "reasons",
                    "can_incrementally_update",
                    "requires_rebuild",
                },
            )
            self.assertEqual(
                summary,
                {
                    "freshness": "exact",
                    "reasons": ["exact-match"],
                    "can_incrementally_update": False,
                    "requires_rebuild": False,
                },
            )
            self.assertEqual(
                stdout,
                json.dumps(summary, sort_keys=True, separators=(",", ":")) + "\n",
            )

    def test_status_detects_local_forward_head_and_dirty_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            manifest = self._snapshot(repo, root / "artifacts")
            write(repo / "tracked.txt", "next commit\n")
            commit_all(repo, "advance")

            code, stdout, stderr = invoke(
                "status", "--repo", str(repo), "--manifest", str(manifest)
            )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            forward = decoded_stdout(stdout)
            self.assertEqual(forward["freshness"], "incrementally-stale")
            self.assertEqual(forward["reasons"], ["head-forward", "changed-path-set-bounded"])
            self.assertTrue(forward["can_incrementally_update"])

            clean_manifest = self._snapshot(repo, root / "artifacts-2")
            write(repo / "tracked.txt", "dirty overlay\n")
            code, stdout, stderr = invoke(
                "status", "--repo", str(repo), "--manifest", str(clean_manifest)
            )
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            dirty = decoded_stdout(stdout)
            self.assertEqual(dirty["freshness"], "commit-fresh-worktree-stale")
            self.assertEqual(dirty["reasons"], ["dirty-fingerprint-mismatch"])

    def test_status_rejects_invalid_manifest_strictly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            for index, content in enumerate(("{not-json}\n", '{"schema_version":"1"}\n')):
                manifest = root / f"invalid-{index}.json"
                write(manifest, content)
                with self.subTest(content=content):
                    code, stdout, stderr = invoke(
                        "status", "--repo", str(repo), "--manifest", str(manifest)
                    )
                    self.assertEqual(code, 2)
                    self.assertEqual(stdout, "")
                    self.assertEqual(len(stderr.rstrip("\n").splitlines()), 1)
                    self.assertTrue(stderr.startswith("error: "))


if __name__ == "__main__":
    unittest.main()
