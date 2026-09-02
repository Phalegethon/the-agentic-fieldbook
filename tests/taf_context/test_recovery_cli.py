"""Black-box tests for the stdout-only recovery command."""

from __future__ import annotations

from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest

from taf_context.cli import main

from .repo_factory import init_committed_repo, write


def invoke(*argv: str) -> tuple[int, str, str]:
    stdout = StringIO()
    stderr = StringIO()
    # The recover command never reads the environment mapping, but the guard
    # test requires an explicit ``environment=`` on every call to the
    # broker's ``main``. Reuse the guard directory tests/__init__.py already
    # pinned TAF_STATE_HOME to instead of creating a second temporary one.
    state_home = os.environ.get("TAF_STATE_HOME", "")
    code = main(
        list(argv),
        stdout=stdout,
        stderr=stderr,
        environment={"HOME": state_home, "PATH": "", "TAF_STATE_HOME": state_home},
    )
    return code, stdout.getvalue(), stderr.getvalue()


class RecoveryCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = init_committed_repo(self.root / "repo")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_default_format_is_plain_model_text_and_writes_no_artifact(self) -> None:
        before = {path.relative_to(self.repo) for path in self.repo.rglob("*")}

        code, stdout, stderr = invoke("recover", "--repo", str(self.repo))

        after = {path.relative_to(self.repo) for path in self.repo.rglob("*")}
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertTrue(stdout.startswith("# TAF Work Recovery Evidence\n"))
        self.assertEqual(before, after)

    def test_json_format_is_canonical_dossier(self) -> None:
        code, stdout, stderr = invoke(
            "recover", "--repo", str(self.repo), "--format", "json", "--base", "main"
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        value = json.loads(stdout)
        self.assertEqual(value["schema_version"], "1")
        self.assertEqual(value["coverage"]["budget_characters"], 2000)
        self.assertEqual(stdout, json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")

    def test_all_explicit_inputs_reach_the_collector(self) -> None:
        write(self.repo / "scratch.txt", "authorized content\n")
        note = self.root / "note.md"
        write(note, "continue the refactor\n")
        first = invoke(
            "recover",
            "--repo", str(self.repo),
            "--base", "main",
            "--max-output-chars", "8000",
            "--include-untracked", "scratch.txt",
            "--note-file", str(note),
        )

        self.assertEqual(first[0], 0)
        self.assertEqual(first[2], "")
        self.assertIn("authorized content", first[1])
        self.assertIn("continue the refactor", first[1])

    def test_invalid_input_has_one_concise_error_and_empty_stdout(self) -> None:
        cases = (
            ("--repo", str(self.root / "missing")),
            ("--repo", str(self.repo), "--base", "missing"),
            ("--repo", str(self.repo), "--max-output-chars", "3000"),
            ("--repo", str(self.repo), "--output-dir", str(self.root / "out")),
        )
        for argv in cases:
            with self.subTest(argv=argv):
                code, stdout, stderr = invoke("recover", *argv)
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertTrue(stderr.startswith("error: "))
                self.assertEqual(len(stderr.rstrip().splitlines()), 1)

    def test_repeated_artifact_options_over_limit_fail_closed(self) -> None:
        argv = ["recover", "--repo", str(self.repo)]
        for index in range(9):
            note = self.root / f"note-{index}.txt"
            write(note, "note\n")
            argv.extend(("--note-file", str(note)))

        code, stdout, stderr = invoke(*argv)

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("eight", stderr)


if __name__ == "__main__":
    unittest.main()
