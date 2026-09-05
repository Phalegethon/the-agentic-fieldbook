"""Opt-in end-to-end check of the commit-time impact hook over a real commit.

A scratch clone of this repository is checked out at a pinned public commit,
built with the released native engine, and given an installed `pre-commit`
launcher. Test A edits a library helper (`normalize_change_base`) whose real
callers - production code and its own unit tests alike - stay untouched and
commits: `git commit` must succeed and the installed launcher's stderr must
report the header, at most five aligned detail lines for the untouched
production files, and a trailer naming any remaining production files and
folding the untouched test files into a count (addendum D16). Test B edits
the same helper again but this time also touches every file that carries an
untouched dependent, so none is left out of the commit: the launcher must
then stay silent. The two tests share one clone and one built index, and
rely on running in that order - the second commit builds on the first
commit the first test made.

The exact dependent set turned out wider than a first reading of the source
suggests: `normalize_change_base` is called from `mcp_server.py` and
`prepare_cli.py` (the two production callers), from its own file at the
excluded same-file call site (`validate_query_request`, part of this very
commit and therefore never a candidate), and from two call sites inside its
own unit test module, `tests/taf_context/test_context_operations.py`, plus
one import-line reference there (`:18`, the
`from taf_context.context_operations import (` statement). Each of
`mcp_server.py` and `prepare_cli.py` also contributes a second, distinct
candidate: that same multi-line import statement resolves every name in the
list to one reference anchored at the `from` line itself, not the name's own
line inside the parenthesized list - so the engine's `reference_line` for
that candidate is the import statement's line, exactly the case this
module's brief flagged as possible. Untouched dependents are now counted per
file (addendum D9): a file that carries both an import-line reference and a
call site collapses to its one call representative, and production files
print before test files.
"""

from __future__ import annotations

from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from taf_context.cli import main

ROOT = Path(__file__).parents[2]
# TAF 2.7.3 ("release: prepare TAF 2.7.3"), a public commit on `main`.
PINNED_COMMIT = "6bbffbb"

CONTEXT_OPERATIONS_PATH = "tools/taf-context/taf_context/context_operations.py"
MCP_SERVER_PATH = "tools/taf-context/taf_context/mcp_server.py"
PREPARE_CLI_PATH = "tools/taf-context/taf_context/prepare_cli.py"
TEST_CONTEXT_OPERATIONS_PATH = "tests/taf_context/test_context_operations.py"

# The message `normalize_change_base` raises; editing it inside its own body
# (lines 904-919 at the pinned commit) is the "library change" the acceptance
# scenario asks for, and it stays a unique string so the substitution below
# cannot silently touch anything else in the file.
_ORIGINAL_MESSAGE = '"selected change base is invalid"'
_EDITED_MESSAGE = '"selected change base is invalid (dogfood)"'
_EDITED_MESSAGE_AGAIN = '"selected change base is invalid (dogfood again)"'

# The exact report the released 0.6.0 engine produced for Test A, pinned
# from a real run of this test (see the task report for the verbatim
# captured stderr). Untouched dependents are counted per file (addendum D9):
# the seven candidates the pre-D9 query returned collapse to three
# representative files - `mcp_server.py` and `prepare_cli.py` each keep only
# their call-site candidate over their import-line one, and
# `test_context_operations.py`'s two call sites collapse to the earlier one.
# The two production files print as detail lines (addendum D16); the one
# test file never takes a slot and is instead folded into the trailer's
# test-file count.
EXPECTED_STDERR_TEST_A = (
    "TAF impact: 2 files depend on this change and are not in this commit\n"
    "  tools/taf-context/taf_context/mcp_server.py:481   "
    "<- context_operations.normalize_change_base\n"
    "  tools/taf-context/taf_context/prepare_cli.py:324  "
    "<- context_operations.normalize_change_base\n"
    "  ... plus 1 test file (ask your agent to list TAF impact for this commit)\n"
)

# The exact marker line the fixture edits for Test C: a comment inserted
# right under the class statement, so the edit falls inside the class's own
# line span and `PrepareCLIError` becomes the changed symbol.
_PREPARE_CLI_ERROR_CLASS_LINE = "class PrepareCLIError(ValueError):\n"
_PREPARE_CLI_ERROR_COMMENT = "    # dogfood: touch every module that raises this error\n"

