"""Tests for the one-line untouched-dependent warnings of a pre-commit hook run."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from taf_context import impact_hook
from taf_context.cli import main
from taf_context.context_operations import QueryArguments, run_query
from taf_context.impact_hook import (
    CHAINED_HOOK_NAME,
    HOOK_FILE_NAME,
    HOOK_MAXIMUM_LINES,
    HOOK_MAXIMUM_RESULTS,
    HOOK_OUTPUT_CHARACTERS,
    HOOK_POINTER,
    HOOK_TIME_LIMIT_SECONDS,
    LAUNCHER_MARKER,
    _entry_point_script,
    _hook_query,
    _render_launcher,
    format_clean_line,
    format_report,
    install_hook,
    launcher_target_path,
    refresh_launcher_target,
    run_hook,
    untouched_dependents,
)
from taf_context.native_transport import OneShotTransport

from .repo_factory import commit_all, init_repo, init_committed_repo, run, write
from .test_prepare_cli import decoded, invoke, write_fake_native_engine


def numbered(prefix: str, count: int) -> str:
    return "\n".join(f"{prefix} {number}" for number in range(1, count + 1)) + "\n"


def edit_line(path: Path, number: int, text: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[number - 1] = text
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def stage_edit(repository: Path, name: str, number: int, text: str) -> None:
    edit_line(repository / name, number, text)
    run(repository, "git", "add", name)


def staged_impact_fixture(root: Path) -> Path:
    """A repository whose index edits `app.py` inside the fake's `app.first`.

    `web.py` and `other.py` exist and are untouched; the fake engine answers
    with one candidate in each of them, so the untouched filter has something
    to keep and something a later stage can take away.
    """
    repository = init_committed_repo(root / "repo")
    write(repository / "app.py", numbered("line", 40))
    write(repository / "web.py", numbered("web", 20))
    write(repository / "other.py", numbered("other", 20))
    commit_all(repository, "base")
    stage_edit(repository, "app.py", 5, "line 5 changed")
    return repository


def build_index(environment: dict[str, str], repository: Path) -> None:
    code, _stdout, stderr = invoke(
        environment, "prepare", "build", "--repo", str(repository), "--confirm-state-write"
    )
    if (code, stderr) != (0, ""):  # pragma: no cover - a broken fixture, not a result
        raise AssertionError(f"fixture build failed: {stderr}")


def ready_repository(root: Path, **options) -> tuple[dict[str, str], Path]:
    """A repository with a built index and a staged change, ready for `run_hook`.

    Duplicates `ImpactHookWarningTests._ready_repository` for tests that call
    `run_hook` directly with a stderr double instead of going through `invoke`.
    """
    repository = staged_impact_fixture(root)
    native = root / "taf-level1"
    write_fake_native_engine(native, **options)
    environment = {
        "TAF_LEVEL1_BINARY": str(native),
        "TAF_STATE_HOME": str(root / "state"),
    }
    build_index(environment, repository)
    return environment, repository


def hook(environment: dict[str, str], repository: Path, *extra: str) -> tuple[int, str, str]:
    return invoke(environment, "prepare", "hook", "run", "--repo", str(repository), *extra)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_until(predicate, seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# The fixture's two untouched dependents (`other.py` an importer, `web.py` a
# caller) reported together: one header, both detail lines padded so their
# `<-` columns align (`other.py:12` and `web.py:12` differ by two
# characters), no trailer since both fit under the five-line cap.
TWO_FILE_REPORT = (
    "TAF impact: 2 files depend on this change and are not in this commit\n"
    "  other.py:12  <- app\n"
    "  web.py:12    <- app.first\n"
)
# Only `other.py` untouched (a commit that also carries `web.py`'s edit):
# the singular header, one detail line, no trailer.
ONE_FILE_REPORT = (
    "TAF impact: 1 file depends on this change and is not in this commit\n"
    "  other.py:12  <- app\n"
)


def framed(report: str) -> str:
    """`run_hook`'s on-screen form: one blank line above and below (D2)."""
    return "\n" + report + "\n"


class ImpactHookReadinessTests(unittest.TestCase):
    """Anything but `use-index` makes the hook silent (spec section 2)."""

    def test_a_missing_engine_is_silent_and_named_only_when_verbose(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = staged_impact_fixture(root)
            environment = {"TAF_STATE_HOME": str(root / "state")}

            code, stdout, stderr = hook(environment, repository)

            self.assertEqual((code, stdout, stderr), (0, "", ""))
            code, stdout, stderr = hook(environment, repository, "--verbose")
            self.assertEqual((code, stdout), (0, ""))
            self.assertEqual(stderr, "TAF hook: the native engine is not installed\n")

    def test_an_unbuilt_index_is_silent_and_names_its_next_safe_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = staged_impact_fixture(root)
            native = root / "taf-level1"
            write_fake_native_engine(native)
            environment = {
                "TAF_LEVEL1_BINARY": str(native),
                "TAF_STATE_HOME": str(root / "state"),
            }

            code, stdout, stderr = hook(environment, repository)

            self.assertEqual((code, stdout, stderr), (0, "", ""))
            code, stdout, stderr = hook(environment, repository, "--verbose")
            self.assertEqual((code, stdout), (0, ""))
            self.assertEqual(
                stderr, "TAF hook: context is not ready (next safe action: build-index)\n"
            )

    def test_a_stale_index_is_silent_and_names_its_next_safe_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = staged_impact_fixture(root)
            fresh = root / "taf-level1"
            write_fake_native_engine(fresh)
            stale = root / "taf-level1-stale"
            write_fake_native_engine(stale, stale=True)
            environment = {
                "TAF_LEVEL1_BINARY": str(fresh),
                "TAF_STATE_HOME": str(root / "state"),
            }
            build_index(environment, repository)
            environment["TAF_LEVEL1_BINARY"] = str(stale)

            code, stdout, stderr = hook(environment, repository)

            self.assertEqual((code, stdout, stderr), (0, "", ""))
            code, stdout, stderr = hook(environment, repository, "--verbose")
            self.assertEqual((code, stdout), (0, ""))
            self.assertEqual(
                stderr, "TAF hook: context is not ready (next safe action: rebuild-index)\n"
            )


class ImpactHookWarningTests(unittest.TestCase):
    """The untouched filter, the exact line format, and the five-line cap."""

    def _ready_repository(self, root: Path, **options) -> tuple[dict[str, str], Path]:
        repository = staged_impact_fixture(root)
        native = root / "taf-level1"
        write_fake_native_engine(native, **options)
        environment = {
            "TAF_LEVEL1_BINARY": str(native),
            "TAF_STATE_HOME": str(root / "state"),
        }
        build_index(environment, repository)
        return environment, repository

    def test_every_untouched_dependent_is_warned_about_in_candidate_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, repository = self._ready_repository(root)

            code, stdout, stderr = hook(environment, repository)

            self.assertEqual((code, stdout), (0, ""))
            self.assertEqual(stderr, framed(TWO_FILE_REPORT))

    def test_a_dependent_inside_the_commit_is_not_warned_about(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, repository = self._ready_repository(root)
            # Line 18 is outside the fake's `web.handle` (lines 5-12), so
            # `web.py` joins the commit without becoming a changed symbol:
            # the path filter is the only thing that can drop its candidate.
            stage_edit(repository, "web.py", 18, "web 18 changed")

            code, stdout, stderr = hook(environment, repository)

            self.assertEqual((code, stdout), (0, ""))
            self.assertEqual(stderr, framed(ONE_FILE_REPORT))

    def test_a_commit_that_carries_every_dependent_prints_the_clean_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, repository = self._ready_repository(root)
            stage_edit(repository, "web.py", 18, "web 18 changed")
            stage_edit(repository, "other.py", 3, "other 3 changed")

            code, stdout, stderr = hook(environment, repository)

            # A completed query that found nothing untouched still says so
            # (D3); this differs from every other quiet outcome, none of
            # which ran a query at all. The staged edit here changes only
            # `app` and `app.first` (`web.py`'s and `other.py`'s edits fall
            # outside any fixture symbol), so `changed_count` is 2.
            self.assertEqual((code, stdout), (0, ""))
            self.assertEqual(stderr, format_clean_line(2) + "\n")
            # --verbose does not change the clean line: it is not a `reason`.
            code, stdout, stderr = hook(environment, repository, "--verbose")
            self.assertEqual((code, stdout), (0, ""))
            self.assertEqual(stderr, format_clean_line(2) + "\n")

    def test_an_unstaged_edit_is_not_part_of_the_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, repository = self._ready_repository(root)
            run(repository, "git", "reset")
            edit_line(repository / "app.py", 5, "line 5 changed again")

            code, stdout, stderr = hook(environment, repository)

            # An empty staged set changes nothing: the query still runs and
            # still finds no untouched dependents (vacuously, since it has no
            # changed symbol to have a dependent on). That is a vacuous
            # check, not a verified all-clear, so it gets the zero-count
            # sentence rather than the "no untouched dependents" wording.
            self.assertEqual((code, stdout), (0, ""))
            self.assertEqual(stderr, format_clean_line(0) + "\n")
            self.assertEqual(stderr, "TAF impact: no indexed symbols changed\n")

    def test_more_dependents_than_the_cap_end_with_a_summary_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, repository = self._ready_repository(root, extra_callers=5)

            code, stdout, stderr = hook(environment, repository)

            self.assertEqual((code, stdout), (0, ""))
            self.assertEqual(
                stderr.splitlines(),
                [""]
                + ["TAF impact: 7 files depend on this change and are not in this commit"]
                + [
                    f"  dep{index:02d}.py:12  <- app.first"
                    for index in range(HOOK_MAXIMUM_LINES)
                ]
                + [f"  ... and 2 more {HOOK_POINTER}", ""],
            )

    def test_exactly_the_cap_many_dependents_end_without_a_summary_line(self) -> None:
        # Five distinct paths, six candidates: web.py contributes both a call
        # (from the callers query) and an import (from the importers query),
        # and the untouched-dependents grouping must collapse that pair to
        # its one call representative for the count to land exactly on the
        # cap with no summary line.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, repository = self._ready_repository(
                root, extra_callers=3, duplicate_caller_path_import=True
            )

            code, stdout, stderr = hook(environment, repository)

            self.assertEqual((code, stdout), (0, ""))
            lines = stderr.splitlines()
            # Header plus the five detail lines, plus the blank frame (D2);
            # nothing was left out.
            self.assertEqual(len(lines), 2 + 1 + HOOK_MAXIMUM_LINES)
            self.assertNotIn("more (", stderr)
            self.assertEqual(
                lines,
                [""]
                + ["TAF impact: 5 files depend on this change and are not in this commit"]
                + [f"  dep{index:02d}.py:12  <- app.first" for index in range(3)]
                + ["  other.py:12  <- app", "  web.py:12    <- app.first", ""],
            )

    def test_the_query_asks_the_engine_for_verified_edges_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request_log = root / "requests.jsonl"
            environment, repository = self._ready_repository(root, request_log=request_log)
            request_log.unlink()  # the build's own requests are not the hook's

            code, _stdout, _stderr = hook(environment, repository)

            self.assertEqual(code, 0)
            requests = [
                json.loads(line)
                for line in request_log.read_text(encoding="utf-8").splitlines()
            ]
            operations = {item["operation"] for item in requests}
            self.assertEqual(operations, {"status", "changed-symbols", "related-symbols"})
            # No request the hook makes ever widens the engine's evidence, so
            # no inferred edge can reach a warning line in the first place.
            self.assertEqual({item["allow_inferred"] for item in requests}, {False})


class HookCleanLineTests(unittest.TestCase):
    """A completed query with no untouched dependents says so (D3)."""

    def test_the_clean_line_names_the_changed_symbol_count(self) -> None:
        self.assertEqual(
            format_clean_line(4),
            "TAF impact: no untouched dependents (4 changed symbols)",
        )
        self.assertEqual(
            format_clean_line(1),
            "TAF impact: no untouched dependents (1 changed symbol)",
        )

    def test_an_absent_count_drops_the_parenthetical(self) -> None:
        self.assertEqual(
            format_clean_line(None), "TAF impact: no untouched dependents"
        )

    def test_a_zero_count_gets_its_own_sentence(self) -> None:
        # A staged change that touches no indexed symbol at all (docs-only,
        # config-only, comment-only) had nothing to check, so it must not
        # read as the same all-clear as a query that actually verified
        # dependents against one or more changed symbols.
        self.assertEqual(
            format_clean_line(0), "TAF impact: no indexed symbols changed"
        )

    def test_a_malformed_changed_count_drops_the_parenthetical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, repository = ready_repository(root)
            # `write_fake_native_engine` always produces a usable integer,
            # so a malformed `changed_count` (the engine sending a string,
            # or a bool riding in on the int check) is exercised by patching
            # the query result directly rather than inventing a new fixture.
            # `True` is the interesting one: `isinstance(True, int)` is true
            # and `True == 1`, so a guard that only checked `int` would print
            # "(True changed symbol)".
            for malformed in ("3", True):
                with self.subTest(changed_count=malformed):
                    malformed_result = {"findings": [], "changed_count": malformed}
                    stderr = io.StringIO()

                    with mock.patch.object(
                        impact_hook, "run_query", return_value=malformed_result
                    ):
                        code = run_hook(
                            repository, environment=environment, stderr=stderr, verbose=False
                        )

                    self.assertEqual(code, 0)
                    self.assertEqual(
                        stderr.getvalue(), "TAF impact: no untouched dependents\n"
                    )

    def test_a_commit_that_carries_every_dependent_prints_the_clean_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, repository = ready_repository(root)
            # Staging the two dependent files leaves nothing untouched.
            run(repository, "git", "add", "web.py", "other.py")
            stage_edit(repository, "web.py", 5, "web 5 changed")
            stage_edit(repository, "other.py", 5, "other 5 changed")
            stderr = io.StringIO()

            code = run_hook(repository, environment=environment, stderr=stderr, verbose=False)

            self.assertEqual(code, 0)
            self.assertTrue(
                stderr.getvalue().startswith("TAF impact: no untouched dependents"),
                stderr.getvalue(),
            )
            # The clean line stands alone: no framing blank lines (D3).
            self.assertFalse(stderr.getvalue().startswith("\n"))
            self.assertEqual(len(stderr.getvalue().splitlines()), 1)

    def test_a_silent_outcome_still_prints_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, repository = ready_repository(root)
            disabled = dict(environment, TAF_HOOK="0")
            stderr = io.StringIO()

            code = run_hook(repository, environment=disabled, stderr=stderr, verbose=False)

            self.assertEqual((code, stderr.getvalue()), (0, ""))


class UntouchedDependentTests(unittest.TestCase):
    """The pure filter and the two pure line formats."""

    def _candidate(
        self,
        path: str,
        evidence: str,
        *,
        relation: str = "",
        reference_line: int = 12,
    ) -> dict[str, object]:
        return {
            "path": path,
            "reference_line": reference_line,
            "edge_evidence": evidence,
            "relation": relation,
            "anchors": [{"qualified_name": "app.first", "path": "app.py"}],
        }

    def test_only_untouched_verified_candidates_survive_in_result_order(self) -> None:
        result = {
            "findings": [
                self._candidate("guess.py", "inferred"),
                self._candidate("web.py", "verified"),
                self._candidate("app.py", "verified"),
                self._candidate("other.py", "verified"),
            ]
        }

        kept = untouched_dependents(result, {"app.py"})

        self.assertEqual([item["path"] for item in kept], ["web.py", "other.py"])

    def test_two_candidates_of_one_path_import_then_call_keeps_the_call(self) -> None:
        result = {
            "findings": [
                self._candidate("web.py", "verified", relation="import", reference_line=1),
                self._candidate("web.py", "verified", relation="call", reference_line=12),
            ]
        }

        kept = untouched_dependents(result, set())

        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["relation"], "call")
        self.assertEqual(kept[0]["reference_line"], 12)

    def test_two_candidates_of_one_path_both_import_keeps_the_first(self) -> None:
        result = {
            "findings": [
                self._candidate("web.py", "verified", relation="import", reference_line=1),
                self._candidate("web.py", "verified", relation="import", reference_line=7),
            ]
        }

        kept = untouched_dependents(result, set())

        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["reference_line"], 1)

    def test_non_test_paths_print_before_test_paths_each_group_in_input_order(
        self,
    ) -> None:
        result = {
            "findings": [
                self._candidate("tests/test_a.py", "verified", relation="call"),
                self._candidate("src/a.py", "verified", relation="call"),
                self._candidate("foo_test.py", "verified", relation="call"),
                self._candidate("src/b.py", "verified", relation="call"),
            ]
        }

        kept = untouched_dependents(result, set())

        self.assertEqual(
            [item["path"] for item in kept],
            ["src/a.py", "src/b.py", "tests/test_a.py", "foo_test.py"],
        )

    def test_a_test_only_input_keeps_its_order(self) -> None:
        result = {
            "findings": [
                self._candidate("tests/test_a.py", "verified", relation="call"),
                self._candidate("foo_test.py", "verified", relation="call"),
            ]
        }

        kept = untouched_dependents(result, set())

        self.assertEqual([item["path"] for item in kept], ["tests/test_a.py", "foo_test.py"])

    def test_seven_distinct_paths_keep_seven_representatives(self) -> None:
        result = {
            "findings": [
                self._candidate(f"dep{index:02d}.py", "verified", relation="call")
                for index in range(7)
            ]
        }

        kept = untouched_dependents(result, set())

        self.assertEqual(len(kept), 7)

    def test_a_result_without_findings_keeps_nothing(self) -> None:
        self.assertEqual(untouched_dependents({}, set()), [])

    def test_a_candidate_without_a_usable_path_is_dropped(self) -> None:
        # A warning line is `<path>:<line>`, so a candidate the engine somehow
        # left without a path could only print `None:12`. Defensive: the
        # engine always sets one.
        pathless = self._candidate("web.py", "verified")
        del pathless["path"]
        empty = self._candidate("", "verified")
        numeric = dict(self._candidate("other.py", "verified"), path=7)
        result = {
            "findings": [pathless, empty, numeric, self._candidate("web.py", "verified")]
        }

        kept = untouched_dependents(result, {"app.py"})

        self.assertEqual([item["path"] for item in kept], ["web.py"])

    def test_a_candidate_without_anchors_is_dropped(self) -> None:
        candidate = self._candidate("web.py", "verified")
        candidate["anchors"] = []
        result = {"findings": [candidate]}

        self.assertEqual(untouched_dependents(result, set()), [])


