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
    HOOK_TIME_LIMIT_SECONDS,
    LAUNCHER_MARKER,
    _entry_point_script,
    _hook_query,
    format_summary_line,
    format_warning_line,
    install_hook,
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


WEB_LINE = "TAF: app.first changed; web.py:12 depends on it and is not in this commit\n"
OTHER_LINE = "TAF: app changed; other.py:12 depends on it and is not in this commit\n"


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
            self.assertEqual(stderr, OTHER_LINE + WEB_LINE)

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
            self.assertEqual(stderr, OTHER_LINE)

    def test_a_commit_that_carries_every_dependent_warns_about_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, repository = self._ready_repository(root)
            stage_edit(repository, "web.py", 18, "web 18 changed")
            stage_edit(repository, "other.py", 3, "other 3 changed")

            code, stdout, stderr = hook(environment, repository)

            self.assertEqual((code, stdout, stderr), (0, "", ""))
            code, stdout, stderr = hook(environment, repository, "--verbose")
            self.assertEqual((code, stdout), (0, ""))
            self.assertEqual(stderr, "TAF hook: no untouched dependents\n")

    def test_an_unstaged_edit_is_not_part_of_the_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, repository = self._ready_repository(root)
            run(repository, "git", "reset")
            edit_line(repository / "app.py", 5, "line 5 changed again")

            code, stdout, stderr = hook(environment, repository)

            self.assertEqual((code, stdout, stderr), (0, "", ""))

    def test_more_dependents_than_the_cap_end_with_a_summary_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, repository = self._ready_repository(root, extra_callers=5)

            code, stdout, stderr = hook(environment, repository)

            self.assertEqual((code, stdout), (0, ""))
            self.assertEqual(
                stderr.splitlines(),
                [
                    f"TAF: app.first changed; dep{index:02d}.py:12 depends on it"
                    " and is not in this commit"
                    for index in range(HOOK_MAXIMUM_LINES)
                ]
                + [
                    "TAF: ... and 2 more "
                    "(query impact-candidates --staged for more)"
                ],
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
            self.assertEqual(len(lines), HOOK_MAXIMUM_LINES)
            self.assertNotIn("more (query", stderr)
            self.assertEqual(
                lines,
                [
                    f"TAF: app.first changed; dep{index:02d}.py:12 depends on it"
                    " and is not in this commit"
                    for index in range(3)
                ]
                + [OTHER_LINE.rstrip("\n"), WEB_LINE.rstrip("\n")],
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

    def test_a_warning_line_names_the_strongest_anchor_and_the_reference(self) -> None:
        self.assertEqual(
            format_warning_line(self._candidate("web.py", "verified")),
            "TAF: app.first changed; web.py:12 depends on it and is not in this commit",
        )

    def test_the_summary_line_names_the_query_and_never_overclaims(self) -> None:
        # Exact: the engine omitted nothing, so the count is trusted as is.
        self.assertEqual(
            format_summary_line(2),
            "TAF: ... and 2 more "
            "(query impact-candidates --staged for more)",
        )
        # Lower bound: the engine omitted candidates in some direction, so
        # the count is marked as a floor rather than asserted exact.
        self.assertEqual(
            format_summary_line(2, truncated=True),
            "TAF: ... and 2+ more "
            "(query impact-candidates --staged for more)",
        )
        # Five or fewer files remain (here, none and a negative remainder)
        # but the result is still truncated: nothing exact is left to name.
        self.assertEqual(
            format_summary_line(0, truncated=True),
            "TAF: ... and possibly more "
            "(query impact-candidates --staged for more)",
        )
        self.assertEqual(
            format_summary_line(-3, truncated=True),
            "TAF: ... and possibly more "
            "(query impact-candidates --staged for more)",
        )
        # Never printed and never called with this combination: a summary
        # would have nothing to say and no reason to say it.
        with self.assertRaises(ValueError):
            format_summary_line(0)
        with self.assertRaises(ValueError):
            format_summary_line(-1)


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
            self.assertEqual(len(lines), HOOK_MAXIMUM_LINES + 1)
            self.assertEqual(
                lines[:HOOK_MAXIMUM_LINES],
                [
                    f"TAF: app.first changed; dep{index:02d}.py:12 depends on it"
                    " and is not in this commit"
                    for index in range(HOOK_MAXIMUM_LINES)
                ],
            )
            # 40 dep*.py callers + web.py (a caller) + other.py (an
            # importer) = 42 untouched files; five print, 37 remain.
            self.assertEqual(
                lines[HOOK_MAXIMUM_LINES],
                "TAF: ... and 37 more "
                "(query impact-candidates --staged for more)",
            )

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
            self.assertEqual(len(lines), HOOK_MAXIMUM_LINES + 1)
            self.assertEqual(
                lines[:HOOK_MAXIMUM_LINES],
                [
                    f"TAF: app.first changed; dep{index:02d}.py:12 depends on it"
                    " and is not in this commit"
                    for index in range(HOOK_MAXIMUM_LINES)
                ],
            )
            self.assertEqual(
                lines[HOOK_MAXIMUM_LINES],
                "TAF: ... and 60 more "
                "(query impact-candidates --staged for more)",
            )


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
            self.assertEqual(len(lines), HOOK_MAXIMUM_LINES + 1)
            # 20 dep*.py callers + web.py (a caller) + other.py (an
            # importer) = 22 untouched files; five print, 17 remain, and
            # the engine reported an omission of its own, so the count is
            # marked as a lower bound.
            self.assertEqual(
                lines[HOOK_MAXIMUM_LINES],
                "TAF: ... and 17+ more "
                "(query impact-candidates --staged for more)",
            )

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
            # so the hook still speaks up rather than staying silent about it.
            self.assertEqual(
                stderr,
                OTHER_LINE
                + WEB_LINE
                + "TAF: ... and possibly more "
                "(query impact-candidates --staged for more)\n",
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
                {},
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
                    "chained": False,
                    "chained_hook_path": None,
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

            code1, stdout1, _stderr1 = invoke(
                {}, "prepare", "hook", "install",
                "--repo", str(repository), "--confirm-hook-write",
            )
            first_bytes = hook_path.read_bytes()
            code2, stdout2, _stderr2 = invoke(
                {}, "prepare", "hook", "install",
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

            summary = install_hook(repository, chain=False, interpreter=pinned)

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


class HookChainTests(unittest.TestCase):
    """Chaining a foreign hook, and `remove` restoring it byte-for-byte."""

    def test_chaining_a_foreign_hook_runs_it_after_taf_and_remove_restores_it(
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

            code, stdout, _stderr = invoke(
                {}, "prepare", "hook", "install",
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
            self.assertEqual(
                launcher_lines[-3:],
                [
                    f"if [ -x {shlex.quote(str(chained_path))} ]; then",
                    f'  exec {shlex.quote(str(chained_path))} "$@"',
                    "fi",
                ],
            )

            # A re-install without --chain keeps the launcher chained.
            code, stdout, _stderr = invoke(
                {}, "prepare", "hook", "install",
                "--repo", str(repository), "--confirm-hook-write",
            )
            self.assertEqual(code, 0)
            self.assertTrue(decoded(stdout)["chained"])

            code, stdout, _stderr = invoke(
                {}, "prepare", "hook", "remove",
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
                {}, "prepare", "hook", "install",
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
            self.assertEqual(
                launcher_lines[-3:],
                [
                    f"if [ -x {shlex.quote(str(chained_path))} ]; then",
                    f'  exec {shlex.quote(str(chained_path))} "$@"',
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

    def test_status_reports_an_unchained_installed_launcher_as_current(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")
            invoke(
                {}, "prepare", "hook", "install",
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

    def test_status_reports_a_chained_installed_launcher_as_current(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")
            hooks_dir = repository / ".git" / "hooks"
            hooks_dir.mkdir(parents=True, exist_ok=True)
            (hooks_dir / HOOK_FILE_NAME).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (hooks_dir / HOOK_FILE_NAME).chmod(0o755)
            invoke(
                {}, "prepare", "hook", "install",
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

    def test_status_reports_a_stale_launcher_as_not_current(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = init_committed_repo(root / "repo")
            invoke(
                {}, "prepare", "hook", "install",
                "--repo", str(repository), "--confirm-hook-write",
            )
            hook_path = repository / ".git" / "hooks" / HOOK_FILE_NAME
            stale = hook_path.read_text(encoding="utf-8").replace(
                shlex.quote(str(Path(sys.executable).resolve())),
                shlex.quote(str(root / "old-python")),
            )
            hook_path.write_text(stale, encoding="utf-8")
            hook_path.chmod(0o755)

            code, stdout, stderr = invoke(
                {"TAF_STATE_HOME": str(root / "state")},
                "prepare", "hook", "status", "--repo", str(repository),
            )

            self.assertEqual((code, stderr), (0, ""))
            summary = decoded(stdout)
            self.assertEqual(summary["hook"], "installed")
            self.assertFalse(summary["launcher_current"])

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
                {}, "prepare", "hook", "install",
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
            self.assertEqual(result.stderr, OTHER_LINE + WEB_LINE)
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
            self.assertIn("TAF:", result.stderr)
            self.assertIn("foreign", result.stderr)
            after_log = run(repository, "git", "log", "--format=%H")
            self.assertEqual(before_log, after_log)


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

    def test_an_ascii_stderr_accepts_all_six_lines_including_the_summary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, repository = ready_repository(root, extra_callers=5)
            # The summary line is pure ASCII, so an ASCII-only stderr accepts
            # every line the hook writes, the summary included; write_through
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
                [
                    f"TAF: app.first changed; dep{index:02d}.py:12 depends on it"
                    " and is not in this commit"
                    for index in range(HOOK_MAXIMUM_LINES)
                ]
                + [
                    "TAF: ... and 2 more "
                    "(query impact-candidates --staged for more)"
                ],
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
            self.assertEqual(result.stderr, OTHER_LINE + WEB_LINE)
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
                    install_hook(repository, chain=True)

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
                {}, "prepare", "hook", "install",
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
                    {}, "prepare", "hook", "install",
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


if __name__ == "__main__":
    unittest.main()