# The exact report the released 0.6.0 engine produced for Test C, pinned
# from a real run of this test (see the task report for the verbatim
# captured stderr). `PrepareCLIError` is imported and raised by every broker
# module, so its four real callers - two production, two test - all survive
# composition; the two production files print as detail lines (addendum
# D16), and the two test files never take a slot, folded instead into the
# trailer's test-file count.
EXPECTED_STDERR_TEST_C = (
    "TAF impact: 2 files depend on this change and are not in this commit\n"
    "  tools/taf-context/taf_context/mcp_server.py:13    "
    "<- context_operations.PrepareCLIError\n"
    "  tools/taf-context/taf_context/prepare_cli.py:132  "
    "<- context_operations.PrepareCLIError\n"
    "  ... plus 2 test files (ask your agent to list TAF impact for this commit)\n"
)


def _git_isolated_environment(base: dict[str, str]) -> dict[str, str]:
    """`repo_factory.run`'s isolation variables, layered on `base`.

    No host `core.hooksPath`, global gitconfig, or maintenance setting may
    leak into a clone or a commit this test drives, exactly as
    `repo_factory.run` isolates its own fixture repositories.
    """
    environment = dict(base)
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "maintenance.auto",
            "GIT_CONFIG_VALUE_0": "0",
            "GIT_CONFIG_KEY_1": "gc.auto",
            "GIT_CONFIG_VALUE_1": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def _run_git(cwd: Path, *args: str, environment: dict[str, str]) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@unittest.skipUnless(
    os.environ.get("TAF_DOGFOOD") == "1" and os.environ.get("TAF_LEVEL1_BINARY"),
    "set TAF_DOGFOOD=1 and TAF_LEVEL1_BINARY to run the commit-time hook dogfood",
)
class DogfoodHookTests(unittest.TestCase):
    """A real `git commit` through an installed launcher, over this repository."""

    repository: Path
    environment: dict[str, str]

    @classmethod
    def setUpClass(cls) -> None:
        cls._directory = tempfile.TemporaryDirectory()
        scratch = Path(cls._directory.name)
        cls.repository = scratch / "clone"
        git_environment = _git_isolated_environment(
            {"HOME": str(scratch), "PATH": os.environ.get("PATH", "")}
        )
        try:
            subprocess.run(
                ["git", "clone", "--quiet", "--no-hardlinks", str(ROOT), str(cls.repository)],
                env=git_environment,
                check=True,
                capture_output=True,
            )
            _run_git(
                cls.repository, "checkout", "--quiet", PINNED_COMMIT, environment=git_environment
            )
            _run_git(
                cls.repository,
                "config",
                "user.email",
                "dogfood@example.invalid",
                environment=git_environment,
            )
            _run_git(cls.repository, "config", "user.name", "Dogfood", environment=git_environment)
        except subprocess.CalledProcessError as error:
            cls._directory.cleanup()
            stderr = (
                error.stderr.decode("utf-8", "replace").strip()
                if isinstance(error.stderr, bytes)
                else str(error.stderr)
            )
            # A checkout that cannot reach the pinned commit says nothing
            # about the hook, so it skips instead of failing.
            raise unittest.SkipTest(
                f"the pinned commit {PINNED_COMMIT} is unreachable from this checkout: {stderr}"
            )

        cls.environment = {
            "HOME": str(scratch),
            "PATH": os.environ.get("PATH", ""),
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
            "TAF_LEVEL1_BINARY": os.environ["TAF_LEVEL1_BINARY"],
            "TAF_STATE_HOME": str(scratch / "state"),
        }

        built = cls._invoke(
            "prepare", "build", "--repo", str(cls.repository), "--confirm-state-write"
        )
        if built["next_safe_action"] != "use-index":
            cls._directory.cleanup()
            raise unittest.SkipTest("the scratch clone did not become ready")

        installed = cls._invoke(
            "prepare", "hook", "install", "--repo", str(cls.repository), "--confirm-hook-write"
        )
        if not (installed["written"] is True and installed["chained"] is False):
            cls._directory.cleanup()
            raise unittest.SkipTest("hook install did not report the expected summary")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._directory.cleanup()

    @classmethod
    def _invoke(cls, *argv: str) -> dict[str, object]:
        stdout, stderr = StringIO(), StringIO()
        code = main(list(argv), stdout=stdout, stderr=stderr, environment=cls.environment)
        if (code, stderr.getvalue()) != (0, ""):
            raise AssertionError(f"CLI call {argv!r} exited {code}: {stderr.getvalue()!r}")
        return json.loads(stdout.getvalue())

    def _commit_environment(self) -> dict[str, str]:
        return _git_isolated_environment(dict(self.environment))

    def _edit_normalize_change_base_message(self, old: str, new: str) -> None:
        path = self.repository / CONTEXT_OPERATIONS_PATH
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new), encoding="utf-8")

    def _append_comment(self, relative_path: str) -> None:
        path = self.repository / relative_path
        with path.open("a", encoding="utf-8") as stream:
            stream.write("# dogfood: touch this caller in the same commit\n")

    def _head(self) -> str:
        return _run_git(
            self.repository, "rev-parse", "HEAD", environment=self._commit_environment()
        )

    def test_a_a_library_change_names_its_untouched_callers(self) -> None:
        """The acceptance scenario: a real library edit, a real commit, a real warning."""
        before_head = self._head()
        self._edit_normalize_change_base_message(_ORIGINAL_MESSAGE, _EDITED_MESSAGE)
        environment = self._commit_environment()
        _run_git(self.repository, "add", CONTEXT_OPERATIONS_PATH, environment=environment)

        result = subprocess.run(
            ["git", "commit", "-q", "-m", "dogfood: touch normalize_change_base"],
            cwd=self.repository,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(before_head, self._head())
        self.assertNotIn("TAF:", result.stdout)
        # `validate_query_request` is the same-file caller of
        # `normalize_change_base` (context_operations.py:961); it is part of
        # this very commit and must not be named.
        self.assertNotIn("validate_query_request", result.stderr)
        self.assertEqual(result.stderr, EXPECTED_STDERR_TEST_A)

    def test_b_editing_the_callers_too_silences_the_hook(self) -> None:
        """Staging every file with an untouched dependent leaves none untouched."""
        before_head = self._head()
        self._edit_normalize_change_base_message(_EDITED_MESSAGE, _EDITED_MESSAGE_AGAIN)
        self._append_comment(MCP_SERVER_PATH)
        self._append_comment(PREPARE_CLI_PATH)
        self._append_comment(TEST_CONTEXT_OPERATIONS_PATH)
        environment = self._commit_environment()
        _run_git(
            self.repository,
            "add",
            CONTEXT_OPERATIONS_PATH,
            MCP_SERVER_PATH,
            PREPARE_CLI_PATH,
            TEST_CONTEXT_OPERATIONS_PATH,
            environment=environment,
        )

        result = subprocess.run(
            ["git", "commit", "-q", "-m", "dogfood: touch normalize_change_base and its callers"],
            cwd=self.repository,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(before_head, self._head())
        self.assertEqual(result.stderr, "")
        self.assertNotIn("TAF:", result.stdout)

    def test_c_a_widely_referenced_base_class_gets_the_full_candidate_set(self) -> None:
        """The full-composition fix (D13): a base class every broker module raises.

        `PrepareCLIError` is imported and raised by every broker module, so a
        staged edit inside its own class body is exactly the coverage gap
        that motivated this fix: a composed answer wide enough that the old
        output-budget trim would have kept only a handful of candidates in
        path order, discarding production dependents while test files and
        same-file candidates survived. Composing the full candidate set
        before the hook's own five-line cap means the launcher now reports
        the two production callers as detail lines and folds the two test
        files into the trailer's count (addendum D16).
        """
        before_head = self._head()
        path = self.repository / CONTEXT_OPERATIONS_PATH
        text = path.read_text(encoding="utf-8")
        self.assertIn(_PREPARE_CLI_ERROR_CLASS_LINE, text)
        text = text.replace(
            _PREPARE_CLI_ERROR_CLASS_LINE,
            _PREPARE_CLI_ERROR_CLASS_LINE + _PREPARE_CLI_ERROR_COMMENT,
            1,
        )
        path.write_text(text, encoding="utf-8")
        environment = self._commit_environment()
        _run_git(self.repository, "add", CONTEXT_OPERATIONS_PATH, environment=environment)

        result = subprocess.run(
            ["git", "commit", "-q", "-m", "dogfood: touch PrepareCLIError"],
            cwd=self.repository,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(before_head, self._head())
        self.assertNotIn("TAF:", result.stdout)
        self.assertEqual(result.stderr, EXPECTED_STDERR_TEST_C)


if __name__ == "__main__":
    unittest.main()