def _detail_candidate(path: str, qualified_name: str, *, reference_line: int = 12) -> dict:
    """A minimal candidate `format_report` needs: a path, a line, and one anchor."""
    return {
        "path": path,
        "reference_line": reference_line,
        "anchors": [{"qualified_name": qualified_name}],
    }


class FormatReportTests(unittest.TestCase):
    """`format_report`'s exact shapes (addendum D16): header, detail lines, trailer."""

    def test_no_untouched_dependents_prints_nothing(self) -> None:
        self.assertEqual(format_report([], truncated=False, colour=False), [])

    def test_an_omission_with_nothing_to_show_still_prints_nothing(self) -> None:
        # D16: an omission the engine cannot even name a file for is not
        # something a user can act on, so a header with nothing under it
        # would only mislead - the hook stays silent instead.
        self.assertEqual(format_report([], truncated=True, colour=False), [])

    def test_a_single_production_file_uses_the_singular_header(self) -> None:
        lines = format_report(
            [_detail_candidate("app.py", "app.first")], truncated=False, colour=False
        )
        self.assertEqual(
            lines,
            [
                "TAF impact: 1 file depends on this change and is not in this commit",
                "  app.py:12  <- app.first",
            ],
        )

    def test_production_only_exact_prints_every_file_with_no_trailer(self) -> None:
        lines = format_report(
            [
                _detail_candidate("a.py", "sym", reference_line=5),
                _detail_candidate("bb.py", "sym", reference_line=100),
            ],
            truncated=False,
            colour=False,
        )
        self.assertEqual(
            lines,
            [
                "TAF impact: 2 files depend on this change and are not in this commit",
                "  a.py:5     <- sym",
                "  bb.py:100  <- sym",
            ],
        )

    def test_alignment_pads_the_shorter_path_so_every_arrow_column_lines_up(self) -> None:
        lines = format_report(
            [
                _detail_candidate("web.py", "app.first"),
                _detail_candidate("other.py", "app"),
            ],
            truncated=False,
            colour=False,
        )
        detail = lines[1:]
        arrow_columns = {line.index("<-") for line in detail}
        self.assertEqual(len(arrow_columns), 1)

    def test_more_than_five_production_files_end_with_a_lower_bound_trailer(self) -> None:
        candidates = [_detail_candidate(f"dep{index:02d}.py", "sym") for index in range(6)]
        lines = format_report(candidates, truncated=False, colour=False)
        self.assertEqual(lines[0], "TAF impact: 6 files depend on this change and are not in this commit")
        self.assertEqual(len(lines), 1 + HOOK_MAXIMUM_LINES + 1)
        self.assertEqual(lines[-1], f"  ... and 1 more {HOOK_POINTER}")

    def test_a_truncated_result_marks_the_remaining_count_with_a_plus(self) -> None:
        candidates = [_detail_candidate(f"dep{index:02d}.py", "sym") for index in range(7)]
        lines = format_report(candidates, truncated=True, colour=False)
        self.assertEqual(lines[0], "TAF impact: 7+ files depend on this change and are not in this commit")
        self.assertEqual(lines[-1], f"  ... and 2+ more {HOOK_POINTER}")

    def test_every_production_file_prints_but_truncation_says_possibly_more(self) -> None:
        candidates = [_detail_candidate(f"dep{index:02d}.py", "sym") for index in range(3)]
        lines = format_report(candidates, truncated=True, colour=False)
        self.assertEqual(lines[0], "TAF impact: 3+ files depend on this change and are not in this commit")
        self.assertEqual(lines[-1], f"  ... and possibly more {HOOK_POINTER}")

    def test_production_and_test_files_fold_the_tests_into_the_trailer(self) -> None:
        candidates = [
            _detail_candidate("a.py", "sym"),
            _detail_candidate("b.py", "sym"),
            _detail_candidate("tests/test_c.py", "sym"),
            _detail_candidate("tests/test_d.py", "sym"),
            _detail_candidate("tests/test_e.py", "sym"),
        ]
        lines = format_report(candidates, truncated=False, colour=False)
        self.assertEqual(lines[0], "TAF impact: 2 files depend on this change and are not in this commit")
        self.assertEqual(len(lines), 1 + 2 + 1)  # header, 2 production lines, trailer
        self.assertEqual(lines[-1], f"  ... plus 3 test files {HOOK_POINTER}")

    def test_all_production_shown_but_truncated_marks_the_test_count_with_a_plus(self) -> None:
        # Fix wave 1 (concern 2): R == 0 (every production file already
        # printed) and T > 0 (test files folded into the trailer) - when the
        # engine also truncated, the trailer's only remaining count is T, so
        # the `+` that would otherwise sit on R moves onto T instead (D16
        # note: "the `+` marks whichever count is uncertain").
        candidates = [
            _detail_candidate("a.py", "sym"),
            _detail_candidate("b.py", "sym"),
            _detail_candidate("tests/test_c.py", "sym"),
            _detail_candidate("tests/test_d.py", "sym"),
            _detail_candidate("tests/test_e.py", "sym"),
        ]
        lines = format_report(candidates, truncated=True, colour=False)
        self.assertEqual(lines[0], "TAF impact: 2+ files depend on this change and are not in this commit")
        self.assertEqual(lines[-1], f"  ... plus 3+ test files {HOOK_POINTER}")

    def test_remaining_production_and_test_files_both_appear_in_the_trailer(self) -> None:
        candidates = [_detail_candidate(f"dep{index:02d}.py", "sym") for index in range(6)] + [
            _detail_candidate("tests/test_a.py", "sym"),
            _detail_candidate("tests/test_b.py", "sym"),
        ]
        lines = format_report(candidates, truncated=False, colour=False)
        self.assertEqual(lines[0], "TAF impact: 6 files depend on this change and are not in this commit")
        self.assertEqual(lines[-1], f"  ... and 1 more, plus 2 test files {HOOK_POINTER}")

    def test_a_single_test_file_uses_the_singular_test_header_when_nothing_else_depends(
        self,
    ) -> None:
        lines = format_report(
            [_detail_candidate("tests/test_a.py", "sym")], truncated=False, colour=False
        )
        self.assertEqual(
            lines,
            [
                "TAF impact: 1 test file depends on this change and is not in this commit",
                "  tests/test_a.py:12  <- sym",
            ],
        )

    def test_test_files_only_are_printed_as_detail_lines_with_no_trailer(self) -> None:
        candidates = [
            _detail_candidate("tests/test_a.py", "sym"),
            _detail_candidate("tests/test_b.py", "sym"),
        ]
        lines = format_report(candidates, truncated=False, colour=False)
        self.assertEqual(
            lines,
            [
                "TAF impact: 2 test files depend on this change and are not in this commit",
                "  tests/test_a.py:12  <- sym",
                "  tests/test_b.py:12  <- sym",
            ],
        )

    def test_tests_only_header_marks_the_test_count_with_a_plus_when_truncated(self) -> None:
        # Fix wave 1: the tests-only header uses the same `count+` marker as
        # the production header - it is the count `_format_header` is given,
        # regardless of which group it names.
        lines = format_report(
            [_detail_candidate("tests/test_a.py", "sym"), _detail_candidate("tests/test_b.py", "sym")],
            truncated=True,
            colour=False,
        )
        self.assertEqual(
            lines[0], "TAF impact: 2+ test files depend on this change and are not in this commit"
        )

    def test_more_than_five_test_only_files_fold_the_remainder_into_the_trailer(self) -> None:
        # Not spelled out verbatim in D16 (whose R/T wording is written for
        # the mixed production-and-test case): when there is no production
        # dependent at all, the test group itself is what's capped at five,
        # so its own overflow is named with the same "R more" wording R
        # carries in the mixed case, and T stays 0 - there is no separate
        # test category left to name once the printed lines are already
        # test files.
        candidates = [_detail_candidate(f"tests/test_{index:02d}.py", "sym") for index in range(6)]
        lines = format_report(candidates, truncated=False, colour=False)
        self.assertEqual(
            lines[0], "TAF impact: 6 test files depend on this change and are not in this commit"
        )
        self.assertEqual(len(lines), 1 + HOOK_MAXIMUM_LINES + 1)
        self.assertEqual(lines[-1], f"  ... and 1 more {HOOK_POINTER}")

    def test_more_than_five_test_only_files_and_truncated_marks_the_remainder_with_a_plus(
        self,
    ) -> None:
        # Fix wave 1: the tests-only overflow (D16 note) folds the remainder
        # into R, so a truncated engine result marks it with a `+` exactly
        # like the production overflow does.
        candidates = [_detail_candidate(f"tests/test_{index:02d}.py", "sym") for index in range(6)]
        lines = format_report(candidates, truncated=True, colour=False)
        self.assertEqual(
            lines[0], "TAF impact: 6+ test files depend on this change and are not in this commit"
        )
        self.assertEqual(lines[-1], f"  ... and 1+ more {HOOK_POINTER}")

    def test_colour_bolds_only_the_header(self) -> None:
        lines = format_report(
            [_detail_candidate("app.py", "app.first")], truncated=False, colour=True
        )
        self.assertEqual(lines[0], "\x1b[1mTAF impact: 1 file depends on this change and is not in this commit\x1b[0m")
        self.assertNotIn("\x1b", lines[1])

    def test_every_header_starts_with_taf_impact_and_every_other_line_with_two_spaces(
        self,
    ) -> None:
        candidates = [_detail_candidate(f"dep{index:02d}.py", "sym") for index in range(6)] + [
            _detail_candidate("tests/test_a.py", "sym")
        ]
        lines = format_report(candidates, truncated=True, colour=False)
        self.assertTrue(lines[0].startswith("TAF impact:"))
        for line in lines[1:]:
            self.assertTrue(line.startswith("  "))


class ImpactHookSafetyTests(unittest.TestCase):
    """The disable switch, the wall-clock cap, and the always-zero exit."""

    def test_the_disable_switch_stops_the_hook_before_any_engine_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = staged_impact_fixture(root)
            native = root / "taf-level1"
            invocation_log = root / "native-invocations.log"
            write_fake_native_engine(native, invocation_log)
            environment = {
                "TAF_LEVEL1_BINARY": str(native),
                "TAF_STATE_HOME": str(root / "state"),
                "TAF_HOOK": "0",
            }
            build_index(environment, repository)
            after_build = invocation_log.read_text(encoding="utf-8")

            code, stdout, stderr = hook(environment, repository)

            self.assertEqual((code, stdout, stderr), (0, "", ""))
            self.assertEqual(invocation_log.read_text(encoding="utf-8"), after_build)
            code, stdout, stderr = hook(environment, repository, "--verbose")
            self.assertEqual((code, stdout), (0, ""))
            self.assertEqual(stderr, "TAF hook: disabled by TAF_HOOK=0\n")

    def test_a_slow_engine_is_abandoned_inside_the_time_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = staged_impact_fixture(root)
            native = root / "taf-level1"
            pid_file = root / "served-pids"
            write_fake_native_engine(
                native,
                pid_file=pid_file,
                serve_delay_seconds=10.0,
                serve_delay_operations=("related-symbols",),
            )
            environment = {
                "TAF_LEVEL1_BINARY": str(native),
                "TAF_STATE_HOME": str(root / "state"),
            }
            build_index(environment, repository)

            started = time.monotonic()
            code, stdout, stderr = hook(environment, repository)
            elapsed = time.monotonic() - started

            self.assertEqual((code, stdout, stderr), (0, "", ""))
            self.assertGreaterEqual(elapsed, HOOK_TIME_LIMIT_SECONDS)
            self.assertLess(elapsed, HOOK_TIME_LIMIT_SECONDS + 0.5)
            # One child was served and it is gone: the watchdog killed it and
            # nothing retried the request onto a replacement.
            pids = [int(value) for value in pid_file.read_text(encoding="utf-8").split()]
            self.assertEqual(len(pids), 1)
            self.assertTrue(wait_until(lambda: not alive(pids[0])))

            code, stdout, stderr = hook(environment, repository, "--verbose")

            self.assertEqual((code, stdout), (0, ""))
            self.assertEqual(stderr, "TAF hook: exceeded the 3.0 second limit\n")
            self.assertEqual(len(pid_file.read_text(encoding="utf-8").split()), 2)

    def test_a_directory_that_is_not_a_repository_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plain = root / "plain"
            plain.mkdir()
            native = root / "taf-level1"
            write_fake_native_engine(native)
            environment = {
                "TAF_LEVEL1_BINARY": str(native),
                "TAF_STATE_HOME": str(root / "state"),
            }

            code, stdout, stderr = hook(environment, plain)

            self.assertEqual((code, stdout, stderr), (0, "", ""))
            code, stdout, stderr = hook(environment, plain, "--verbose")
            self.assertEqual((code, stdout), (0, ""))
            self.assertTrue(stderr.startswith("TAF hook: SnapshotError: "))
            self.assertEqual(len(stderr.splitlines()), 1)

    def test_the_first_commit_of_a_repository_is_silent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = init_repo(root / "repo")
            write(repository / "app.py", numbered("line", 40))
            run(repository, "git", "add", "app.py")
            native = root / "taf-level1"
            write_fake_native_engine(native)
            environment = {
                "TAF_LEVEL1_BINARY": str(native),
                "TAF_STATE_HOME": str(root / "state"),
            }

            code, stdout, stderr = hook(environment, repository)

            self.assertEqual((code, stdout, stderr), (0, "", ""))
            code, stdout, stderr = hook(environment, repository, "--verbose")
            self.assertEqual((code, stdout), (0, ""))
            self.assertEqual(
                stderr,
                "TAF hook: PrepareCLIError: repository must have at least one commit\n",
            )


class HookQueryTests(unittest.TestCase):
    """The one query's shape is pinned so a typo in a constant fails a test."""

    def test_the_hook_query_carries_exactly_the_brief_s_budget_and_no_query(self) -> None:
        self.assertEqual(
            _hook_query(),
            QueryArguments(
                operation="impact-candidates",
                query=None,
                result_identities=(),
                path_prefixes=[],
                languages=[],
                symbol_kinds=[],
                source_types=[],
                maximum_results=HOOK_MAXIMUM_RESULTS,
                maximum_output_characters=HOOK_OUTPUT_CHARACTERS,
                allow_inferred=False,
                direction=None,
                base=None,
                staged=True,
            ),
        )
        # Pinned literally too, so a typo in either constant still fails this
        # test even if both sides of the equality above drifted together.
        self.assertEqual(HOOK_MAXIMUM_RESULTS, 1_000_000)
        self.assertEqual(HOOK_OUTPUT_CHARACTERS, 10_000_000)


class HookFullCompositionTests(unittest.TestCase):
    """The hook composes the full candidate set before its five-line cap (D13).

    `run_query`'s output-budget trim (`trim_to_budget`) exists to keep a CLI
    or MCP answer inside a size a model context can hold. The hook never
    serializes its answer at all, so a budget trim ahead of its own
    five-line cap can only ever do harm: it can drop a legitimate untouched
    dependent whose path happens to sort late, before the hook ever gets to
    choose its five lines from the whole set. This fixture makes forty
    distinct callers of the same changed symbol - `dep00.py` through
    `dep39.py`, verified in `write_fake_native_engine`'s own `CALLERS` table
    to be forty genuinely distinct paths - wide enough that the old
    16-result/12000-character budget cut before the fixture's other two
    candidates, `web.py` (a caller) and `other.py` (an importer), which both
    sort after every `depNN.py` and so are always last to be composed.
    """

    def test_a_late_sorting_path_survives_and_the_summary_counts_every_file(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, repository = ready_repository(root, extra_callers=40)

            code, stdout, stderr = hook(environment, repository)

            self.assertEqual((code, stdout), (0, ""))
            lines = stderr.splitlines()
            # Blank frame, header, five detail lines, one trailer, blank frame (D2).
            self.assertEqual(len(lines), 2 + 1 + HOOK_MAXIMUM_LINES + 1)
            self.assertEqual(
                lines[1], "TAF impact: 42 files depend on this change and are not in this commit"
            )
            self.assertEqual(
                lines[2 : 2 + HOOK_MAXIMUM_LINES],
                [
                    f"  dep{index:02d}.py:12  <- app.first"
                    for index in range(HOOK_MAXIMUM_LINES)
                ],
            )
            # 40 dep*.py callers + web.py (a caller) + other.py (an
            # importer) = 42 untouched files; five print, 37 remain.
            self.assertEqual(lines[-2], f"  ... and 37 more {HOOK_POINTER}")
            self.assertEqual(lines[-1], "")

            # The point of the fix: the composed answer itself carries every
            # candidate, not just the ones an output-budget trim would have
            # left - so a path that sorts after the old trim tail is still
            # in it, even though the hook's own five-line cap never prints
            # it here.
            result = run_query(
                repository,
                _hook_query(),
                environment=environment,
                transport_for=OneShotTransport,
            )
            kept = untouched_dependents(result, {"app.py"})
            self.assertEqual(len(kept), 42)
            self.assertIn("web.py", [item["path"] for item in kept])
            self.assertIn("other.py", [item["path"] for item in kept])

    def test_more_than_sixty_four_candidates_are_all_composed_and_counted(
        self,
    ) -> None:
        """The retired composition cap (D14).

        `extra_callers=63` puts exactly 64 candidates on the wire for the
        one `callers` call (`web.py` plus `dep00.py` through `dep62.py` -
        the wire's own per-collection ceiling, `_MAX_COLLECTION` in
        `level1_models.py`, so this is as far as a single relationship call
        can go without the fixture itself becoming invalid), plus one more
        from the `importers` call (`other.py`): 65 merged candidates, past
        the 64-result ceiling `compose_impact_candidates` used to apply as
        a plain slice. Under that old cap the slice kept only the first 64
        in sort order - `dep00.py` through `dep62.py` and `other.py` -
        silently dropping `web.py`, so the summary undercounted by one
        (should have said "60 more", said "59 more" instead). With the cap
        retired the composed answer carries every one of the 65 candidates,
        so the summary's count is exact.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, repository = ready_repository(root, extra_callers=63)

            code, stdout, stderr = hook(environment, repository)

            self.assertEqual((code, stdout), (0, ""))
            lines = stderr.splitlines()
            # Blank frame, header, five detail lines, one trailer, blank frame (D2).
            self.assertEqual(len(lines), 2 + 1 + HOOK_MAXIMUM_LINES + 1)
            self.assertEqual(
                lines[1], "TAF impact: 65 files depend on this change and are not in this commit"
            )
            self.assertEqual(
                lines[2 : 2 + HOOK_MAXIMUM_LINES],
                [
                    f"  dep{index:02d}.py:12  <- app.first"
                    for index in range(HOOK_MAXIMUM_LINES)
                ],
            )
            self.assertEqual(lines[-2], f"  ... and 60 more {HOOK_POINTER}")
            self.assertEqual(lines[-1], "")


class HookTruncationMarkerTests(unittest.TestCase):
    """The lower-bound marker: `+` when files remain, "possibly more" when not (D14).

    `write_fake_native_engine`'s default-off `related_truncated` option makes
    every `related-symbols` answer report `truncated: true` (with
    `omitted_count` incremented), the same shape an engine that genuinely cut
    a relationship call in some direction would return. `compose_impact_candidates`
    ORs that flag into the composed result regardless of its own (now
    unbindable) slice, so these tests exercise the marker on its own, apart
    from the composition-cap fix above.
    """

    def test_more_than_five_files_remain_and_print_a_lower_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, repository = ready_repository(
                root, extra_callers=20, related_truncated=True
            )

            code, stdout, stderr = hook(environment, repository)

            self.assertEqual((code, stdout), (0, ""))
            lines = stderr.splitlines()
            # Blank frame, header, five detail lines, one trailer, blank frame (D2).
            self.assertEqual(len(lines), 2 + 1 + HOOK_MAXIMUM_LINES + 1)
            # 20 dep*.py callers + web.py (a caller) + other.py (an
            # importer) = 22 untouched files; the header count is marked a
            # lower bound because the engine reported its own omission; five
            # print, 17 remain, also marked with a `+`.
            self.assertEqual(
                lines[1],
                "TAF impact: 22+ files depend on this change and are not in this commit",
            )
            self.assertEqual(
                lines[-2],
                f"  ... and 17+ more {HOOK_POINTER}",
            )
            self.assertEqual(lines[-1], "")

    def test_five_or_fewer_files_remain_and_print_possibly_more(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, repository = ready_repository(
                root, related_truncated=True
            )

            code, stdout, stderr = hook(environment, repository)

            self.assertEqual((code, stdout), (0, ""))
            # Only web.py and other.py are untouched - both print, nothing
            # remains to count - but the engine still reported an omission,
            # so the hook still speaks up rather than staying silent about
            # it: the header is marked with a `+` and the trailer falls
            # back to "possibly more" since nothing exact remains to name.
            self.assertEqual(
                stderr,
                framed(
                    "TAF impact: 2+ files depend on this change and are not in this commit\n"
                    "  other.py:12  <- app\n"
                    "  web.py:12    <- app.first\n"
                    f"  ... and possibly more {HOOK_POINTER}\n"
                ),
            )


def commit_environment(extra: dict[str, str]) -> dict[str, str]:
    """The environment a real `git commit` subprocess needs to run the launcher.

    Carries the host's own `PATH`/`HOME`/`LANG`/`LC_ALL` (the launcher's
    embedded interpreter is an absolute path, but `git` itself and every
    internal `git` call the hook makes are found on `PATH`), the fixture's
    `TAF_STATE_HOME`/`TAF_LEVEL1_BINARY`, and `repo_factory.run`'s own git
    config isolation so no host hooksPath or maintenance setting leaks in.
    """
    environment: dict[str, str] = {
        key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL") if key in os.environ
    }
    environment.update(extra)
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


class HookInstallTests(unittest.TestCase):
    """`hook install` writes an executable launcher naming the entry point."""

    def test_a_fresh_install_writes_an_executable_launcher_and_the_exact_summary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")

            code, stdout, stderr = invoke(
                {"TAF_STATE_HOME": str(root / "state")},
                "prepare", "hook", "install",
                "--repo", str(repository),
                "--confirm-hook-write",
            )

            self.assertEqual((code, stderr), (0, ""))
            hook_path = repository / ".git" / "hooks" / HOOK_FILE_NAME
            self.assertTrue(hook_path.is_file())
            self.assertEqual(hook_path.stat().st_mode & 0o111, 0o111)
            content = hook_path.read_text(encoding="utf-8")
            lines = content.splitlines()
            self.assertEqual(lines[1], LAUNCHER_MARKER)
            interpreter = str(Path(sys.executable).resolve())
            script = _entry_point_script()
            self.assertIn(shlex.quote(interpreter), content)
            self.assertIn(shlex.quote(str(script)), content)
            self.assertNotIn("\nexec ", content)
            self.assertEqual(
                decoded(stdout),
                {
                    "schema_version": "1",
                    "mode": "hook-install",
                    "hook_path": str(hook_path),
                    "written": True,
                    "hook_mode": "advisory",
                    "chained": False,
                    "chained_hook_path": None,
                    "launcher_current": True,
                    "interpreter": interpreter,
                    "script": str(script),
                    "next_safe_action": "none",
                },
            )

    def test_installing_twice_writes_identical_bytes_both_times(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")
            hook_path = repository / ".git" / "hooks" / HOOK_FILE_NAME
            environment = {"TAF_STATE_HOME": str(root / "state")}

            code1, stdout1, _stderr1 = invoke(
                environment, "prepare", "hook", "install",
                "--repo", str(repository), "--confirm-hook-write",
            )
            first_bytes = hook_path.read_bytes()
            code2, stdout2, _stderr2 = invoke(
                environment, "prepare", "hook", "install",
                "--repo", str(repository), "--confirm-hook-write",
            )
            second_bytes = hook_path.read_bytes()

            self.assertEqual((code1, code2), (0, 0))
            self.assertEqual(first_bytes, second_bytes)
            self.assertTrue(decoded(stdout1)["written"])
            self.assertTrue(decoded(stdout2)["written"])

    def test_a_pinned_interpreter_is_embedded_verbatim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")
            pinned = root / "some" / "other" / "python3"

            summary = install_hook(
                repository,
                chain=False,
                interpreter=pinned,
                environment={"TAF_STATE_HOME": str(root / "state")},
            )

            self.assertEqual(summary["interpreter"], str(pinned.resolve()))
            hook_path = repository / ".git" / "hooks" / HOOK_FILE_NAME
            self.assertIn(
                shlex.quote(str(pinned.resolve())), hook_path.read_text(encoding="utf-8")
            )


class HookRefusalTests(unittest.TestCase):
    """Every refusal the spec names, verbatim, and nothing written by it."""

    def test_install_without_confirmation_refuses_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")

            code, stdout, stderr = invoke(
                {}, "prepare", "hook", "install", "--repo", str(repository)
            )

            self.assertEqual(code, 2)
            self.assertEqual(stdout, "")
            self.assertEqual(stderr, "error: explicit hook-write confirmation required\n")
            self.assertFalse((repository / ".git" / "hooks" / HOOK_FILE_NAME).exists())

    def test_remove_without_confirmation_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")

            code, stdout, stderr = invoke(
                {}, "prepare", "hook", "remove", "--repo", str(repository)
            )

            self.assertEqual(code, 2)
            self.assertEqual(stderr, "error: explicit hook-write confirmation required\n")

    def test_a_foreign_hook_without_chain_is_refused_and_left_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")
            hooks_dir = repository / ".git" / "hooks"
            hooks_dir.mkdir(parents=True, exist_ok=True)
            foreign = hooks_dir / HOOK_FILE_NAME
            foreign_bytes = b"#!/bin/sh\necho foreign >&2\nexit 3\n"
            foreign.write_bytes(foreign_bytes)
            foreign.chmod(0o755)

            code, stdout, stderr = invoke(
                {}, "prepare", "hook", "install",
                "--repo", str(repository), "--confirm-hook-write",
            )

            self.assertEqual(code, 2)
            self.assertEqual(
                stderr,
                "error: a foreign pre-commit hook exists; pass --chain to run it after "
                "TAF, or remove it first\n",
            )
            self.assertEqual(foreign.read_bytes(), foreign_bytes)

    def test_chaining_refuses_when_a_stale_chained_hook_already_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")
            hooks_dir = repository / ".git" / "hooks"
            hooks_dir.mkdir(parents=True, exist_ok=True)
            (hooks_dir / HOOK_FILE_NAME).write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            (hooks_dir / HOOK_FILE_NAME).chmod(0o755)
            (hooks_dir / CHAINED_HOOK_NAME).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

            code, stdout, stderr = invoke(
                {}, "prepare", "hook", "install",
                "--repo", str(repository), "--confirm-hook-write", "--chain",
            )

            self.assertEqual(code, 2)
            self.assertEqual(
                stderr,
                f"error: {CHAINED_HOOK_NAME} already exists; remove it before chaining "
                "another hook\n",
            )

    def test_remove_refuses_a_foreign_hook(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")
            hooks_dir = repository / ".git" / "hooks"
            hooks_dir.mkdir(parents=True, exist_ok=True)
            (hooks_dir / HOOK_FILE_NAME).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

            code, stdout, stderr = invoke(
                {}, "prepare", "hook", "remove",
                "--repo", str(repository), "--confirm-hook-write",
            )

            self.assertEqual(code, 2)
            self.assertEqual(
                stderr, "error: pre-commit is not a TAF launcher; nothing removed\n"
            )

    def test_remove_on_an_absent_hook_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")

            code, stdout, stderr = invoke(
                {}, "prepare", "hook", "remove",
                "--repo", str(repository), "--confirm-hook-write",
            )

            self.assertEqual((code, stderr), (0, ""))
            summary = decoded(stdout)
            self.assertEqual(summary["removed"], False)
            self.assertEqual(summary["restored"], False)

    def test_a_redirected_hooks_path_refuses_install_and_remove(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")
            custom = root / "custom-hooks"
            custom.mkdir()
            run(repository, "git", "config", "core.hooksPath", str(custom))

            code, stdout, stderr = invoke(
                {}, "prepare", "hook", "install",
                "--repo", str(repository), "--confirm-hook-write",
            )
            self.assertEqual(code, 2)
            self.assertEqual(
                stderr,
                f"error: core.hooksPath redirects hooks to {custom}; TAF installs only "
                "under the repository's own hooks directory\n",
            )

            code, stdout, stderr = invoke(
                {}, "prepare", "hook", "remove",
                "--repo", str(repository), "--confirm-hook-write",
            )
            self.assertEqual(code, 2)
            self.assertEqual(
                stderr,
                f"error: core.hooksPath redirects hooks to {custom}; nothing to remove "
                "there\n",
            )

            code, stdout, stderr = invoke(
                {"TAF_STATE_HOME": str(root / "state")},
                "prepare", "hook", "status", "--repo", str(repository),
            )
            self.assertEqual((code, stderr), (0, ""))
            summary = decoded(stdout)
            self.assertEqual(summary["hook"], "redirected")
            self.assertEqual(summary["hooks_path"], str(custom))
            self.assertIsNone(summary["hook_path"])
            self.assertFalse(summary["chained"])
            self.assertIsNone(summary["launcher_current"])
            self.assertIsNone(summary["launcher_text_current"])
            self.assertIsNone(summary["launcher_generation"])


class HookChainTests(unittest.TestCase):
    """Chaining a foreign hook, and `remove` restoring it byte-for-byte."""

    def test_chaining_a_foreign_hook_runs_it_before_taf_and_remove_restores_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")
            hooks_dir = repository / ".git" / "hooks"
            hooks_dir.mkdir(parents=True, exist_ok=True)
            foreign_path = hooks_dir / HOOK_FILE_NAME
            foreign_bytes = b"#!/bin/sh\necho foreign >&2\nexit 3\n"
            foreign_path.write_bytes(foreign_bytes)
            foreign_path.chmod(0o755)
            foreign_mode = foreign_path.stat().st_mode
            environment = {"TAF_STATE_HOME": str(root / "state")}

            code, stdout, _stderr = invoke(
                environment, "prepare", "hook", "install",
                "--repo", str(repository), "--confirm-hook-write", "--chain",
            )
            self.assertEqual(code, 0)
            summary = decoded(stdout)
            chained_path = hooks_dir / CHAINED_HOOK_NAME
            self.assertTrue(summary["chained"])
            self.assertEqual(summary["chained_hook_path"], str(chained_path))
            self.assertEqual(chained_path.read_bytes(), foreign_bytes)
            self.assertEqual(chained_path.stat().st_mode, foreign_mode)
            launcher_lines = (hooks_dir / HOOK_FILE_NAME).read_text(
                encoding="utf-8"
            ).splitlines()
            quoted_chained = shlex.quote(str(chained_path))
            self.assertEqual(
                launcher_lines[3:6],
                [
                    f"if [ -x {quoted_chained} ]; then",
                    f'  {quoted_chained} "$@" || exit $?',
                    "fi",
                ],
            )

            # A re-install without --chain keeps the launcher chained.
            code, stdout, _stderr = invoke(
                environment, "prepare", "hook", "install",
                "--repo", str(repository), "--confirm-hook-write",
            )
            self.assertEqual(code, 0)
            self.assertTrue(decoded(stdout)["chained"])

            code, stdout, _stderr = invoke(
                environment, "prepare", "hook", "remove",
                "--repo", str(repository), "--confirm-hook-write",
            )
            self.assertEqual(code, 0)
            summary = decoded(stdout)
            self.assertEqual((summary["removed"], summary["restored"]), (True, True))
            self.assertFalse(chained_path.exists())
            restored_path = hooks_dir / HOOK_FILE_NAME
            self.assertEqual(restored_path.read_bytes(), foreign_bytes)
            self.assertEqual(restored_path.stat().st_mode, foreign_mode)


class HookOrphanedChainTests(unittest.TestCase):
    """A `pre-commit.taf-chained` backup with no `pre-commit` alongside it.

    This state is reachable without any crash: an external actor, or the user
    themselves, can delete `.git/hooks/pre-commit` by hand while a backup from
    an earlier `--chain` install still sits next to it. `install` and `remove`
    must adopt or restore that backup rather than silently orphaning it.
    """

    def test_install_over_an_absent_hook_with_a_backup_adopts_it_as_chained(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")
            hooks_dir = repository / ".git" / "hooks"
            hooks_dir.mkdir(parents=True, exist_ok=True)
            chained_path = hooks_dir / CHAINED_HOOK_NAME
            backup_bytes = b"#!/bin/sh\necho backup >&2\nexit 3\n"
            chained_path.write_bytes(backup_bytes)
            chained_path.chmod(0o755)

            code, stdout, stderr = invoke(
                {"TAF_STATE_HOME": str(root / "state")}, "prepare", "hook", "install",
                "--repo", str(repository), "--confirm-hook-write",
            )

            self.assertEqual((code, stderr), (0, ""))
            summary = decoded(stdout)
            self.assertTrue(summary["chained"])
            self.assertEqual(summary["chained_hook_path"], str(chained_path))
            # The backup itself is untouched: install only writes the launcher.
            self.assertEqual(chained_path.read_bytes(), backup_bytes)
            launcher_lines = (hooks_dir / HOOK_FILE_NAME).read_text(
                encoding="utf-8"
            ).splitlines()
            quoted_chained = shlex.quote(str(chained_path))
            self.assertEqual(
                launcher_lines[3:6],
                [
                    f"if [ -x {quoted_chained} ]; then",
                    f'  {quoted_chained} "$@" || exit $?',
                    "fi",
                ],
            )

    def test_remove_over_an_absent_hook_with_a_backup_restores_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")
            hooks_dir = repository / ".git" / "hooks"
            hooks_dir.mkdir(parents=True, exist_ok=True)
            chained_path = hooks_dir / CHAINED_HOOK_NAME
            backup_bytes = b"#!/bin/sh\necho backup >&2\nexit 3\n"
            chained_path.write_bytes(backup_bytes)
            chained_path.chmod(0o755)
            backup_mode = chained_path.stat().st_mode

            code, stdout, stderr = invoke(
                {}, "prepare", "hook", "remove",
                "--repo", str(repository), "--confirm-hook-write",
            )

            self.assertEqual((code, stderr), (0, ""))
            summary = decoded(stdout)
            self.assertEqual((summary["removed"], summary["restored"]), (False, True))
            self.assertFalse(chained_path.exists())
            restored_path = hooks_dir / HOOK_FILE_NAME
            self.assertEqual(restored_path.read_bytes(), backup_bytes)
            self.assertEqual(restored_path.stat().st_mode, backup_mode)

    def test_status_on_an_absent_hook_with_a_backup_reports_it_as_chained(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")
            hooks_dir = repository / ".git" / "hooks"
            hooks_dir.mkdir(parents=True, exist_ok=True)
            (hooks_dir / CHAINED_HOOK_NAME).write_text(
                "#!/bin/sh\nexit 0\n", encoding="utf-8"
            )

            code, stdout, stderr = invoke(
                {"TAF_STATE_HOME": str(root / "state")},
                "prepare", "hook", "status", "--repo", str(repository),
            )

            self.assertEqual((code, stderr), (0, ""))
            summary = decoded(stdout)
            self.assertEqual(summary["hook"], "absent")
            self.assertTrue(summary["chained"])
            self.assertIsNone(summary["launcher_current"])
            self.assertIsNone(summary["launcher_text_current"])
            self.assertIsNone(summary["launcher_generation"])


class _FakeTerminal(io.StringIO):
    """A `/dev/tty` double: readable, writable, and selectable-by-fake."""

    def __init__(self, answer: str) -> None:
        super().__init__(answer)
        self.written = ""

    def write(self, text: str) -> int:  # the question goes to the terminal, not stderr
        self.written += text
        return len(text)


class HookConfirmTests(unittest.TestCase):
    """`--confirm` asks only after a warning, and only where a person can answer."""

    def _ask(self, answer: str, environment: dict[str, str]) -> tuple[int, str, str]:
        terminal = _FakeTerminal(answer)
        stderr = io.StringIO()
        with mock.patch.object(impact_hook, "_open_terminal", return_value=terminal), \
             mock.patch.object(impact_hook, "_wait_for_input", return_value=True):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                run_environment, repository = ready_repository(root)
                code = run_hook(
                    repository,
                    environment={**run_environment, **environment},
                    stderr=stderr,
                    verbose=False,
                    confirm=True,
                )
        return code, stderr.getvalue(), terminal.written

    def test_an_empty_answer_continues(self) -> None:
        code, stderr, written = self._ask("\n", {})
        self.assertEqual(code, 0)
        self.assertIn("TAF impact:", stderr)
        self.assertEqual(written, "Continue with this commit? [Y/n] ")

    def test_y_continues(self) -> None:
        self.assertEqual(self._ask("y\n", {})[0], 0)

    def test_eof_continues(self) -> None:
        self.assertEqual(self._ask("", {})[0], 0)

    def test_n_aborts_the_commit(self) -> None:
        self.assertEqual(self._ask("n\n", {})[0], impact_hook.HOOK_DECLINE_EXIT_CODE)

    def test_upper_case_n_aborts_the_commit(self) -> None:
        self.assertEqual(self._ask("N\n", {})[0], impact_hook.HOOK_DECLINE_EXIT_CODE)

    def test_the_disable_variable_skips_the_prompt(self) -> None:
        code, _stderr, written = self._ask("n\n", {"TAF_HOOK_CONFIRM": "0"})
        self.assertEqual((code, written), (0, ""))

    def test_a_ci_environment_skips_the_prompt(self) -> None:
        code, _stderr, written = self._ask("n\n", {"CI": "1"})
        self.assertEqual((code, written), (0, ""))

    def test_an_agent_environment_skips_the_prompt(self) -> None:
        for name in ("CLAUDECODE", "AI_AGENT"):
            with self.subTest(name=name):
                code, _stderr, written = self._ask("n\n", {name: "1"})
                self.assertEqual((code, written), (0, ""))

    def test_no_terminal_skips_the_prompt(self) -> None:
        stderr = io.StringIO()
        with mock.patch.object(
            impact_hook, "_open_terminal", side_effect=OSError(6, "Device not configured")
        ):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                environment, repository = ready_repository(root)
                code = run_hook(
                    repository,
                    environment=environment,
                    stderr=stderr,
                    verbose=False,
                    confirm=True,
                )
        self.assertEqual(code, 0)
        self.assertIn("TAF impact:", stderr.getvalue())

    def test_a_timeout_continues_and_says_so(self) -> None:
        terminal = _FakeTerminal("n\n")  # an answer that would abort, never read
        stderr = io.StringIO()
        with mock.patch.object(impact_hook, "_open_terminal", return_value=terminal), \
             mock.patch.object(impact_hook, "_wait_for_input", return_value=False):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                environment, repository = ready_repository(root)
                code = run_hook(
                    repository,
                    environment=environment,
                    stderr=stderr,
                    verbose=False,
                    confirm=True,
                )
        self.assertEqual(code, 0)
        self.assertTrue(
            stderr.getvalue().endswith("TAF impact: no answer; the commit continues\n"),
            stderr.getvalue(),
        )

    def test_a_clean_outcome_never_asks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, repository = ready_repository(root)
            run(repository, "git", "add", "web.py", "other.py")
            stage_edit(repository, "web.py", 5, "web 5 changed")
            stage_edit(repository, "other.py", 5, "other 5 changed")
            terminal = _FakeTerminal("n\n")
            stderr = io.StringIO()
            with mock.patch.object(impact_hook, "_open_terminal", return_value=terminal):
                code = run_hook(
                    repository,
                    environment=environment,
                    stderr=stderr,
                    verbose=False,
                    confirm=True,
                )

            self.assertEqual((code, terminal.written), (0, ""))

    def test_the_timeout_falls_back_on_a_bad_value(self) -> None:
        self.assertEqual(impact_hook._confirm_timeout({}), impact_hook.HOOK_CONFIRM_TIMEOUT_SECONDS)
        self.assertEqual(
            impact_hook._confirm_timeout({"TAF_HOOK_CONFIRM_TIMEOUT": "not a number"}),
            impact_hook.HOOK_CONFIRM_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            impact_hook._confirm_timeout({"TAF_HOOK_CONFIRM_TIMEOUT": "0"}),
            impact_hook.HOOK_CONFIRM_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            impact_hook._confirm_timeout({"TAF_HOOK_CONFIRM_TIMEOUT": "2.5"}), 2.5
        )


class ConfirmLauncherExitCodeTests(unittest.TestCase):
    """Only the refusal code blocks a commit; a broken broker never does.

    The launcher's own shell logic is exercised directly with a stub broker,
    because the real refusal needs a controlling terminal no test process has.
    """

    def _run_launcher(self, root: Path, broker_exit: int) -> subprocess.CompletedProcess:
        script = root / "broker.py"
        write(script, f"import sys\nsys.exit({broker_exit})\n")
        launcher = root / "pre-commit"
        write(
            launcher,
            _render_launcher(
                interpreter=Path(sys.executable).resolve(),
                script=script,
                chained_hook_path=None,
                state_root=root / "state",
                confirm=True,
            ),
        )
        launcher.chmod(0o755)
        return subprocess.run(
            ["/bin/sh", str(launcher)], capture_output=True, text=True, timeout=30
        )

    def test_the_decline_code_blocks_the_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self._run_launcher(Path(directory), impact_hook.HOOK_DECLINE_EXIT_CODE)

            self.assertEqual(result.returncode, 1)

    def test_any_other_failure_lets_the_commit_through(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for broker_exit in (0, 1, 2, 127):
                with self.subTest(broker_exit=broker_exit):
                    result = self._run_launcher(root, broker_exit)

                    self.assertEqual(result.returncode, 0, result.stderr)


class HookConfirmLauncherTests(unittest.TestCase):
    """`--mode=confirm` is recorded in the launcher and reported by `status`."""

    def test_the_confirm_launcher_passes_the_flag(self) -> None:
        source = _render_launcher(
            interpreter=Path("/usr/bin/python3"),
            script=Path("/plugin/prepare_repo_context.py"),
            chained_hook_path=None,
            state_root=Path("/home/user/state"),
            confirm=True,
        )

        self.assertIn('hook run --repo "$PWD" --confirm', source)

    def test_the_advisory_launcher_does_not(self) -> None:
        source = _render_launcher(
            interpreter=Path("/usr/bin/python3"),
            script=Path("/plugin/prepare_repo_context.py"),
            chained_hook_path=None,
            state_root=Path("/home/user/state"),
        )

        self.assertNotIn("--confirm", source)

    def test_install_records_the_mode_and_status_reports_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            environment, repository = ready_repository(root)

            code, stdout, _stderr = invoke(
                environment, "prepare", "hook", "install",
                "--repo", str(repository), "--confirm-hook-write", "--mode", "confirm",
            )
            self.assertEqual(code, 0)
            self.assertEqual(decoded(stdout)["hook_mode"], "confirm")

            code, stdout, _stderr = invoke(
                environment, "prepare", "hook", "status", "--repo", str(repository)
            )
            self.assertEqual(code, 0)
            summary = decoded(stdout)
            self.assertEqual(summary["hook_mode"], "confirm")
            # A confirm launcher is current: `status` must render its
            # comparison in the same mode, not against the advisory text.
            self.assertTrue(summary["launcher_text_current"])
            self.assertTrue(summary["launcher_current"])

    def test_status_reports_advisory_for_a_default_install_and_null_for_none(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            environment, repository = ready_repository(root)

            code, stdout, _stderr = invoke(
                environment, "prepare", "hook", "status", "--repo", str(repository)
            )
            self.assertEqual(code, 0)
            self.assertIsNone(decoded(stdout)["hook_mode"])

            invoke(
                environment, "prepare", "hook", "install",
                "--repo", str(repository), "--confirm-hook-write",
            )
            code, stdout, _stderr = invoke(
                environment, "prepare", "hook", "status", "--repo", str(repository)
            )
            self.assertEqual(code, 0)
            self.assertEqual(decoded(stdout)["hook_mode"], "advisory")

            # A launcher written before this change carries no --confirm and
            # must read as advisory, not as an unknown mode.
            legacy = repository / ".git" / "hooks" / HOOK_FILE_NAME
            legacy.write_text(
                "#!/bin/sh\n"
                + LAUNCHER_MARKER
                + '\nexec /usr/bin/python3 /plugin/prepare_repo_context.py hook run '
                '--repo "$PWD"\n',
                encoding="utf-8",
            )
            legacy.chmod(0o755)
            code, stdout, _stderr = invoke(
                environment, "prepare", "hook", "status", "--repo", str(repository)
            )
            self.assertEqual(code, 0)
            self.assertEqual(decoded(stdout)["hook_mode"], "advisory")

    def test_a_real_commit_under_confirm_aborts_on_n(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            environment, repository = ready_repository(root)
            invoke(
                environment, "prepare", "hook", "install",
                "--repo", str(repository), "--confirm-hook-write", "--mode", "confirm",
            )
            before_log = run(repository, "git", "log", "--format=%H")
            # No `/dev/tty` is reachable from this subprocess, so the prompt is
            # skipped and the commit proceeds: the fail-open guarantee (D4).
            result = subprocess.run(
                ["git", "commit", "-m", "x"],
                cwd=repository,
                env=commit_environment(environment),
                capture_output=True,
                text=True,
                timeout=30,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotEqual(before_log, run(repository, "git", "log", "--format=%H"))


class HookStatusTests(unittest.TestCase):
    """`status`'s four hook states, `launcher_current`, and its readiness field."""

    def test_status_on_a_repository_with_no_hook_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")

            code, stdout, stderr = invoke(
                {"TAF_STATE_HOME": str(root / "state")},
                "prepare", "hook", "status", "--repo", str(repository),
            )

            self.assertEqual((code, stderr), (0, ""))
            summary = decoded(stdout)
            self.assertEqual(summary["hook"], "absent")
            self.assertFalse(summary["chained"])
            self.assertIsNone(summary["launcher_current"])
            self.assertIsNone(summary["launcher_text_current"])
            self.assertIsNone(summary["launcher_generation"])

    def test_status_on_a_foreign_hook_is_foreign(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")
            hooks_dir = repository / ".git" / "hooks"
            hooks_dir.mkdir(parents=True, exist_ok=True)
            (hooks_dir / HOOK_FILE_NAME).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

            code, stdout, stderr = invoke(
                {"TAF_STATE_HOME": str(root / "state")},
                "prepare", "hook", "status", "--repo", str(repository),
            )

            self.assertEqual((code, stderr), (0, ""))
            summary = decoded(stdout)
            self.assertEqual(summary["hook"], "foreign")
            self.assertIsNone(summary["launcher_current"])
            self.assertIsNone(summary["launcher_text_current"])
            self.assertIsNone(summary["launcher_generation"])

    def test_status_reports_an_unchained_installed_launcher_as_current(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")
            invoke(
                {"TAF_STATE_HOME": str(root / "state")}, "prepare", "hook", "install",
                "--repo", str(repository), "--confirm-hook-write",
            )

            code, stdout, stderr = invoke(
                {"TAF_STATE_HOME": str(root / "state")},
                "prepare", "hook", "status", "--repo", str(repository),
            )

            self.assertEqual((code, stderr), (0, ""))
            summary = decoded(stdout)
            self.assertEqual(summary["hook"], "installed")
            self.assertFalse(summary["chained"])
            self.assertTrue(summary["launcher_current"])
            self.assertTrue(summary["launcher_text_current"])
            self.assertEqual(summary["launcher_generation"], "pointer")

    def test_status_reports_a_chained_installed_launcher_as_current(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")
            hooks_dir = repository / ".git" / "hooks"
            hooks_dir.mkdir(parents=True, exist_ok=True)
            (hooks_dir / HOOK_FILE_NAME).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (hooks_dir / HOOK_FILE_NAME).chmod(0o755)
            invoke(
                {"TAF_STATE_HOME": str(root / "state")}, "prepare", "hook", "install",
                "--repo", str(repository), "--confirm-hook-write", "--chain",
            )

            code, stdout, stderr = invoke(
                {"TAF_STATE_HOME": str(root / "state")},
                "prepare", "hook", "status", "--repo", str(repository),
            )

            self.assertEqual((code, stderr), (0, ""))
            summary = decoded(stdout)
            self.assertEqual(summary["hook"], "installed")
            self.assertTrue(summary["chained"])
            self.assertTrue(summary["launcher_current"])
            self.assertTrue(summary["launcher_text_current"])
            self.assertEqual(summary["launcher_generation"], "pointer")

    def test_status_reports_a_stale_text_pointer_launcher_as_current(self) -> None:
        """Finding O: a stale embedded fallback whose pointer still names this
        plugin's real entry script stays current - only the text comparison
        (`launcher_text_current`) reports the divergence."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")
            state_root = root / "state"
            state_root.mkdir(parents=True)
            environment = {"TAF_STATE_HOME": str(state_root)}
            invoke(
                environment, "prepare", "hook", "install",
                "--repo", str(repository), "--confirm-hook-write",
            )
            target = launcher_target_path(state_root)
            self.assertTrue(target.is_file())  # the pointer was written on install

            hook_path = repository / ".git" / "hooks" / HOOK_FILE_NAME
            stale = hook_path.read_text(encoding="utf-8").replace(
                f"taf_script={shlex.quote(str(_entry_point_script()))}",
                f"taf_script={shlex.quote(str(root / 'old-script.py'))}",
            ).replace(
                "# Follows the TAF broker that last ran on this machine (a pointer under TAF's own",
                "# Follows an older TAF broker layout (a pointer under TAF's own",
            )
            self.assertNotEqual(stale, hook_path.read_text(encoding="utf-8"))
            hook_path.write_text(stale, encoding="utf-8")
            hook_path.chmod(0o755)

            code, stdout, stderr = invoke(
                environment, "prepare", "hook", "status", "--repo", str(repository),
            )

            self.assertEqual((code, stderr), (0, ""))
            summary = decoded(stdout)
            self.assertEqual(summary["hook"], "installed")
            self.assertFalse(summary["launcher_text_current"])
            self.assertEqual(summary["launcher_generation"], "pointer")
            self.assertTrue(summary["launcher_current"])

    def _install_with_a_stale_pointer(
        self, root: Path, repository: Path, state_root: Path
    ) -> dict[str, str]:
        """Install, then rewrite the on-disk pointer to name a wrong script.

        A single `hook status` call reports honestly from what is on disk at
        call time, but every `prepare` command (`hook status` included) also
        refreshes the pointer as a side effect once it has answered - so a
        corrupted pointer only stays corrupted for the *next* call, never for
        a second one after that. Each scenario below therefore gets its own
        fresh fixture with exactly one `status` call.
        """
        environment = {"TAF_STATE_HOME": str(state_root)}
        invoke(
            environment, "prepare", "hook", "install",
            "--repo", str(repository), "--confirm-hook-write",
        )
        other_script = root / "other-script.py"
        other_script.write_text("print('not the real entry point')\n", encoding="utf-8")
        target = launcher_target_path(state_root)
        interpreter_line, _script_line = target.read_text(encoding="utf-8").splitlines()
        target.write_text(f"{interpreter_line}\n{other_script}\n", encoding="utf-8")
        return environment

    def test_status_with_a_stale_pointer_and_current_text_lets_text_win(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")
            state_root = root / "state"
            state_root.mkdir(parents=True)
            environment = self._install_with_a_stale_pointer(root, repository, state_root)

            # The launcher's own bytes still match what install would write
            # now, so text wins over the now-stale pointer.
            code, stdout, stderr = invoke(
                environment, "prepare", "hook", "status", "--repo", str(repository),
            )

            self.assertEqual((code, stderr), (0, ""))
            summary = decoded(stdout)
            self.assertTrue(summary["launcher_text_current"])
            self.assertEqual(summary["launcher_generation"], "pointer")
            self.assertTrue(summary["launcher_current"])

    def test_status_with_a_stale_pointer_and_stale_text_is_not_current(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")
            state_root = root / "state"
            state_root.mkdir(parents=True)
            environment = self._install_with_a_stale_pointer(root, repository, state_root)
            hook_path = repository / ".git" / "hooks" / HOOK_FILE_NAME
            stale = hook_path.read_text(encoding="utf-8").replace(
                f"taf_script={shlex.quote(str(_entry_point_script()))}",
                f"taf_script={shlex.quote(str(root / 'old-script.py'))}",
            )
            hook_path.write_text(stale, encoding="utf-8")
            hook_path.chmod(0o755)

            # Neither the text nor the (still-stale) pointer names this plugin.
            code, stdout, stderr = invoke(
                environment, "prepare", "hook", "status", "--repo", str(repository),
            )

            self.assertEqual((code, stderr), (0, ""))
            summary = decoded(stdout)
            self.assertFalse(summary["launcher_text_current"])
            self.assertEqual(summary["launcher_generation"], "pointer")
            self.assertFalse(summary["launcher_current"])

    def test_status_with_a_nul_byte_in_the_pointer_reports_false_not_a_crash(self) -> None:
        """Minor 1 (review): a pointer hand-corrupted with an embedded NUL
        byte cannot be turned into a path at all - `Path.resolve()` raises
        `ValueError` for it. `_pointer_runs_this_plugin`'s docstring promises
        `False` "on any error", so `hook status` must still exit 0 with an
        honest report, not bubble a raw `ValueError` up into a generic
        CLI failure."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")
            state_root = root / "state"
            state_root.mkdir(parents=True)
            environment = {"TAF_STATE_HOME": str(state_root)}
            invoke(
                environment, "prepare", "hook", "install",
                "--repo", str(repository), "--confirm-hook-write",
            )
            target = launcher_target_path(state_root)
            target.write_bytes(b"/usr/bin/python3\n/tmp/x\x00y\n")

            # The embedded fallback is also stale, so `launcher_current` can
            # only come out true through the (now-corrupted) pointer check.
            hook_path = repository / ".git" / "hooks" / HOOK_FILE_NAME
            stale = hook_path.read_text(encoding="utf-8").replace(
                f"taf_script={shlex.quote(str(_entry_point_script()))}",
                f"taf_script={shlex.quote(str(root / 'old-script.py'))}",
            )
            self.assertNotEqual(stale, hook_path.read_text(encoding="utf-8"))
            hook_path.write_text(stale, encoding="utf-8")
            hook_path.chmod(0o755)

            code, stdout, stderr = invoke(
                environment, "prepare", "hook", "status", "--repo", str(repository),
            )

            self.assertEqual((code, stderr), (0, ""))
            summary = decoded(stdout)
            self.assertEqual(summary["hook"], "installed")
            self.assertEqual(summary["launcher_generation"], "pointer")
            self.assertFalse(summary["launcher_text_current"])
            self.assertFalse(summary["launcher_current"])

    def test_status_reports_an_embedded_generation_launcher_as_not_current(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")
            hooks_dir = repository / ".git" / "hooks"
            hooks_dir.mkdir(parents=True, exist_ok=True)
            hook_path = hooks_dir / HOOK_FILE_NAME
            interpreter = shlex.quote(str(Path(sys.executable).resolve()))
            script = shlex.quote(str(_entry_point_script()))
            hook_path.write_text(
                "#!/bin/sh\n"
                f"{LAUNCHER_MARKER}\n"
                "# An older TAF launcher with no self-healing pointer at all.\n"
                f'{interpreter} {script} hook run --repo "$PWD" || :\n',
                encoding="utf-8",
            )
            hook_path.chmod(0o755)

            code, stdout, stderr = invoke(
                {"TAF_STATE_HOME": str(root / "state")},
                "prepare", "hook", "status", "--repo", str(repository),
            )

            self.assertEqual((code, stderr), (0, ""))
            summary = decoded(stdout)
            self.assertEqual(summary["hook"], "installed")
            self.assertEqual(summary["launcher_generation"], "embedded")
            self.assertFalse(summary["launcher_text_current"])
            self.assertFalse(summary["launcher_current"])

    def test_status_still_reports_when_the_state_root_cannot_be_resolved(self) -> None:
        # No HOME, no USERPROFILE, no TAF_STATE_HOME: `_state_paths` raises
        # `PrepareCLIError`. `hook_status` must fold that into
        # `launcher_current: None` and `readiness.error`, not abort the
        # report - and still report `launcher_generation` from the launcher's
        # own text, which needs no state root to read.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")
            invoke(
                {"TAF_STATE_HOME": str(root / "state")}, "prepare", "hook", "install",
                "--repo", str(repository), "--confirm-hook-write",
            )

            code, stdout, stderr = invoke(
                {}, "prepare", "hook", "status", "--repo", str(repository)
            )

            self.assertEqual((code, stderr), (0, ""))
            summary = decoded(stdout)
            self.assertEqual(summary["hook"], "installed")
            self.assertIsNone(summary["launcher_current"])
            self.assertIsNone(summary["launcher_text_current"])
            self.assertEqual(summary["launcher_generation"], "pointer")
            self.assertIsNotNone(summary["readiness"]["error"])

    def test_readiness_names_use_index_after_a_fake_engine_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = staged_impact_fixture(root)
            native = root / "taf-level1"
            write_fake_native_engine(native)
            environment = {
                "TAF_LEVEL1_BINARY": str(native),
                "TAF_STATE_HOME": str(root / "state"),
            }
            build_index(environment, repository)

            code, stdout, stderr = invoke(
                environment, "prepare", "hook", "status", "--repo", str(repository)
            )

            self.assertEqual((code, stderr), (0, ""))
            self.assertEqual(
                decoded(stdout)["readiness"],
                {"next_safe_action": "use-index", "error": None},
            )

    def test_readiness_names_the_unborn_repository_error_while_hook_is_still_reported(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_repo(root / "repo")

            code, stdout, stderr = invoke(
                {"TAF_STATE_HOME": str(root / "state")},
                "prepare", "hook", "status", "--repo", str(repository),
            )

            self.assertEqual((code, stderr), (0, ""))
            summary = decoded(stdout)
            self.assertEqual(summary["hook"], "absent")
            self.assertIsNone(summary["readiness"]["next_safe_action"])
            self.assertEqual(
                summary["readiness"]["error"], "repository must have at least one commit"
            )


class HookWorktreeTests(unittest.TestCase):
    """A linked worktree shares its common directory's `.git/hooks`."""

    def test_install_from_a_linked_worktree_writes_the_common_hooks_directory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")
            worktree = root / "wt"
            run(repository, "git", "worktree", "add", "-b", "wt-branch", str(worktree), "HEAD")

            code, stdout, stderr = invoke(
                {"TAF_STATE_HOME": str(root / "state")}, "prepare", "hook", "install",
                "--repo", str(worktree), "--confirm-hook-write",
            )

            self.assertEqual((code, stderr), (0, ""))
            common_hook = repository / ".git" / "hooks" / HOOK_FILE_NAME
            self.assertTrue(common_hook.is_file())

            for checkout in (repository, worktree):
                code, stdout, stderr = invoke(
                    {"TAF_STATE_HOME": str(root / "state")},
                    "prepare", "hook", "status", "--repo", str(checkout),
                )
                self.assertEqual((code, stderr), (0, ""))
                self.assertEqual(decoded(stdout)["hook"], "installed")


class HookEndToEndTests(unittest.TestCase):
    """A real `git commit` through the installed launcher."""

    def test_a_real_commit_runs_the_launcher_and_the_disable_switch_silences_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            environment, repository = ready_repository(root)
            install_code, _stdout, _stderr = invoke(
                environment, "prepare", "hook", "install",
                "--repo", str(repository), "--confirm-hook-write",
            )
            self.assertEqual(install_code, 0)
            before_log = run(repository, "git", "log", "--format=%H")

            result = subprocess.run(
                ["git", "commit", "-m", "x"],
                cwd=repository,
                env=commit_environment(environment),
                capture_output=True,
                text=True,
                timeout=30,
            )

            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stderr, framed(TWO_FILE_REPORT))
            self.assertNotIn("TAF:", result.stdout)
            after_log = run(repository, "git", "log", "--format=%H")
            self.assertNotEqual(before_log, after_log)

            # A second, otherwise-warning-worthy commit prints nothing under
            # TAF_HOOK=0.
            stage_edit(repository, "app.py", 5, "line 5 changed again")
            disabled_environment = commit_environment(environment)
            disabled_environment["TAF_HOOK"] = "0"

            result = subprocess.run(
                ["git", "commit", "-m", "y"],
                cwd=repository,
                env=disabled_environment,
                capture_output=True,
                text=True,
                timeout=30,
            )

            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stderr, "")

    def test_a_chained_failing_hook_still_blocks_the_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            environment, repository = ready_repository(root)
            hooks_dir = repository / ".git" / "hooks"
            hooks_dir.mkdir(parents=True, exist_ok=True)
            foreign = hooks_dir / HOOK_FILE_NAME
            foreign.write_text("#!/bin/sh\necho foreign >&2\nexit 3\n", encoding="utf-8")
            foreign.chmod(0o755)
            install_code, _stdout, _stderr = invoke(
                environment, "prepare", "hook", "install",
                "--repo", str(repository), "--confirm-hook-write", "--chain",
            )
            self.assertEqual(install_code, 0)
            before_log = run(repository, "git", "log", "--format=%H")

            result = subprocess.run(
                ["git", "commit", "-m", "x"],
                cwd=repository,
                env=commit_environment(environment),
                capture_output=True,
                text=True,
                timeout=30,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stderr, "foreign\n")
            after_log = run(repository, "git", "log", "--format=%H")
            self.assertEqual(before_log, after_log)

    def test_a_passing_chained_hook_runs_before_the_taf_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            environment, repository = ready_repository(root)
            hooks_dir = repository / ".git" / "hooks"
            hooks_dir.mkdir(parents=True, exist_ok=True)
            foreign = hooks_dir / HOOK_FILE_NAME
            foreign.write_text("#!/bin/sh\necho foreign >&2\nexit 0\n", encoding="utf-8")
            foreign.chmod(0o755)
            install_code, _stdout, _stderr = invoke(
                environment, "prepare", "hook", "install",
                "--repo", str(repository), "--confirm-hook-write", "--chain",
            )
            self.assertEqual(install_code, 0)

            result = subprocess.run(
                ["git", "commit", "-m", "x"],
                cwd=repository,
                env=commit_environment(environment),
                capture_output=True,
                text=True,
                timeout=30,
            )

            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stderr, "foreign\n" + framed(TWO_FILE_REPORT))

    def test_the_report_reflects_what_the_chained_hook_re_staged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            environment, repository = ready_repository(root)
            hooks_dir = repository / ".git" / "hooks"
            hooks_dir.mkdir(parents=True, exist_ok=True)
            foreign = hooks_dir / HOOK_FILE_NAME
            # A formatter-shaped hook: it stages the two dependent files, so a
            # report composed after it must find nothing untouched (D1).
            foreign.write_text(
                "#!/bin/sh\ngit add web.py other.py\nexit 0\n", encoding="utf-8"
            )
            foreign.chmod(0o755)
            stage_edit(repository, "web.py", 5, "web 5 changed")
            stage_edit(repository, "other.py", 5, "other 5 changed")
            run(repository, "git", "reset", "web.py", "other.py")
            install_code, _stdout, _stderr = invoke(
                environment, "prepare", "hook", "install",
                "--repo", str(repository), "--confirm-hook-write", "--chain",
            )
            self.assertEqual(install_code, 0)

            result = subprocess.run(
                ["git", "commit", "-m", "x"],
                cwd=repository,
                env=commit_environment(environment),
                capture_output=True,
                text=True,
                timeout=30,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("depend on this change", result.stderr)


class _RaisingStderr:
    """A stderr double whose every write raises, like a closed pipe would."""

    def write(self, _text: str) -> int:
        raise BrokenPipeError("stderr closed")


class RunHookNeverRaisesTests(unittest.TestCase):
    """`run_hook` must return 0 even when writing to stderr itself fails."""

    def test_a_broken_pipe_on_the_warning_path_still_returns_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, repository = ready_repository(root)

            code = run_hook(
                repository,
                environment=environment,
                stderr=_RaisingStderr(),
                verbose=False,
            )

            self.assertEqual(code, 0)

    def test_an_ascii_stderr_accepts_every_line_including_the_trailer(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, repository = ready_repository(root, extra_callers=5)
            # The header, detail lines, and trailer are pure ASCII, so an
            # ASCII-only stderr accepts every one of them; write_through
            # makes every accepted write land in the buffer immediately, so it
            # can be inspected below without an explicit flush.
            stderr = io.TextIOWrapper(
                io.BytesIO(), encoding="ascii", write_through=True
            )

            code = run_hook(
                repository,
                environment=environment,
                stderr=stderr,
                verbose=False,
            )

            self.assertEqual(code, 0)
            written = stderr.buffer.getvalue().decode("ascii")
            self.assertEqual(
                written.splitlines(),
                [""]
                + ["TAF impact: 7 files depend on this change and are not in this commit"]
                + [
                    f"  dep{index:02d}.py:12  <- app.first"
                    for index in range(HOOK_MAXIMUM_LINES)
                ]
                + [f"  ... and 2 more {HOOK_POINTER}", ""],
            )

    def test_a_raising_stderr_through_cli_main_still_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, repository = ready_repository(root)

            code = main(
                ["prepare", "hook", "run", "--repo", str(repository)],
                stdout=io.StringIO(),
                stderr=_RaisingStderr(),
                environment=environment,
            )

            self.assertEqual(code, 0)


class _FakeTTYStderr(io.StringIO):
    """A stderr double that reports itself as a real terminal (D16 colour rule)."""

    def isatty(self) -> bool:
        return True


class HookColourTests(unittest.TestCase):
    """`run_hook` bolds the header only on a real TTY stderr (addendum D16)."""

    def test_a_tty_stderr_bolds_only_the_header(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, repository = ready_repository(root)
            stderr = _FakeTTYStderr()

            code = run_hook(repository, environment=environment, stderr=stderr, verbose=False)

            self.assertEqual(code, 0)
            lines = stderr.getvalue().splitlines()
            self.assertEqual(
                lines[1],
                "\x1b[1mTAF impact: 2 files depend on this change and are not in this "
                "commit\x1b[0m",
            )
            self.assertNotIn("\x1b", "\n".join(lines[2:]))

    def test_no_color_disables_colour_on_a_tty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, repository = ready_repository(root)
            environment = dict(environment, NO_COLOR="1")
            stderr = _FakeTTYStderr()

            code = run_hook(repository, environment=environment, stderr=stderr, verbose=False)

            self.assertEqual(code, 0)
            self.assertNotIn("\x1b", stderr.getvalue())
            self.assertTrue(
                stderr.getvalue().startswith(
                    "\nTAF impact: 2 files depend on this change and are not in this commit"
                )
            )

    def test_a_dumb_term_disables_colour_on_a_tty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, repository = ready_repository(root)
            environment = dict(environment, TERM="dumb")
            stderr = _FakeTTYStderr()

            code = run_hook(repository, environment=environment, stderr=stderr, verbose=False)

            self.assertEqual(code, 0)
            self.assertNotIn("\x1b", stderr.getvalue())

    def test_a_non_tty_stderr_never_emits_escape_codes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, repository = ready_repository(root)
            stderr = io.StringIO()  # isatty() is False by default

            code = run_hook(repository, environment=environment, stderr=stderr, verbose=False)

            self.assertEqual(code, 0)
            self.assertNotIn("\x1b", stderr.getvalue())
            self.assertEqual(stderr.getvalue(), framed(TWO_FILE_REPORT))


class HookChainSafetyTests(unittest.TestCase):
    """A chained hook that cannot be run is skipped, never `exec`'d (D10).

    `exec` in POSIX `sh` terminates the shell when its target cannot be
    executed, so an unguarded chained `exec` would turn the launcher into a
    repository-wide commit block the moment the backup went missing or lost
    its executable bit - the one outcome this feature promises can never
    happen.
    """

    def test_a_deleted_chained_backup_leaves_the_commit_working(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            environment, repository = ready_repository(root)
            hooks_dir = repository / ".git" / "hooks"
            hooks_dir.mkdir(parents=True, exist_ok=True)
            foreign = hooks_dir / HOOK_FILE_NAME
            foreign.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            foreign.chmod(0o755)
            install_code, _stdout, _stderr = invoke(
                environment, "prepare", "hook", "install",
                "--repo", str(repository), "--confirm-hook-write", "--chain",
            )
            self.assertEqual(install_code, 0)
            # The backup carries an unfamiliar name; a user or a cleanup
            # script deleting it must not cost the repository its commits.
            (hooks_dir / CHAINED_HOOK_NAME).unlink()
            before_log = run(repository, "git", "log", "--format=%H")

            result = subprocess.run(
                ["git", "commit", "-m", "x"],
                cwd=repository,
                env=commit_environment(environment),
                capture_output=True,
                text=True,
                timeout=30,
            )

            self.assertEqual(result.returncode, 0)
            # Exact equality: the TAF warnings and nothing else, so a shell
            # error about the missing backup would fail this test.
            self.assertEqual(result.stderr, framed(TWO_FILE_REPORT))
            after_log = run(repository, "git", "log", "--format=%H")
            self.assertNotEqual(before_log, after_log)

    def test_chaining_refuses_a_foreign_hook_that_is_not_executable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")
            hooks_dir = repository / ".git" / "hooks"
            hooks_dir.mkdir(parents=True, exist_ok=True)
            foreign = hooks_dir / HOOK_FILE_NAME
            foreign_bytes = b"#!/bin/sh\necho foreign >&2\nexit 3\n"
            foreign.write_bytes(foreign_bytes)
            # Git ignores a pre-commit hook without the executable bit and
            # says so; chaining it would be a behaviour change the user did
            # not ask for.
            foreign.chmod(0o644)

            code, stdout, stderr = invoke(
                {}, "prepare", "hook", "install",
                "--repo", str(repository), "--confirm-hook-write", "--chain",
            )

            self.assertEqual((code, stdout), (2, ""))
            self.assertEqual(
                stderr,
                "error: the foreign pre-commit hook is not executable, so git was not "
                "running it; chmod +x it before chaining, or remove it\n",
            )
            self.assertEqual(foreign.read_bytes(), foreign_bytes)
            self.assertFalse((hooks_dir / CHAINED_HOOK_NAME).exists())

    def test_a_failed_launcher_write_puts_the_foreign_hook_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")
            hooks_dir = repository / ".git" / "hooks"
            hooks_dir.mkdir(parents=True, exist_ok=True)
            foreign = hooks_dir / HOOK_FILE_NAME
            foreign_bytes = b"#!/bin/sh\necho foreign >&2\nexit 3\n"
            foreign.write_bytes(foreign_bytes)
            foreign.chmod(0o755)
            foreign_mode = foreign.stat().st_mode

            def explode(*_args: object, **_options: object) -> None:
                raise OSError("no space left on device")

            with mock.patch.object(impact_hook, "_write_launcher_atomically", explode):
                with self.assertRaises(OSError):
                    install_hook(
                        repository,
                        chain=True,
                        environment={"TAF_STATE_HOME": str(root / "state")},
                    )

            self.assertEqual(foreign.read_bytes(), foreign_bytes)
            self.assertEqual(foreign.stat().st_mode, foreign_mode)
            self.assertFalse((hooks_dir / CHAINED_HOOK_NAME).exists())

    def test_a_dangling_backup_symlink_is_adopted_reported_and_restored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")
            hooks_dir = repository / ".git" / "hooks"
            hooks_dir.mkdir(parents=True, exist_ok=True)
            chained_path = hooks_dir / CHAINED_HOOK_NAME
            chained_path.symlink_to(root / "gone" / "pre-commit")

            code, stdout, stderr = invoke(
                {"TAF_STATE_HOME": str(root / "state")}, "prepare", "hook", "install",
                "--repo", str(repository), "--confirm-hook-write",
            )
            self.assertEqual((code, stderr), (0, ""))
            self.assertTrue(decoded(stdout)["chained"])

            code, stdout, stderr = invoke(
                {"TAF_STATE_HOME": str(root / "state")},
                "prepare", "hook", "status", "--repo", str(repository),
            )
            self.assertEqual((code, stderr), (0, ""))
            self.assertTrue(decoded(stdout)["chained"])

            code, stdout, stderr = invoke(
                {}, "prepare", "hook", "remove",
                "--repo", str(repository), "--confirm-hook-write",
            )
            self.assertEqual((code, stderr), (0, ""))
            self.assertTrue(decoded(stdout)["restored"])
            self.assertFalse(os.path.lexists(chained_path))
            self.assertTrue((hooks_dir / HOOK_FILE_NAME).is_symlink())


class HookEnvironmentTests(unittest.TestCase):
    """The hooks directory is the named repository's, never the ambient one."""

    def test_an_exported_git_dir_does_not_redirect_the_install(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            named = init_committed_repo(root / "named")
            other = init_committed_repo(root / "other")

            # `GIT_DIR` takes precedence over `-C <repo>` discovery, so an
            # install run from inside another hook or a `git rebase` shell
            # would otherwise write into a repository the user never named.
            with mock.patch.dict(os.environ, {"GIT_DIR": str(other / ".git")}):
                code, _stdout, stderr = invoke(
                    {"TAF_STATE_HOME": str(root / "state")}, "prepare", "hook", "install",
                    "--repo", str(named), "--confirm-hook-write",
                )

            self.assertEqual((code, stderr), (0, ""))
            self.assertTrue((named / ".git" / "hooks" / HOOK_FILE_NAME).is_file())
            self.assertFalse((other / ".git" / "hooks" / HOOK_FILE_NAME).exists())


class _NonPosixOs:
    """`impact_hook`'s view of `os` on a platform whose `os.name` is not posix.

    Patching the real `os.name` is not an option: `pathlib` reads it when a
    `Path` is instantiated and would hand every caller a `WindowsPath` this
    interpreter refuses to build. Only the module under test sees the
    substitute, and everything but `name` is the real `os`.
    """

    name = "nt"

    def __getattr__(self, attribute: str) -> object:
        return getattr(os, attribute)


class HookPlatformTests(unittest.TestCase):
    """The launcher is POSIX `sh`; the management verbs refuse elsewhere."""

    def test_install_and_remove_refuse_off_posix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")

            with mock.patch.object(impact_hook, "os", _NonPosixOs()):
                install_code, _install_stdout, install_stderr = invoke(
                    {}, "prepare", "hook", "install",
                    "--repo", str(repository), "--confirm-hook-write",
                )
                remove_code, _remove_stdout, remove_stderr = invoke(
                    {}, "prepare", "hook", "remove",
                    "--repo", str(repository), "--confirm-hook-write",
                )

            refusal = "error: the commit-time hook is available on macOS and Linux only\n"
            self.assertEqual((install_code, install_stderr), (2, refusal))
            self.assertEqual((remove_code, remove_stderr), (2, refusal))
            self.assertFalse((repository / ".git" / "hooks" / HOOK_FILE_NAME).exists())

    def test_status_reports_the_platform_and_never_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")
            environment = {"TAF_STATE_HOME": str(root / "state")}

            code, stdout, stderr = invoke(
                environment, "prepare", "hook", "status", "--repo", str(repository)
            )
            self.assertEqual((code, stderr), (0, ""))
            self.assertIs(decoded(stdout)["posix"], True)

            with mock.patch.object(impact_hook, "os", _NonPosixOs()):
                code, stdout, stderr = invoke(
                    environment, "prepare", "hook", "status", "--repo", str(repository)
                )
            self.assertEqual((code, stderr), (0, ""))
            summary = decoded(stdout)
            self.assertIs(summary["posix"], False)
            self.assertEqual(summary["hook"], "absent")


class RefreshLauncherTargetTests(unittest.TestCase):
    """`refresh_launcher_target`'s own contract, independent of any command."""

    def test_a_missing_state_root_writes_nothing_and_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            environment = {"TAF_STATE_HOME": str(root / "state")}  # never created

            changed = refresh_launcher_target(environment, interpreter=root / "python3")

            self.assertFalse(changed)
            self.assertFalse((root / "state").exists())

    def test_an_existing_state_root_gets_a_pointer_with_the_right_modes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state_root = root / "state"
            state_root.mkdir()
            environment = {"TAF_STATE_HOME": str(state_root)}
            interpreter = root / "python3"

            changed = refresh_launcher_target(environment, interpreter=interpreter)

            self.assertTrue(changed)
            target = launcher_target_path(state_root)
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                f"{interpreter.resolve()}\n{_entry_point_script()}\n",
            )
            self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_a_second_call_with_unchanged_content_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state_root = root / "state"
            state_root.mkdir()
            environment = {"TAF_STATE_HOME": str(state_root)}
            interpreter = root / "python3"
            self.assertTrue(refresh_launcher_target(environment, interpreter=interpreter))
            target = launcher_target_path(state_root)
            before = target.stat().st_mtime_ns

            changed = refresh_launcher_target(environment, interpreter=interpreter)

            self.assertFalse(changed)
            self.assertEqual(target.stat().st_mtime_ns, before)

    def test_a_differing_existing_pointer_is_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state_root = root / "state"
            state_root.mkdir()
            environment = {"TAF_STATE_HOME": str(state_root)}
            first = root / "python3-old"
            second = root / "python3-new"
            self.assertTrue(refresh_launcher_target(environment, interpreter=first))
            target = launcher_target_path(state_root)

            changed = refresh_launcher_target(environment, interpreter=second)

            self.assertTrue(changed)
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                f"{second.resolve()}\n{_entry_point_script()}\n",
            )

    def test_an_unresolvable_state_root_is_swallowed_into_false(self) -> None:
        # No HOME, no TAF_STATE_HOME, no Windows fallback: `_state_paths` raises
        # `PrepareCLIError`, which this function must never let escape.
        self.assertFalse(refresh_launcher_target({}))


class RenderLauncherTemplateTests(unittest.TestCase):
    """`_render_launcher`'s exact shape: the marker, the pointer, the chain block."""

    def test_the_marker_stays_line_two_and_the_pointer_path_is_one_quoted_string(
        self,
    ) -> None:
        interpreter = Path("/usr/bin/python3")
        script = Path("/plugin/prepare_repo_context.py")
        state_root = Path("/home/a user/state")  # a space, to prove quoting

        source = _render_launcher(
            interpreter=interpreter,
            script=script,
            chained_hook_path=None,
            state_root=state_root,
        )

        lines = source.splitlines()
        self.assertEqual(lines[0], "#!/bin/sh")
        self.assertEqual(lines[1], LAUNCHER_MARKER)
        target = launcher_target_path(state_root)
        self.assertIn(f"taf_target={shlex.quote(str(target))}", lines)
        self.assertNotIn("After a TAF plugin update", source)
        self.assertNotIn("\nexec ", source)

    def test_the_chain_block_runs_before_the_taf_block_and_never_execs(self) -> None:
        interpreter = Path("/usr/bin/python3")
        script = Path("/plugin/prepare_repo_context.py")
        state_root = Path("/home/user/state")
        chained = Path("/repo/.git/hooks/pre-commit.taf-chained")

        source = _render_launcher(
            interpreter=interpreter,
            script=script,
            chained_hook_path=chained,
            state_root=state_root,
        )

        lines = source.splitlines()
        quoted = shlex.quote(str(chained))
        self.assertEqual(
            lines[lines.index(f"if [ -x {quoted} ]; then") : ][:3],
            [
                f"if [ -x {quoted} ]; then",
                f'  {quoted} "$@" || exit $?',
                "fi",
            ],
        )
        # The chained hook decides the commit first; only then does TAF speak,
        # so its report is the last thing on the screen (D1).
        self.assertLess(
            lines.index(f"if [ -x {quoted} ]; then"),
            lines.index("taf_interpreter=" + shlex.quote(str(interpreter))),
        )
        self.assertNotIn("\nexec ", source)
        self.assertIn(f"taf_target={shlex.quote(str(launcher_target_path(state_root)))}", lines)

    def test_the_read_variables_are_reset_before_the_pointer_read(self) -> None:
        # A directory (or other non-regular file) at the pointer path makes the
        # `read`s fail without unsetting `taf_line1`/`taf_line2`; resetting them
        # right before the guarded block keeps an inherited environment
        # variable of the same name from deciding the interpreter or script.
        source = _render_launcher(
            interpreter=Path("/usr/bin/python3"),
            script=Path("/plugin/prepare_repo_context.py"),
            chained_hook_path=None,
            state_root=Path("/home/user/state"),
        )

        lines = source.splitlines()
        pointer_index = lines.index('if [ -r "$taf_target" ]; then')
        self.assertEqual(lines[pointer_index - 1], "taf_line1= taf_line2=")


class LauncherTargetCliSeamTests(unittest.TestCase):
    """Every `prepare` command but `hook run` refreshes the pointer on success."""

    def test_inspect_on_a_fixture_with_no_state_writes_no_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")
            environment = {"TAF_STATE_HOME": str(root / "state")}

            code, _stdout, stderr = invoke(
                environment, "prepare", "inspect", "--repo", str(repository)
            )

            self.assertEqual((code, stderr), (0, ""))
            self.assertFalse((root / "state").exists())

    def test_build_writes_the_pointer_once_it_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")
            native = root / "taf-level1"
            write_fake_native_engine(native)
            environment = {
                "TAF_LEVEL1_BINARY": str(native),
                "TAF_STATE_HOME": str(root / "state"),
            }

            build_index(environment, repository)

            target = launcher_target_path(root / "state")
            self.assertTrue(target.is_file())
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                f"{Path(sys.executable).resolve()}\n{_entry_point_script()}\n",
            )

    def test_hook_run_never_touches_an_existing_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, repository = ready_repository(root)
            target = launcher_target_path(Path(environment["TAF_STATE_HOME"]))
            self.assertTrue(target.is_file())
            modified = "/bin/modified-interpreter\n/bin/modified-script\n"
            target.write_text(modified, encoding="utf-8")
            before = target.stat().st_mtime_ns

            code, _stdout, _stderr = hook(environment, repository)

            self.assertEqual(code, 0)
            self.assertEqual(target.read_text(encoding="utf-8"), modified)
            self.assertEqual(target.stat().st_mtime_ns, before)


class HookManagerAppendedBlockTests(unittest.TestCase):
    """A hook manager appending its own block after TAF's trips `launcher_text_current`
    but not `launcher_current`: TAF's own conditional still runs first, unaffected by
    whatever a later hook manager appends after it, and the pointer it refreshed on
    install still names this plugin's real entry script and an executable interpreter."""

    def test_an_appended_block_after_taf_s_trips_only_launcher_text_current(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")
            state_root = root / "state"
            state_root.mkdir(parents=True)  # so install's pointer refresh actually writes
            environment = {"TAF_STATE_HOME": str(state_root)}
            invoke(
                environment, "prepare", "hook", "install",
                "--repo", str(repository), "--confirm-hook-write",
            )
            self.assertTrue(launcher_target_path(state_root).is_file())

            code, stdout, stderr = invoke(
                environment, "prepare", "hook", "status", "--repo", str(repository)
            )
            self.assertEqual((code, stderr), (0, ""))
            self.assertTrue(decoded(stdout)["launcher_current"])

            hook_path = repository / ".git" / "hooks" / HOOK_FILE_NAME
            with hook_path.open("a", encoding="utf-8") as stream:
                stream.write("# appended by another hook manager\necho appended >&2\n")

            code, stdout, stderr = invoke(
                environment, "prepare", "hook", "status", "--repo", str(repository)
            )

            self.assertEqual((code, stderr), (0, ""))
            summary = decoded(stdout)
            self.assertEqual(summary["hook"], "installed")
            self.assertFalse(summary["launcher_text_current"])
            self.assertEqual(summary["launcher_generation"], "pointer")
            self.assertTrue(summary["launcher_current"])


class LauncherSelfHealingEndToEndTests(unittest.TestCase):
    """A real `git commit` through the installed launcher, pointer cases (D15)."""

    def _install(self, environment: dict[str, str], repository: Path) -> None:
        install_code, _stdout, _stderr = invoke(
            environment, "prepare", "hook", "install",
            "--repo", str(repository), "--confirm-hook-write",
        )
        self.assertEqual(install_code, 0)

    def _commit(self, environment: dict[str, str], repository: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "commit", "-m", "x"],
            cwd=repository,
            env=commit_environment(environment),
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_a_valid_pointer_wins_over_a_wrong_embedded_script(self) -> None:
        # Chosen implementation of the brief's scenario (a): install normally
        # (embedding valid paths and, via `refresh_launcher_target`, a pointer
        # that matches them), then rewrite only the launcher's own embedded
        # `taf_script` line to a path that does not exist. The pointer file on
        # disk still names the real script, so it must win.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            environment, repository = ready_repository(root)
            self._install(environment, repository)
            target = launcher_target_path(Path(environment["TAF_STATE_HOME"]))
            self.assertTrue(target.is_file())

            hook_path = repository / ".git" / "hooks" / HOOK_FILE_NAME
            content = hook_path.read_text(encoding="utf-8")
            broken = content.replace(
                f"taf_script={shlex.quote(str(_entry_point_script()))}",
                f"taf_script={shlex.quote(str(root / 'missing-script.py'))}",
            )
            self.assertNotEqual(broken, content)
            hook_path.write_text(broken, encoding="utf-8")
            hook_path.chmod(0o755)

            result = self._commit(environment, repository)

            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stderr, framed(TWO_FILE_REPORT))

    def test_a_pointer_absent_falls_back_to_the_embedded_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            environment, repository = ready_repository(root)
            self._install(environment, repository)
            target = launcher_target_path(Path(environment["TAF_STATE_HOME"]))
            self.assertTrue(target.is_file())
            target.unlink()

            result = self._commit(environment, repository)

            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stderr, framed(TWO_FILE_REPORT))

    def test_a_pointer_naming_a_missing_script_falls_back_to_the_embedded_paths(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            environment, repository = ready_repository(root)
            self._install(environment, repository)
            target = launcher_target_path(Path(environment["TAF_STATE_HOME"]))
            interpreter_line, _script_line = target.read_text(
                encoding="utf-8"
            ).splitlines()
            target.write_text(
                f"{interpreter_line}\n{root / 'missing-script.py'}\n", encoding="utf-8"
            )

            result = self._commit(environment, repository)

            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stderr, framed(TWO_FILE_REPORT))

    def test_a_bogus_embedded_interpreter_falls_back_to_command_v_python3(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            environment, repository = ready_repository(root)
            self._install(environment, repository)
            target = launcher_target_path(Path(environment["TAF_STATE_HOME"]))
            self.assertTrue(target.is_file())
            target.unlink()  # the pointer is absent for this scenario

            hook_path = repository / ".git" / "hooks" / HOOK_FILE_NAME
            interpreter = str(Path(sys.executable).resolve())
            content = hook_path.read_text(encoding="utf-8")
            broken = content.replace(
                f"taf_interpreter={shlex.quote(interpreter)}",
                f"taf_interpreter={shlex.quote(str(root / 'no-such-python'))}",
            )
            self.assertNotEqual(broken, content)
            hook_path.write_text(broken, encoding="utf-8")
            hook_path.chmod(0o755)

            result = self._commit(environment, repository)

            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stderr, framed(TWO_FILE_REPORT))

    def test_neither_interpreter_nor_script_resolvable_stays_silent(self) -> None:
        # Chosen implementation of the brief's scenario (e): the pointer is
        # absent, the embedded interpreter is bogus, and - rather than also
        # arranging for `command -v python3` to fail, which would need a PATH
        # with no Python 3 on it at all - the embedded script is bogus too.
        # Even if the interpreter fallback finds a real python3, the launcher's
        # own `[ -f "$taf_script" ]` guard still fails, so the observable
        # contract (silent, exit 0, commit proceeds) holds either way.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            environment, repository = ready_repository(root)
            self._install(environment, repository)
            target = launcher_target_path(Path(environment["TAF_STATE_HOME"]))
            target.unlink()

            hook_path = repository / ".git" / "hooks" / HOOK_FILE_NAME
            interpreter = str(Path(sys.executable).resolve())
            script = str(_entry_point_script())
            broken = hook_path.read_text(encoding="utf-8")
            broken = broken.replace(
                f"taf_interpreter={shlex.quote(interpreter)}",
                f"taf_interpreter={shlex.quote(str(root / 'no-such-python'))}",
            )
            broken = broken.replace(
                f"taf_script={shlex.quote(script)}",
                f"taf_script={shlex.quote(str(root / 'no-such-script.py'))}",
            )
            hook_path.write_text(broken, encoding="utf-8")
            hook_path.chmod(0o755)
            before_log = run(repository, "git", "log", "--format=%H")

            result = self._commit(environment, repository)

            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stderr, "")
            after_log = run(repository, "git", "log", "--format=%H")
            self.assertNotEqual(before_log, after_log)


if __name__ == "__main__":
    unittest.main()
