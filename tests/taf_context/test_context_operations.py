"""Tests for the transport seam and the extracted broker operations."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import random
import stat
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

from taf_context import context_operations, level1_models
from taf_context.change_ranges import ChangedPath
from taf_context.context_operations import (
    MAXIMUM_REQUEST_BYTES,
    OVERVIEW_BUDGET_SHARES,
    OVERVIEW_ENGINE_OUTPUT_CHARACTERS,
    PrepareCLIError,
    QueryArguments,
    _merge_overview_languages,
    _overview_rows_folded_to,
    bound_changed_selector,
    compose_impact_candidates,
    default_maximum_results,
    default_output_characters,
    fit_overview_to_budget,
    normalize_change_base,
    run_build,
    run_inspect,
    run_query,
    trim_to_budget,
    validate_query_request,
)
from taf_context.level1_models import Level1Result
from taf_context.native_transport import NativeTransportError, OneShotTransport

from .repo_factory import commit_all, init_committed_repo, run, write
from .test_prepare_cli import fabricate_incompatible_generation, write_fake_native_engine


def _script(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class OneShotTransportTests(unittest.TestCase):
    def test_success_returns_stdout_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = _script(
                Path(directory) / "engine",
                "import sys; sys.stdout.write(sys.stdin.read().upper())\n",
            )
            self.assertEqual(
                OneShotTransport(binary).exchange(b'{"a":1}\n', idempotent=True), b'{"A":1}\n'
            )

    def test_non_zero_exit_is_rejected_with_the_stderr_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = _script(
                Path(directory) / "engine",
                "import sys; sys.stderr.write('invalid-native-level1-request\\n'); sys.exit(2)\n",
            )
            with self.assertRaises(NativeTransportError) as caught:
                OneShotTransport(binary).exchange(b"{}\n", idempotent=True)
            self.assertEqual(
                (caught.exception.reason, caught.exception.detail),
                ("rejected", "invalid-native-level1-request"),
            )

    def test_timeout_and_missing_binary_have_their_own_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = _script(Path(directory) / "engine", "import time; time.sleep(5)\n")
            with self.assertRaises(NativeTransportError) as caught:
                OneShotTransport(binary, timeout_seconds=0.2).exchange(b"{}\n", idempotent=True)
            self.assertEqual(caught.exception.reason, "timeout")
            with self.assertRaises(NativeTransportError) as missing:
                OneShotTransport(Path(directory) / "absent").exchange(b"{}\n", idempotent=True)
            self.assertEqual(missing.exception.reason, "invocation-failed")


class RecordingTransport:
    """Forwards to a fake engine binary and records what crossed the seam."""

    def __init__(self, binary: Path) -> None:
        self.inner = OneShotTransport(binary)
        self.frames: list[tuple[str, bool]] = []
        self.requests: list[dict] = []

    def exchange(self, wire: bytes, *, idempotent: bool) -> bytes:
        request = json.loads(wire)["request"]
        self.frames.append((request["operation"], idempotent))
        self.requests.append(request)
        return self.inner.exchange(wire, idempotent=idempotent)


def change_fixture(root: Path) -> tuple[Path, str]:
    """A repository whose last commit edits line 5 of a 40-line `app.py`."""
    repository = init_committed_repo(root / "repo")
    lines = [f"line {number}" for number in range(1, 41)]
    write(repository / "app.py", "\n".join(lines) + "\n")
    base = commit_all(repository, "base")
    lines[4] = "line 5 changed"
    write(repository / "app.py", "\n".join(lines) + "\n")
    commit_all(repository, "edit")
    return repository, base


class OperationTests(unittest.TestCase):
    def _environment(self, directory: str, binary: Path) -> dict[str, str]:
        return {
            "HOME": directory,
            "PATH": "",
            "TAF_LEVEL1_BINARY": str(binary),
            "TAF_STATE_HOME": str(Path(directory) / "state"),
        }

    def test_build_then_query_cross_the_seam_with_the_idempotency_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = init_committed_repo(Path(directory) / "repo")
            binary = Path(directory) / "engine"
            write_fake_native_engine(binary)
            environment = self._environment(directory, binary)
            transports: list[RecordingTransport] = []

            def transport_for(path: Path) -> RecordingTransport:
                transports.append(RecordingTransport(path))
                return transports[-1]

            built = run_build(repository, environment=environment, transport_for=transport_for)
            self.assertEqual((built["mode"], built["next_safe_action"]), ("build", "use-index"))
            arguments = QueryArguments("search-symbols", "main", (), [], [], [], [], 8, 4000, False)
            queried = run_query(
                repository, arguments, environment=environment, transport_for=transport_for
            )
            self.assertEqual(
                (queried["mode"], queried["operation"], queried["status"]),
                ("query", "search-symbols", "ready"),
            )
            inspected = run_inspect(repository, environment=environment, transport_for=transport_for)
            self.assertEqual((inspected["mode"], inspected["next_safe_action"]), ("inspect", "use-index"))
            frames = [frame for transport in transports for frame in transport.frames]
            self.assertEqual(frames, [("build", False), ("search-symbols", True), ("status", True)])

    def test_an_edit_sends_the_refresh_update_across_the_seam_as_non_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = init_committed_repo(Path(directory) / "repo")
            binary = Path(directory) / "engine"
            write_fake_native_engine(binary)
            environment = self._environment(directory, binary)
            transports: list[RecordingTransport] = []

            def transport_for(path: Path) -> RecordingTransport:
                transports.append(RecordingTransport(path))
                return transports[-1]

            run_build(repository, environment=environment, transport_for=transport_for)
            arguments = QueryArguments("search-symbols", "main", (), [], [], [], [], 8, 4000, False)
            run_query(repository, arguments, environment=environment, transport_for=transport_for)
            write(repository / "tracked.txt", "edited\n")
            refreshed = run_query(
                repository, arguments, environment=environment, transport_for=transport_for
            )
            self.assertTrue(refreshed["refresh"]["performed"])
            frames = [frame for transport in transports for frame in transport.frames]
            self.assertEqual(
                frames,
                [
                    ("build", False),
                    ("search-symbols", True),
                    ("update", False),
                    ("search-symbols", True),
                ],
            )

    def test_only_the_relationship_query_crosses_the_seam_as_schema_two(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = init_committed_repo(Path(directory) / "repo")
            binary = Path(directory) / "engine"
            write_fake_native_engine(binary)
            environment = self._environment(directory, binary)
            transports: list[RecordingTransport] = []

            def transport_for(path: Path) -> RecordingTransport:
                transports.append(RecordingTransport(path))
                return transports[-1]

            run_build(repository, environment=environment, transport_for=transport_for)
            anchor = "sha256:" + "a" * 64
            run_query(
                repository,
                QueryArguments("search-symbols", "main", (), [], [], [], [], 8, 4000, False),
                environment=environment,
                transport_for=transport_for,
            )
            related = run_query(
                repository,
                QueryArguments(
                    "related-symbols", None, (anchor,), [], [], [], [], 8, 4000, False, "callers"
                ),
                environment=environment,
                transport_for=transport_for,
            )

            requests = [request for transport in transports for request in transport.requests]
            by_operation = {request["operation"]: request for request in requests}
            self.assertEqual(by_operation["search-symbols"]["schema_version"], "1")
            self.assertNotIn("direction", by_operation["search-symbols"])
            self.assertEqual(by_operation["build"]["schema_version"], "1")
            self.assertNotIn("direction", by_operation["build"])
            self.assertEqual(by_operation["related-symbols"]["schema_version"], "2")
            self.assertEqual(by_operation["related-symbols"]["direction"], "callers")
            self.assertEqual(by_operation["related-symbols"]["result_identities"], [anchor])
            self.assertIsNone(by_operation["related-symbols"]["query"])
            self.assertEqual(
                related["findings"][0]["relation"], "call"
            )
            self.assertEqual(related["findings"][0]["edge_evidence"], "verified")

    def test_the_change_query_crosses_the_seam_as_schema_three(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, base = change_fixture(root)
            binary = root / "engine"
            write_fake_native_engine(binary)
            environment = self._environment(directory, binary)
            transports: list[RecordingTransport] = []

            def transport_for(path: Path) -> RecordingTransport:
                transports.append(RecordingTransport(path))
                return transports[-1]

            run_build(repository, environment=environment, transport_for=transport_for)
            changed = run_query(
                repository,
                QueryArguments(
                    "changed-symbols", None, (), [], [], [], [], 8, 4000, False, None, base
                ),
                environment=environment,
                transport_for=transport_for,
            )

            requests = [request for transport in transports for request in transport.requests]
            sent = [item for item in requests if item["operation"] == "changed-symbols"][0]
            self.assertEqual(sent["schema_version"], "3")
            self.assertIsNone(sent["direction"])
            self.assertIsNone(sent["query"])
            self.assertEqual(sent["result_identities"], [])
            self.assertEqual(sent["changed_ranges"], [{"path": "app.py", "ranges": [[5, 5]]}])
            self.assertEqual(
                changed["base"],
                {
                    "requested": base,
                    "ref": base,
                    "sha": base,
                    "source": "explicit",
                    "warning": None,
                },
            )
            self.assertEqual(
                [(item["qualified_name"], item["record_kind"]) for item in changed["findings"]],
                [("app", "module"), ("app.first", "definition")],
            )
            # Unlike repository-overview, changed-symbols still carries the
            # four relationship keys on every finding, even though it names no
            # edge (relation and edge_evidence null, the two counters zero).
            for finding in changed["findings"]:
                self.assertIn("relation", finding)
                self.assertIn("edge_evidence", finding)
                self.assertIn("reference_line", finding)
                self.assertIn("reference_count", finding)

    def test_the_overview_query_crosses_the_seam_as_schema_four(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = init_committed_repo(Path(directory) / "repo")
            binary = Path(directory) / "engine"
            write_fake_native_engine(binary)
            environment = self._environment(directory, binary)
            transports: list[RecordingTransport] = []

            def transport_for(path: Path) -> RecordingTransport:
                transports.append(RecordingTransport(path))
                return transports[-1]

            run_build(repository, environment=environment, transport_for=transport_for)
            overview = run_query(
                repository,
                QueryArguments(
                    "repository-overview", None, (), [], [], [], [], 8, 12000, False
                ),
                environment=environment,
                transport_for=transport_for,
            )

            requests = [request for transport in transports for request in transport.requests]
            sent = [item for item in requests if item["operation"] == "repository-overview"][0]
            self.assertEqual(sent["schema_version"], "4")
            # The schema-4 request is the schema-3 key set with both selectors
            # spelled out as null.
            self.assertIsNone(sent["direction"])
            self.assertIsNone(sent["changed_ranges"])
            self.assertIsNone(sent["query"])
            self.assertEqual(sent["result_identities"], [])
            self.assertEqual(sent["filters"]["symbol_kinds"], [])
            self.assertEqual(sent["filters"]["source_types"], [])
            self.assertEqual(
                [group["path_prefix"] for group in overview["groups"]], [".", "web/"]
            )
            self.assertEqual(
                overview["overview"],
                {"root": "", "counted_file_count": 2, "other_group_count": 0},
            )
            self.assertEqual(
                [group["languages"] for group in overview["groups"]],
                [[{"language": "Python", "file_count": 1}]] * 2,
            )
            self.assertEqual(
                [finding["path"] for finding in overview["findings"]],
                ["app.py", "web/handler.py"],
            )
            self.assertEqual(overview["operation"], "repository-overview")
            self.assertEqual(overview["status"], "ready")
            self.assertNotIn("base", overview)
            # The overview names no relationship, so its findings carry the
            # twelve base finding keys only; the fake engine's wire answer
            # carries the four relationship keys underneath (empty, as every
            # non-relationship result does), and the broker drops them here.
            for finding in overview["findings"]:
                self.assertEqual(
                    set(finding),
                    {
                        "rank", "result_identity", "path", "start_line", "end_line",
                        "language", "record_kind", "source_type", "qualified_name",
                        "extraction_method", "evidence_class", "preview",
                    },
                )

    def test_default_maximum_results_is_per_operation(self) -> None:
        # repository-overview lists directories as well as files, so an
        # unbudgeted request wants more than the shared 8's worth of files to
        # get a feel for a whole repository; every other operation is
        # unaffected.
        self.assertEqual(default_maximum_results("repository-overview"), 24)
        for operation in (
            "repository-map", "search-symbols", "search-docs", "source-snippets",
            "related-symbols", "changed-symbols", "impact-candidates",
        ):
            self.assertEqual(default_maximum_results(operation), 8)
        # The output-budget table this mirrors is untouched by this change.
        self.assertEqual(default_output_characters("repository-overview"), 8000)

    def test_the_overview_always_asks_the_engine_for_the_widest_answer(self) -> None:
        # The engine fits its own answer to the budget it is given, and it
        # measures rendered lines rather than the object the broker sends, so
        # a small budget passed straight through made the engine drop findings
        # the broker's own two-layer rule could still have afforded - an 8000
        # character request answering with four of eight candidates. The
        # overview therefore asks for the widest answer the wire allows and
        # does every bit of the fitting itself.
        with tempfile.TemporaryDirectory() as directory:
            repository = init_committed_repo(Path(directory) / "repo")
            binary = Path(directory) / "engine"
            write_fake_native_engine(binary, wide_overview=True)
            environment = self._environment(directory, binary)
            transports: list[RecordingTransport] = []

            def transport_for(path: Path) -> RecordingTransport:
                transports.append(RecordingTransport(path))
                return transports[-1]

            run_build(repository, environment=environment, transport_for=transport_for)
            for budget in (2000, 4000, 8000, 12000):
                with self.subTest(budget=budget):
                    overview = run_query(
                        repository,
                        QueryArguments(
                            "repository-overview", None, (), [], [], [], [], 6, budget, False
                        ),
                        environment=environment,
                        transport_for=transport_for,
                    )
                    sent = [
                        item
                        for transport in transports
                        for item in transport.requests
                        if item["operation"] == "repository-overview"
                    ][-1]

                    self.assertEqual(sent["maximum_model_output_characters"], 12000)
                    # Only the budget is widened: how many files the engine
                    # ranks is still the caller's own limit.
                    self.assertEqual(sent["maximum_results"], 6)
                    # And the caller's budget is what bounds what it receives.
                    self.assertLessEqual(overview["output_characters"], budget)
                    self.assertEqual(
                        overview["output_characters"],
                        len(
                            json.dumps(
                                overview,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                        ),
                    )
            # Every other operation still hands the engine the caller's own
            # budget, because the engine is what fits those answers.
            run_query(
                repository,
                QueryArguments("repository-map", None, (), [], [], [], [], 6, 2000, False),
                environment=environment,
                transport_for=transport_for,
            )
            mapped = [
                item
                for transport in transports
                for item in transport.requests
                if item["operation"] == "repository-map"
            ][-1]
            self.assertEqual(mapped["maximum_model_output_characters"], 2000)

    def test_the_engine_budget_stays_the_wire_maximum(self) -> None:
        # OVERVIEW_ENGINE_OUTPUT_CHARACTERS must track the wire's own allowed
        # budgets, not a copy of the number: if the schema ever grows a wider
        # budget, this constant should widen with it rather than silently
        # falling behind.
        self.assertEqual(
            OVERVIEW_ENGINE_OUTPUT_CHARACTERS,
            max(level1_models._ALLOWED_BUDGETS),
        )

    def test_the_overview_summary_is_never_trimmed_and_reports_its_length(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = init_committed_repo(Path(directory) / "repo")
            binary = Path(directory) / "engine"
            write_fake_native_engine(binary)
            environment = self._environment(directory, binary)

            run_build(repository, environment=environment, transport_for=OneShotTransport)
            overview = run_query(
                repository,
                QueryArguments(
                    "repository-overview", None, (), [], [], [], [], 8, 2000, False
                ),
                environment=environment,
                transport_for=OneShotTransport,
            )

            # The table survives the smallest budget; only the honest length
            # and the warning report the overrun.
            self.assertEqual(len(overview["groups"]), 2)
            self.assertEqual(
                overview["output_characters"],
                len(
                    json.dumps(
                        overview, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    )
                ),
            )

    def test_a_refused_build_over_an_old_generation_answers_rebuild_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = init_committed_repo(Path(directory) / "repo")
            binary = Path(directory) / "engine"
            write_fake_native_engine(binary, build_error=True)
            environment = self._environment(directory, binary)
            state_home = Path(environment["TAF_STATE_HOME"])
            entry = fabricate_incompatible_generation(state_home, repository)

            refused = run_build(
                repository, environment=environment, transport_for=OneShotTransport
            )

            # A build the engine refused with no warning of its own is never a
            # bare "did not become ready" once the state explains it: the
            # answer names the next safe action and the runtime that wrote the
            # generation this one could not read.
            self.assertEqual(refused["next_safe_action"], "rebuild-index")
            self.assertIn("incompatible-generation", refused["warnings"])
            self.assertEqual(refused["engine"]["replaced_generation_version"], "0.1.1")
            self.assertEqual(refused["context"]["status"], "error")
            self.assertEqual(refused["required_authorizations"], ["state-write"])
            self.assertFalse((entry / "native" / "generations" / ("d" * 64)).exists())
            self.assertFalse((entry / "binding.json").exists())

    def test_an_unexplained_build_refusal_carries_the_engine_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = init_committed_repo(Path(directory) / "repo")
            binary = Path(directory) / "engine"
            write_fake_native_engine(binary, build_error=True)
            environment = self._environment(directory, binary)

            with self.assertRaises(PrepareCLIError) as caught:
                run_build(repository, environment=environment, transport_for=OneShotTransport)

            self.assertEqual(
                str(caught.exception),
                "native context build did not become ready (engine status: error)",
            )

    def test_impact_candidates_composes_one_related_call_per_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, base = change_fixture(root)
            binary = root / "engine"
            write_fake_native_engine(binary)
            environment = self._environment(directory, binary)
            transports: list[RecordingTransport] = []

            def transport_for(path: Path) -> RecordingTransport:
                transports.append(RecordingTransport(path))
                return transports[-1]

            run_build(repository, environment=environment, transport_for=transport_for)
            impact = run_query(
                repository,
                QueryArguments(
                    "impact-candidates", None, (), [], [], [], [], 8, 4000, False, None, base
                ),
                environment=environment,
                transport_for=transport_for,
            )

            requests = [request for transport in transports for request in transport.requests]
            related = [item for item in requests if item["operation"] == "related-symbols"]
            self.assertEqual(
                [(item["schema_version"], item["direction"], len(item["result_identities"]))
                 for item in related],
                [("2", "callers", 1), ("2", "importers", 1), ("2", "importers", 1)],
            )
            self.assertEqual(impact["operation"], "impact-candidates")
            self.assertEqual(impact["status"], "ready")
            self.assertEqual(impact["changed_count"], 2)
            self.assertEqual(
                [item["qualified_name"] for item in impact["changed"]], ["app", "app.first"]
            )
            self.assertEqual(
                [(item["qualified_name"], [anchor["qualified_name"] for anchor in item["anchors"]])
                 for item in impact["findings"]],
                [("app", ["app"]), ("web.handle", ["app.first"])],
            )
            self.assertEqual(impact["findings"][1]["relation"], "call")
            self.assertLessEqual(int(impact["output_characters"]), 4000)
            self.assertEqual(impact["refresh"]["performed"], False)

    def test_impact_candidates_reuses_the_resolved_root_instead_of_re_deriving_it(self) -> None:
        # `collect_snapshot` already resolves the repository root once (H2);
        # the change-query path used to re-derive it twice more, through
        # `resolve_change_base` and `changed_ranges` each calling
        # `_repository_root`, which both run `git rev-parse --show-toplevel`
        # via `recovery._run_git`. Wrapping that one seam and filtering for
        # that exact call proves the change-query path no longer adds any.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, base = change_fixture(root)
            binary = root / "engine"
            write_fake_native_engine(binary)
            environment = self._environment(directory, binary)
            run_build(repository, environment=environment, transport_for=OneShotTransport)

            from taf_context import recovery as recovery_module

            original_run_git = recovery_module._run_git
            toplevel_calls: list[tuple[str, ...]] = []

            def counting_run_git(repo, *args, **kwargs):
                if args[:2] == ("rev-parse", "--show-toplevel"):
                    toplevel_calls.append(args)
                return original_run_git(repo, *args, **kwargs)

            with patch("taf_context.recovery._run_git", side_effect=counting_run_git):
                run_query(
                    repository,
                    QueryArguments(
                        "impact-candidates", None, (), [], [], [], [], 8, 4000, False, None, base
                    ),
                    environment=environment,
                    transport_for=OneShotTransport,
                )

            self.assertEqual(toplevel_calls, [])

    def test_an_unresolved_base_is_reported_and_covers_uncommitted_work_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _base = change_fixture(root)
            # No upstream, no origin/HEAD, and no local main or master left.
            run(repository, "git", "branch", "-m", "main", "work")
            lines = [f"line {number}" for number in range(1, 41)]
            lines[6] = "line 7 edited in the worktree"
            write(repository / "app.py", "\n".join(lines) + "\n")
            binary = root / "engine"
            write_fake_native_engine(binary)
            environment = self._environment(directory, binary)

            run_build(repository, environment=environment, transport_for=OneShotTransport)
            changed = run_query(
                repository,
                QueryArguments("changed-symbols", None, (), [], [], [], [], 8, 4000, False),
                environment=environment,
                transport_for=OneShotTransport,
            )

            self.assertEqual(
                changed["base"],
                {
                    "requested": None,
                    "ref": None,
                    "sha": None,
                    "source": "unknown",
                    "warning": "base-unresolved",
                },
            )
            self.assertEqual(changed["warnings"], ["base-unresolved"])
            self.assertEqual(
                [item["qualified_name"] for item in changed["findings"]], ["app", "app.first"]
            )

    def test_transport_failures_keep_the_cli_messages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = init_committed_repo(Path(directory) / "repo")
            binary = Path(directory) / "engine"
            write_fake_native_engine(binary)
            environment = self._environment(directory, binary)

            class Failing:
                def __init__(self, reason: str) -> None:
                    self.reason = reason

                def exchange(self, wire: bytes, *, idempotent: bool) -> bytes:
                    raise NativeTransportError(self.reason)

            for reason, message in (
                ("timeout", "native engine invocation failed"),
                ("invocation-failed", "native engine invocation failed"),
                ("rejected", "native engine rejected the request"),
                ("restarted", "native engine rejected the request"),
            ):
                with self.assertRaises(PrepareCLIError) as caught:
                    run_build(
                        repository,
                        environment=environment,
                        transport_for=lambda _path, reason=reason: Failing(reason),
                    )
                self.assertEqual(str(caught.exception), message)

    def test_build_without_engine_or_installer_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = init_committed_repo(Path(directory) / "repo")
            environment = {
                "HOME": directory,
                "PATH": "",
                "TAF_STATE_HOME": str(Path(directory) / "state"),
            }
            with self.assertRaises(PrepareCLIError) as caught:
                run_build(repository, environment=environment, transport_for=OneShotTransport)
            self.assertEqual(str(caught.exception), "native engine is unavailable")


REPOSITORY_IDENTITY = "sha256:" + "1" * 64
WORKTREE_IDENTITY = "sha256:" + "2" * 64
DIRTY_IDENTITY = "sha256:" + "3" * 64
INDEX_IDENTITY = "sha256:" + "4" * 64
HEAD = "a" * 40


def identity(path: str, name: str) -> str:
    """A stable result identity; two files may carry the same symbol name."""
    return "sha256:" + hashlib.sha256((path + "\x00" + name).encode("utf-8")).hexdigest()


def finding_wire(
    rank: int,
    name: str,
    *,
    path: str,
    kind: str = "definition",
    start: int = 10,
    end: int = 14,
    relation: str | None = None,
    edge_evidence: str | None = None,
    reference_line: int = 0,
    reference_count: int = 0,
) -> dict[str, object]:
    return {
        "rank": rank,
        "result_identity": identity(path, name),
        "path": path,
        "start_line": start,
        "end_line": end,
        "language": "Python",
        "record_kind": kind,
        "source_type": "source",
        "qualified_name": name,
        "extraction_method": "fixture",
        "evidence_class": "verified",
        "preview": "",
        "relation": relation,
        "edge_evidence": edge_evidence,
        "reference_line": reference_line,
        "reference_count": reference_count,
    }


def engine_result(
    operation: str,
    schema: str,
    findings: list[dict[str, object]],
    *,
    status: str = "ready",
    warnings: tuple[str, ...] = (),
    omitted: int = 0,
    truncated: bool = False,
    next_safe_action: str = "use-index",
) -> Level1Result:
    return Level1Result.from_dict(
        {
            "schema_version": schema,
            "request_identity": "request-0001",
            "operation": operation,
            "status": status,
            "provider_identity": "taf-context",
            "provider_version": "0.6.0",
            "index_identity": INDEX_IDENTITY,
            "repository_identity": REPOSITORY_IDENTITY,
            "worktree_identity": WORKTREE_IDENTITY,
            "committed_head": HEAD,
            "dirty_overlay_fingerprint": DIRTY_IDENTITY,
            "freshness": "exact",
            "parser_versions": {},
            "coverage": {
                "path_coverage": 1.0,
                "language_coverage": 1.0,
                "indexed_path_count": 1,
                "excluded_path_count": 0,
                "unsupported_language_count": 0,
                "parse_failure_count": 0,
                "exclusion_reason_counts": {},
            },
            "findings": findings,
            "returned_count": len(findings),
            "omitted_count": omitted,
            "truncated": truncated,
            "output_characters": 100,
            "warnings": list(warnings),
            "next_safe_action": next_safe_action,
        }
    )


CHANGED = engine_result(
    "changed-symbols",
    "3",
    [
        finding_wire(1, "app", path="app.py", kind="module", start=1, end=40),
        finding_wire(2, "app.first", path="app.py", start=3, end=9),
        finding_wire(3, "app.second", path="app.py", start=11, end=20),
    ],
)

# The two shapes a changed entry can have, sorted the way `sorted(entry)` is.
FULL_CHANGED_KEYS = [
    "end_line",
    "path",
    "qualified_name",
    "record_kind",
    "result_identity",
    "start_line",
]
COMPACT_CHANGED_KEYS = ["path", "qualified_name"]


class FakeRelated:
    """Answers `related-symbols` from a canned per-anchor table."""

    def __init__(self, table: dict[tuple[str, str], list[dict[str, object]]], **outcomes: object) -> None:
        self.table = table
        self.outcomes = outcomes
        self.calls: list[tuple[tuple[str, ...], str]] = []

    def __call__(self, anchors: tuple[str, ...], direction: str) -> Level1Result:
        self.calls.append((anchors, direction))
        findings: list[dict[str, object]] = []
        for anchor in anchors:
            for entry in self.table.get((anchor, direction), []):
                findings.append(dict(entry, rank=len(findings) + 1))
        outcome = self.outcomes.get(",".join(anchors) + "/" + direction, {})
        assert isinstance(outcome, dict)
        return engine_result("related-symbols", "2", findings, **outcome)


def caller_of(name: str, *, path: str, evidence: str = "verified", count: int = 2) -> dict[str, object]:
    return finding_wire(
        1,
        name,
        path=path,
        relation="call",
        edge_evidence=evidence,
        reference_line=12,
        reference_count=count,
    )


class ComposeImpactCandidatesTests(unittest.TestCase):
    def _compose(self, related: FakeRelated, **overrides: object) -> dict[str, object]:
        arguments: dict[str, object] = {"allow_inferred": False, "maximum_results": 8}
        arguments.update(overrides)
        return compose_impact_candidates(CHANGED, related, **arguments)

    def test_every_anchor_is_asked_for_on_its_own_so_attribution_is_exact(self) -> None:
        # A multi-anchor related-symbols call merges every anchor's edges into
        # one edge per candidate record, so the broker asks per anchor.
        related = FakeRelated(
            {
                (identity("app.py", "app.first"), "callers"): [caller_of("web.handle", path="web.py")],
                (identity("app.py", "app.second"), "callers"): [caller_of("web.handle", path="web.py")],
                (identity("app.py", "app"), "importers"): [
                    finding_wire(
                        1,
                        "app",
                        path="other.py",
                        kind="import",
                        start=1,
                        end=1,
                        relation="import",
                        edge_evidence="verified",
                        reference_line=1,
                        reference_count=1,
                    )
                ],
            }
        )
        composed = self._compose(related)

        self.assertEqual(
            related.calls,
            [
                ((identity("app.py", "app.first"),), "callers"),
                ((identity("app.py", "app.second"),), "callers"),
                ((identity("app.py", "app"),), "importers"),
                ((identity("app.py", "app.first"),), "importers"),
                ((identity("app.py", "app.second"),), "importers"),
            ],
        )
        self.assertEqual(composed["changed_count"], 3)
        findings = composed["findings"]
        assert isinstance(findings, list)
        self.assertEqual(
            [(item["qualified_name"], [anchor["qualified_name"] for anchor in item["anchors"]])
             for item in findings],
            [
                ("web.handle", ["app.first", "app.second"]),
                ("app", ["app"]),
            ],
        )
        self.assertEqual([item["rank"] for item in findings], [1, 2])
        self.assertEqual(composed["returned_count"], 2)
        self.assertEqual((composed["status"], composed["omitted_count"]), ("ready", 0))
        self.assertEqual(findings[0]["anchors"][0]["reference_line"], 12)

    def test_a_changed_symbol_is_never_its_own_candidate(self) -> None:
        related = FakeRelated(
            {
                (identity("app.py", "app.first"), "callers"): [
                    caller_of("app.second", path="app.py"),
                    caller_of("web.handle", path="web.py"),
                ],
                # A module record reached from its own file is dropped too.
                (identity("app.py", "app"), "importers"): [
                    finding_wire(1, "app", path="app.py", kind="module", start=1, end=40,
                                 relation="import", edge_evidence="verified",
                                 reference_line=1, reference_count=1)
                ],
            }
        )
        composed = self._compose(related)
        findings = composed["findings"]
        assert isinstance(findings, list)
        self.assertEqual([item["qualified_name"] for item in findings], ["web.handle"])

    def test_the_strongest_anchor_gives_the_candidate_its_edge(self) -> None:
        related = FakeRelated(
            {
                (identity("app.py", "app.first"), "callers"): [
                    caller_of("web.handle", path="web.py", evidence="inferred", count=1)
                ],
                (identity("app.py", "app.second"), "callers"): [
                    caller_of("web.handle", path="web.py", evidence="verified", count=7)
                ],
            }
        )
        composed = self._compose(related, allow_inferred=True)
        candidate = composed["findings"][0]

        self.assertEqual(
            (candidate["edge_evidence"], candidate["reference_count"]), ("verified", 7)
        )
        self.assertEqual(
            [(anchor["qualified_name"], anchor["edge_evidence"]) for anchor in candidate["anchors"]],
            [("app.second", "verified"), ("app.first", "inferred")],
        )

    def test_inferred_edges_stay_hidden_unless_they_were_asked_for(self) -> None:
        related = FakeRelated(
            {
                (identity("app.py", "app.first"), "callers"): [
                    caller_of("web.handle", path="web.py", evidence="inferred")
                ]
            }
        )
        self.assertEqual(self._compose(related)["findings"], [])
        self.assertEqual(len(self._compose(related, allow_inferred=True)["findings"]), 1)

    def test_verified_candidates_lead_then_the_ones_with_more_anchors(self) -> None:
        related = FakeRelated(
            {
                (identity("app.py", "app.first"), "callers"): [
                    caller_of("a.one", path="a.py", evidence="inferred"),
                    caller_of("z.many", path="z.py"),
                    caller_of("b.single", path="b.py"),
                ],
                (identity("app.py", "app.second"), "callers"): [caller_of("z.many", path="z.py")],
            }
        )
        composed = self._compose(related, allow_inferred=True)
        self.assertEqual(
            [item["qualified_name"] for item in composed["findings"]],
            ["z.many", "b.single", "a.one"],
        )

    def test_status_next_action_warnings_and_omissions_aggregate(self) -> None:
        related = FakeRelated(
            {(identity("app.py", "app.first"), "callers"): [caller_of("web.handle", path="web.py")]},
            **{
                identity("app.py", "app.second") + "/callers": {
                    "status": "partial",
                    "warnings": ("work-budget-exhausted",),
                    "omitted": 3,
                    "truncated": True,
                    "next_safe_action": "refine-query",
                }
            },
        )
        composed = self._compose(related)

        self.assertEqual(composed["status"], "partial")
        self.assertEqual(composed["next_safe_action"], "refine-query")
        self.assertEqual(composed["warnings"], ["work-budget-exhausted"])
        self.assertEqual(composed["omitted_count"], 3)
        self.assertIs(composed["truncated"], True)

    def test_the_changed_sets_own_omissions_are_counted_on_their_own(self) -> None:
        # `omitted_count` keeps its spec meaning (underlying omissions plus
        # dropped candidates), so the changed set's share is reported next to
        # `changed_count` where a reader looks for it.
        changed = engine_result(
            "changed-symbols",
            "3",
            [finding_wire(1, "app.first", path="app.py", start=3, end=9)],
            omitted=8,
            truncated=True,
        )
        related = FakeRelated(
            {
                (identity("app.py", "app.first"), "callers"): [
                    caller_of(f"web.handle{index}", path=f"web{index}.py") for index in range(3)
                ]
            }
        )
        composed = compose_impact_candidates(
            changed, related, allow_inferred=False, maximum_results=2
        )

        keys = list(composed)
        self.assertEqual(
            keys[keys.index("changed_count") + 1], "changed_omitted_count"
        )
        # What the budget trimmed off the changed list is counted next to what
        # the engine left out of it, and a composed answer has trimmed nothing.
        self.assertEqual(
            keys[keys.index("changed_omitted_count") + 1], "changed_trimmed_count"
        )
        self.assertEqual(composed["changed_trimmed_count"], 0)
        self.assertEqual(composed["changed_omitted_count"], 8)
        self.assertEqual(composed["changed_count"], 1)
        self.assertEqual(composed["omitted_count"], 9)

    def test_maximum_results_bounds_the_candidates_and_counts_the_rest(self) -> None:
        related = FakeRelated(
            {
                (identity("app.py", "app.first"), "callers"): [
                    caller_of(f"web.handle{index}", path=f"web{index}.py") for index in range(5)
                ]
            }
        )
        composed = self._compose(related, maximum_results=2)

        self.assertEqual(composed["returned_count"], 2)
        self.assertEqual(composed["omitted_count"], 3)
        self.assertIs(composed["truncated"], True)
        self.assertEqual([item["rank"] for item in composed["findings"]], [1, 2])

    def test_changed_orders_the_anchor_first_then_by_kind_ahead_of_module(self) -> None:
        # 2.7.1 decision 1: `app.first` anchors the one returned candidate, so
        # it leads even though the engine returned it second; the remaining
        # two are neither test files, so they order by kind - `app.second`
        # (a definition) ahead of `app` (a module) - not by their own lines,
        # which would otherwise put `app` (line 1) first.
        related = FakeRelated(
            {(identity("app.py", "app.first"), "callers"): [caller_of("web.handle", path="web.py")]}
        )
        composed = self._compose(related)

        self.assertEqual(
            [item["qualified_name"] for item in composed["changed"]],
            ["app.first", "app.second", "app"],
        )

    def test_changed_group_a_ranks_by_how_many_candidates_an_anchor_reaches(self) -> None:
        # 2.7.1 decision 1(a): the anchor reaching more returned candidates
        # sorts first even when its own line comes later in the file - the
        # count is the primary key, not the path or the start line.
        changed = engine_result(
            "changed-symbols",
            "3",
            [
                finding_wire(1, "early", path="app.py", start=3, end=4),
                finding_wire(2, "late", path="app.py", start=20, end=21),
            ],
        )
        related = FakeRelated(
            {
                (identity("app.py", "early"), "callers"): [caller_of("web.one", path="web.py")],
                (identity("app.py", "late"), "callers"): [
                    caller_of("web.one", path="web.py"),
                    caller_of("web.two", path="web.py"),
                ],
            }
        )
        composed = compose_impact_candidates(
            changed, related, allow_inferred=False, maximum_results=8
        )

        self.assertEqual(
            [item["qualified_name"] for item in composed["changed"]], ["late", "early"]
        )

    def test_changed_group_b_orders_non_test_paths_and_definitions_first(self) -> None:
        # 2.7.1 decision 1(b): with no anchor at all (no related answer
        # reaches any of these), the changed set orders non-test paths before
        # test paths, and within each a caller-anchor kind (`definition`)
        # before `module`, ahead of path and start line - `mod.fn` (line 5)
        # sorts before `mod` (line 1) in the same non-test file for exactly
        # that reason, and a `tests/` directory segment marks a test path
        # the same way a `_test.` suffix does.
        changed = engine_result(
            "changed-symbols",
            "3",
            [
                finding_wire(1, "mod", path="lib/mod.py", kind="module", start=1, end=40),
                finding_wire(2, "mod.fn", path="lib/mod.py", start=5, end=9),
                finding_wire(3, "mod_test.fn", path="lib/mod_test.py", start=2, end=4),
                finding_wire(4, "other", path="tests/other.py", kind="module", start=1, end=20),
            ],
        )
        related = FakeRelated({})
        composed = compose_impact_candidates(
            changed, related, allow_inferred=False, maximum_results=8
        )

        self.assertEqual(composed["findings"], [])
        self.assertEqual(
            [(item["path"], item["qualified_name"]) for item in composed["changed"]],
            [
                ("lib/mod.py", "mod.fn"),
                ("lib/mod.py", "mod"),
                ("lib/mod_test.py", "mod_test.fn"),
                ("tests/other.py", "other"),
            ],
        )


class TrimToBudgetTests(unittest.TestCase):
    def _object(self) -> dict[str, object]:
        related = FakeRelated(
            {
                (identity("app.py", "app.first"), "callers"): [
                    caller_of(f"web.handle{index}", path=f"web{index}.py") for index in range(6)
                ]
            }
        )
        return compose_impact_candidates(
            CHANGED, related, allow_inferred=False, maximum_results=8
        )

    def test_an_object_inside_the_budget_only_reports_its_length(self) -> None:
        composed = self._object()
        trimmed = trim_to_budget(composed, 12000)

        self.assertEqual(trimmed["findings"], composed["findings"])
        self.assertEqual(trimmed["changed"], composed["changed"])
        self.assertEqual(trimmed["changed_trimmed_count"], 0)
        self.assertEqual(sorted(trimmed["changed"][0]), FULL_CHANGED_KEYS)
        self.assertLessEqual(int(trimmed["output_characters"]), 12000)
        self.assertEqual(
            int(trimmed["output_characters"]),
            len(json.dumps(trimmed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
        )

    def test_compacting_the_changed_list_alone_absorbs_a_small_overrun(self) -> None:
        # A budget only 40 characters under the full form is exactly the case
        # the order change is for: compacting the three changed entries frees
        # far more than 40 characters, so nothing else has to pay - every
        # candidate stays and no candidate is even dropped.
        composed = self._object()
        full = int(trim_to_budget(composed, 12000)["output_characters"])
        trimmed = trim_to_budget(composed, full - 40)

        self.assertEqual(
            [sorted(item) for item in trimmed["changed"]], [COMPACT_CHANGED_KEYS] * 3
        )
        self.assertEqual(trimmed["changed_trimmed_count"], 0)
        self.assertNotIn("changed-list-trimmed", trimmed["warnings"])
        self.assertEqual(trimmed["findings"], composed["findings"])
        self.assertIs(trimmed["truncated"], False)
        self.assertLessEqual(int(trimmed["output_characters"]), full - 40)

    def test_candidates_are_dropped_from_the_tail_with_their_ranks_intact(self) -> None:
        composed = self._object()
        trimmed = trim_to_budget(composed, 2000)

        self.assertLessEqual(int(trimmed["output_characters"]), 2000)
        self.assertLess(len(trimmed["findings"]), len(composed["findings"]))
        self.assertIs(trimmed["truncated"], True)
        self.assertEqual(
            int(trimmed["omitted_count"]),
            int(composed["omitted_count"])
            + len(composed["findings"])
            - len(trimmed["findings"]),
        )
        self.assertEqual(
            [item["rank"] for item in trimmed["findings"]],
            list(range(1, len(trimmed["findings"]) + 1)),
        )

    def _wide_object(self, changed_symbols: int, candidates: int) -> dict[str, object]:
        """A change set at the internal cap whose anchors reach few candidates."""
        changed = engine_result(
            "changed-symbols",
            "3",
            [
                finding_wire(index + 1, f"app.symbol{index:03d}", path="app.py", start=index * 3 + 1, end=index * 3 + 2)
                for index in range(changed_symbols)
            ],
        )
        first = identity("app.py", "app.symbol000")
        related = FakeRelated(
            {
                (first, "callers"): [
                    caller_of(f"web.handle{index}", path=f"web{index}.py")
                    for index in range(candidates)
                ]
            }
        )
        return compose_impact_candidates(
            changed, related, allow_inferred=False, maximum_results=8
        )

    def test_a_changed_list_over_its_share_shrinks_to_the_compact_form(self) -> None:
        # Ten changed symbols are about 1995 characters in full and 511
        # compact (no identity), so at 5000 (a third is 1666) the compact
        # form is the whole of the fix: no entry is lost and nothing is
        # warned about.
        composed = self._wide_object(10, 8)
        trimmed = trim_to_budget(composed, 5000)

        self.assertLessEqual(int(trimmed["output_characters"]), 5000)
        self.assertEqual(len(trimmed["changed"]), 10)
        self.assertEqual(
            [sorted(item) for item in trimmed["changed"]], [COMPACT_CHANGED_KEYS] * 10
        )
        self.assertEqual(
            [item["qualified_name"] for item in trimmed["changed"]],
            [item["qualified_name"] for item in composed["changed"]],
        )
        self.assertEqual(trimmed["changed_trimmed_count"], 0)
        self.assertNotIn("changed-list-trimmed", trimmed["warnings"])
        self.assertLess(len(trimmed["findings"]), len(composed["findings"]))

    def test_a_compact_changed_list_over_its_share_loses_its_tail(self) -> None:
        # A compact entry (no identity) is about 51 characters, so a third of
        # 2000 holds only thirteen of them on its own (13 x 51 = 664, a
        # fourteenth would be 715) - the share-only cutoff of 2.7.1. 2.7.2's
        # give-back then restores what the object as a whole, once the single
        # candidate settled, still had room for: seventeen survive and
        # forty-seven are counted, not silently gone. The candidate - the
        # operation's answer - survives that trimming either way.
        composed = self._wide_object(64, 1)
        trimmed = trim_to_budget(composed, 2000)

        self.assertLessEqual(int(trimmed["output_characters"]), 2000)
        self.assertEqual(len(trimmed["findings"]), 1)
        self.assertEqual(trimmed["returned_count"], 1)
        self.assertIn("changed-list-trimmed", trimmed["warnings"])
        self.assertNotIn("output-budget-exceeded", trimmed["warnings"])
        self.assertEqual(trimmed["changed_count"], 64)
        self.assertEqual(len(trimmed["changed"]), 17)
        self.assertEqual(
            len(trimmed["changed"]) + int(trimmed["changed_trimmed_count"]), 64
        )
        self.assertEqual(
            trimmed["changed"],
            [
                {
                    "path": item["path"],
                    "qualified_name": item["qualified_name"],
                }
                for item in composed["changed"][: len(trimmed["changed"])]
            ],
        )
        self.assertIs(trimmed["truncated"], False)
        self.assertEqual(trimmed["omitted_count"], composed["omitted_count"])

    def _wide_object_long(self, changed_symbols: int, candidates: int) -> dict[str, object]:
        """A change set whose entries are long enough that the full form alone
        exceeds a typical budget - the 2.7.1 release-verification shape give-back
        is meant to fix, where the share-only trim lost entries the whole object
        never actually needed to give up."""
        prefix = "pkg/module/deeply/nested/path/segment"
        changed = engine_result(
            "changed-symbols",
            "3",
            [
                finding_wire(
                    index + 1,
                    f"very_long_qualified_symbol_name_number_{index:04d}",
                    path=f"{prefix}{index:04d}.py",
                    start=index * 3 + 1,
                    end=index * 3 + 2,
                )
                for index in range(changed_symbols)
            ],
        )
        if candidates:
            first = identity(f"{prefix}0000.py", "very_long_qualified_symbol_name_number_0000")
            related = FakeRelated(
                {
                    (first, "callers"): [
                        caller_of(f"web.handle{index}", path=f"web{index}.py")
                        for index in range(candidates)
                    ]
                }
            )
        else:
            related = FakeRelated({})
        return compose_impact_candidates(
            changed, related, allow_inferred=False, maximum_results=8
        )

    def test_give_back_restores_every_entry_the_share_only_trim_would_drop(self) -> None:
        # The 2.7.1 release verification: 0 candidates, 40 changed symbols long
        # enough that the full form exceeds the 8000-character default budget,
        # so `_fit_changed_to_share` compacts and then trims the tail down to
        # its third-of-budget share - even though the compact form of all 40
        # measures well under 8000 once nothing else needed trimming. 2.7.2's
        # give-back restores every one of them: the whole changed list is
        # listed, nothing is counted as trimmed, and no warning is raised.
        composed = self._wide_object_long(40, 0)
        trimmed = trim_to_budget(composed, 8000)

        self.assertEqual(len(trimmed["changed"]), 40)
        self.assertEqual(trimmed["changed_count"], 40)
        self.assertEqual(trimmed["changed_trimmed_count"], 0)
        self.assertNotIn("changed-list-trimmed", trimmed["warnings"])
        self.assertLessEqual(int(trimmed["output_characters"]), 8000)
        self.assertEqual(
            int(trimmed["output_characters"]),
            len(json.dumps(trimmed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
        )
        self.assertEqual(
            [item["qualified_name"] for item in trimmed["changed"]],
            [item["qualified_name"] for item in composed["changed"]],
        )

    def test_give_back_restores_some_but_not_all_entries(self) -> None:
        # Sixty-four changed symbols and one candidate at 2000: the share-only
        # trim keeps thirteen on its own; give-back extends that as far as the
        # remaining budget allows and no further, so some - not all - of what
        # was trimmed comes back, and the counter and the warning both still
        # agree with what is actually left out.
        composed = self._wide_object(64, 1)
        trimmed = trim_to_budget(composed, 2000)

        self.assertGreater(len(trimmed["changed"]), 13)
        self.assertLess(len(trimmed["changed"]), 64)
        self.assertGreater(int(trimmed["changed_trimmed_count"]), 0)
        self.assertEqual(
            trimmed["changed_count"],
            len(trimmed["changed"]) + int(trimmed["changed_trimmed_count"]),
        )
        self.assertIn("changed-list-trimmed", trimmed["warnings"])
        self.assertLessEqual(int(trimmed["output_characters"]), 2000)
        # What survives is still the retained-order prefix, unbroken.
        self.assertEqual(
            trimmed["changed"],
            [
                {"path": item["path"], "qualified_name": item["qualified_name"]}
                for item in composed["changed"][: len(trimmed["changed"])]
            ],
        )
        # One more entry would overrun the budget - that is why it stayed trimmed.
        one_more = dict(trimmed)
        next_item = composed["changed"][len(trimmed["changed"])]
        one_more["changed"] = list(trimmed["changed"]) + [
            {"path": next_item["path"], "qualified_name": next_item["qualified_name"]}
        ]
        self.assertGreater(context_operations._output_characters(one_more), 2000)

    def test_candidates_pay_before_the_changed_lists_share_does(self) -> None:
        composed = self._wide_object(64, 4)
        trimmed = trim_to_budget(composed, 2000)

        self.assertLessEqual(int(trimmed["output_characters"]), 2000)
        self.assertGreater(len(trimmed["changed"]), 0)
        self.assertLess(len(trimmed["findings"]), 4)
        self.assertGreater(len(trimmed["findings"]), 0)
        self.assertIs(trimmed["truncated"], True)
        self.assertEqual(
            int(trimmed["omitted_count"]),
            int(composed["omitted_count"]) + 4 - len(trimmed["findings"]),
        )
        self.assertEqual(
            [item["rank"] for item in trimmed["findings"]],
            list(range(1, len(trimmed["findings"]) + 1)),
        )

    def test_a_field_sized_change_set_keeps_a_readable_changed_list(self) -> None:
        # The 2.6.0 field case: 46 changed symbols and more candidates than
        # `maximum_results` keeps, at the operation's default budget. Compact
        # (no identity) the 46 entries measure 2347 canonical characters,
        # under the 2666-character third of 8000, so the whole changed list
        # survives compact and none of it is trimmed from the tail.
        composed = self._wide_object(46, 24)
        trimmed = trim_to_budget(composed, 8000)

        self.assertLessEqual(int(trimmed["output_characters"]), 8000)
        self.assertEqual(trimmed["changed_count"], 46)
        self.assertEqual(trimmed["changed_omitted_count"], 0)
        self.assertEqual(len(trimmed["changed"]), 46)
        self.assertEqual(trimmed["changed_trimmed_count"], 0)
        self.assertEqual(
            int(trimmed["changed_count"]),
            len(trimmed["changed"]) + int(trimmed["changed_trimmed_count"]),
        )
        self.assertEqual(
            [sorted(item) for item in trimmed["changed"]], [COMPACT_CHANGED_KEYS] * 46
        )
        self.assertNotIn("changed-list-trimmed", trimmed["warnings"])
        self.assertNotIn("output-budget-exceeded", trimmed["warnings"])
        self.assertEqual(len(trimmed["findings"]), 8)

    def test_a_budget_nothing_fits_in_is_reported_instead_of_looping(self) -> None:
        composed = self._wide_object(8, 2)
        trimmed = trim_to_budget(composed, 1)

        self.assertEqual(trimmed["changed"], [])
        self.assertEqual(trimmed["changed_trimmed_count"], 8)
        self.assertEqual(trimmed["findings"], [])
        self.assertEqual(trimmed["returned_count"], 0)
        self.assertIn("output-budget-exceeded", trimmed["warnings"])
        self.assertIn("changed-list-trimmed", trimmed["warnings"])
        self.assertEqual(trimmed["warnings"], sorted(trimmed["warnings"]))
        self.assertGreater(int(trimmed["output_characters"]), 1)
        self.assertEqual(
            int(trimmed["output_characters"]),
            len(json.dumps(trimmed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
        )

    def test_the_changed_list_goes_only_when_no_candidate_is_left_to_drop(self) -> None:
        # A budget barely over the envelope leaves nothing else to give, so
        # the changed layer's share goes too rather than overrunning silently.
        # It goes last: the candidates are already gone by then.
        composed = self._wide_object(64, 4)
        trimmed = trim_to_budget(composed, 500)

        self.assertEqual(trimmed["findings"], [])
        self.assertEqual(trimmed["changed"], [])
        self.assertEqual(trimmed["changed_trimmed_count"], 64)
        self.assertEqual(trimmed["changed_count"], 64)
        self.assertLessEqual(int(trimmed["output_characters"]), 500)
        self.assertNotIn("output-budget-exceeded", trimmed["warnings"])

    def test_the_budget_holds_whenever_it_is_not_reported_as_exceeded(self) -> None:
        for changed_symbols, candidates in ((0, 0), (3, 2), (10, 8), (46, 24), (64, 1), (64, 8)):
            composed = self._wide_object(changed_symbols, candidates)
            for budget in (1, 300, 900, 2000, 4000, 8000, 12000):
                with self.subTest(changed=changed_symbols, candidates=candidates, budget=budget):
                    trimmed = trim_to_budget(composed, budget)
                    measured = len(json.dumps(
                        trimmed, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    ))
                    self.assertEqual(int(trimmed["output_characters"]), measured)
                    # The engine omitted nothing here, so every changed symbol
                    # is either in the list or counted as trimmed.
                    self.assertEqual(
                        int(trimmed["changed_count"]),
                        len(trimmed["changed"]) + int(trimmed["changed_trimmed_count"]),
                    )
                    if "output-budget-exceeded" not in trimmed["warnings"]:
                        self.assertLessEqual(measured, budget)

    def test_anchor_retention_keeps_every_anchor_and_trims_test_files_first(self) -> None:
        # The 2.7.0 re-test's finding E: the anchor `zzz_service.handler`
        # sorts last in the engine's own answer (it comes after every library
        # and test entry), and a tight budget forces a tail trim. Decision 1
        # hoists it to the front of `changed` regardless, and decision 2
        # guarantees the tail trim never removes it: every anchor a returned
        # candidate names must still be in `changed` afterwards. The ten
        # library entries and the ten test entries are otherwise identical,
        # so whichever of the two groups loses entries first is decided by
        # decision 1(b) alone (non-test before test) - not by size or count.
        library = [
            finding_wire(
                index + 1, f"lib.symbol{index:03d}", path="lib.py", start=index * 3 + 1, end=index * 3 + 2
            )
            for index in range(10)
        ]
        tests = [
            finding_wire(
                index + 11,
                f"lib.symbol{index:03d}",
                path="tests/lib.py",
                start=index * 3 + 1,
                end=index * 3 + 2,
            )
            for index in range(10)
        ]
        anchor = finding_wire(21, "zzz_service.handler", path="zzz_service.py", start=5, end=9)
        changed = engine_result("changed-symbols", "3", library + tests + [anchor])
        related = FakeRelated(
            {
                (identity("zzz_service.py", "zzz_service.handler"), "callers"): [
                    caller_of("web.handle", path="web.py")
                ]
            }
        )
        composed = compose_impact_candidates(
            changed, related, allow_inferred=False, maximum_results=8
        )
        # Decision 1: the anchor leads even though the engine returned it last.
        self.assertEqual(composed["changed"][0]["qualified_name"], "zzz_service.handler")

        trimmed = trim_to_budget(composed, 2000)

        self.assertGreater(int(trimmed["changed_trimmed_count"]), 0)
        self.assertIn("changed-list-trimmed", trimmed["warnings"])
        changed_keys = {(item["path"], item["qualified_name"]) for item in trimmed["changed"]}
        # Every anchor of every returned candidate is still named in `changed`.
        self.assertGreater(len(trimmed["findings"]), 0)
        for candidate in trimmed["findings"]:
            for edge_anchor in candidate["anchors"]:
                self.assertIn((edge_anchor["path"], edge_anchor["qualified_name"]), changed_keys)
        # The library entries survive whole; the test entries are what the
        # tail trim spent instead.
        library_survivors = {q for p, q in changed_keys if p == "lib.py"}
        test_survivors = {q for p, q in changed_keys if p == "tests/lib.py"}
        self.assertEqual(library_survivors, {f"lib.symbol{index:03d}" for index in range(10)})
        self.assertLess(len(test_survivors), 10)

    def test_anchors_alone_over_their_share_are_kept_without_trimming(self) -> None:
        # 2.7.1 decision 2's second half: thirty anchors, each reaching its
        # own candidate, cost more compact than a third of this budget on
        # their own - so the changed layer's own share is exceeded by the
        # anchors alone. They are kept anyway rather than trimmed, and the
        # object still fits because the candidates gave up the rest.
        count = 30
        changed = engine_result(
            "changed-symbols",
            "3",
            [
                finding_wire(index + 1, f"anchor{index:03d}", path=f"a{index:03d}.py", start=1, end=2)
                for index in range(count)
            ],
        )
        table = {
            (identity(f"a{index:03d}.py", f"anchor{index:03d}"), "callers"): [
                caller_of(f"c{index:03d}", path=f"c{index:03d}.py")
            ]
            for index in range(count)
        }
        related = FakeRelated(table)
        composed = compose_impact_candidates(
            changed, related, allow_inferred=False, maximum_results=count
        )

        trimmed = trim_to_budget(composed, 3000)

        self.assertLessEqual(int(trimmed["output_characters"]), 3000)
        self.assertEqual(len(trimmed["changed"]), count)
        self.assertEqual(trimmed["changed_trimmed_count"], 0)
        self.assertNotIn("changed-list-trimmed", trimmed["warnings"])
        self.assertGreater(len(trimmed["findings"]), 0)
        self.assertLess(len(trimmed["findings"]), count)


class ChangedSelectorGuardTests(unittest.TestCase):
    def test_a_selector_inside_the_frame_is_returned_unchanged(self) -> None:
        changed = (ChangedPath("a.py", ((3, 4), (9, 9))), ChangedPath("b.py", ()))
        self.assertEqual(bound_changed_selector(changed), (changed, []))

    def test_range_heavy_paths_collapse_to_whole_file_entries_until_it_fits(self) -> None:
        # 200 long paths with 64 spans each is about 220 KB of selector before
        # the paths are counted; the engine refuses the frame above 256 KiB.
        changed = tuple(
            ChangedPath(
                f"{'d' * 240}/{'n' * 240}-{index:03d}.py",
                tuple(
                    (line * 20000 + 1, line * 20000 + 9)
                    for line in range(64)
                ),
            )
            for index in range(200)
        )
        bounded, warnings = bound_changed_selector(changed)

        # The frame guard has its own codes: a collapse here is request-frame
        # pressure, not the per-path hunk limit of `change_ranges`.
        self.assertEqual(warnings, ["changed-selector-collapsed"])
        self.assertEqual(len(bounded), 200)
        self.assertEqual([item.path for item in bounded], [item.path for item in changed])
        self.assertTrue(any(item.ranges == () for item in bounded))
        payload = [
            {"path": item.path, "ranges": [[start, end] for start, end in item.ranges]}
            for item in bounded
        ]
        self.assertLess(len(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
                        MAXIMUM_REQUEST_BYTES)

    def test_the_widest_path_is_collapsed_first(self) -> None:
        wide = ChangedPath("wide.py", tuple((line * 2 + 1, line * 2 + 1) for line in range(64)))
        narrow = ChangedPath("narrow.py", ((1, 2),))
        bounded, warnings = bound_changed_selector(
            (narrow, wide), available_bytes=len(b'[{"path":"narrow.py","ranges":[[1,2]]},') + 40
        )

        self.assertEqual(warnings, ["changed-selector-collapsed"])
        self.assertEqual(bounded, (narrow, ChangedPath("wide.py", ())))

    def test_a_selector_that_cannot_fit_at_all_drops_paths_from_the_tail(self) -> None:
        changed = tuple(ChangedPath(f"{'p' * 200}-{index:03d}.py", ()) for index in range(200))
        bounded, warnings = bound_changed_selector(changed, available_bytes=1000)

        self.assertEqual(warnings, ["changed-selector-limit"])
        self.assertLess(len(bounded), 200)
        self.assertEqual(bounded, changed[: len(bounded)])


class ValidateQueryRequestTests(unittest.TestCase):
    def test_rules_match_the_cli(self) -> None:
        sha = "sha256:" + "a" * 64
        self.assertEqual(validate_query_request("search-symbols", " main ", ()), ("main", ()))
        self.assertEqual(validate_query_request("source-snippets", None, (sha, sha)), (None, (sha,)))
        for operation, query, ids, message in (
            ("search-docs", "  ", (), "selected query operation requires --query"),
            ("repository-map", "x", (), "selected query operation does not accept --query"),
            ("source-snippets", None, (), "source-snippets requires at least one --result-id"),
            ("search-symbols", "x", (sha,), "selected query operation does not accept --result-id"),
            ("source-snippets", None, ("bad",), "query result identity is invalid"),
            ("related-symbols", "x", (sha,), "selected query operation does not accept --query"),
            ("related-symbols", None, (), "related-symbols requires at least one --result-id"),
        ):
            with self.assertRaises(PrepareCLIError) as caught:
                validate_query_request(operation, query, ids)
            self.assertEqual(str(caught.exception), message)

    def test_direction_rules_match_the_cli(self) -> None:
        sha = "sha256:" + "a" * 64
        self.assertEqual(
            validate_query_request("related-symbols", None, (sha,), "callers"), (None, (sha,))
        )
        self.assertEqual(validate_query_request("search-symbols", "main", ()), ("main", ()))
        anchors = tuple(sorted("sha256:" + f"{index:064x}" for index in range(17)))
        for operation, ids, direction, message in (
            ("related-symbols", (sha,), None, "related-symbols requires --direction"),
            ("related-symbols", (sha,), "sideways", "selected query direction is invalid"),
            ("search-symbols", (), "callers", "selected query operation does not accept --direction"),
            ("source-snippets", (sha,), "callers", "selected query operation does not accept --direction"),
            ("related-symbols", anchors, "callers", "related-symbols accepts at most 16 --result-id values"),
        ):
            with self.subTest(operation=operation, direction=direction):
                query = "main" if operation == "search-symbols" else None
                with self.assertRaises(PrepareCLIError) as caught:
                    validate_query_request(operation, query, ids, direction)
                self.assertEqual(str(caught.exception), message)

    def test_only_the_change_operations_accept_a_base(self) -> None:
        for operation in ("changed-symbols", "impact-candidates"):
            with self.subTest(operation=operation):
                self.assertEqual(
                    validate_query_request(operation, None, (), None, "origin/main"),
                    (None, ()),
                )
                self.assertEqual(validate_query_request(operation, None, ()), (None, ()))
        sha = "sha256:" + "a" * 64
        for operation, query, ids, direction, base, message in (
            ("search-symbols", "main", (), None, "origin/main",
             "selected query operation does not accept --base"),
            ("related-symbols", None, (sha,), "callers", "origin/main",
             "selected query operation does not accept --base"),
            ("changed-symbols", "main", (), None, None,
             "selected query operation does not accept --query"),
            ("changed-symbols", None, (sha,), None, None,
             "selected query operation does not accept --result-id"),
            ("changed-symbols", None, (), "callers", None,
             "selected query operation does not accept --direction"),
            ("impact-candidates", None, (), "importers", None,
             "selected query operation does not accept --direction"),
            ("changed-symbols", None, (), None, "", "selected change base is invalid"),
            ("changed-symbols", None, (), None, "   ", "selected change base is invalid"),
            ("changed-symbols", None, (), None, "a" * 513, "selected change base is invalid"),
            ("impact-candidates", None, (), None, "bad\nref", "selected change base is invalid"),
        ):
            with self.subTest(operation=operation, base=base):
                with self.assertRaises(PrepareCLIError) as caught:
                    validate_query_request(operation, query, ids, direction, base)
                self.assertEqual(str(caught.exception), message)


class OverviewQueryRequestTests(unittest.TestCase):
    """The overview names no query, no anchor, no direction, and no base."""

    def test_the_overview_accepts_no_query_anchor_direction_or_base(self) -> None:
        sha = "sha256:" + "a" * 64
        self.assertEqual(validate_query_request("repository-overview", None, ()), (None, ()))
        for query, ids, direction, base, message in (
            ("main", (), None, None, "selected query operation does not accept --query"),
            (None, (sha,), None, None, "selected query operation does not accept --result-id"),
            (None, (), "callers", None, "selected query operation does not accept --direction"),
            (None, (), None, "origin/main", "selected query operation does not accept --base"),
        ):
            with self.subTest(message=message):
                with self.assertRaises(PrepareCLIError) as caught:
                    validate_query_request(
                        "repository-overview", query, ids, direction, base
                    )
                self.assertEqual(str(caught.exception), message)

    def test_the_overview_accepts_neither_symbol_shaped_filter(self) -> None:
        for symbol_kinds, source_types, message in (
            (["definition"], [], "selected query operation does not accept --symbol-kind"),
            ([], ["source"], "selected query operation does not accept --source-type"),
        ):
            with self.subTest(message=message):
                with self.assertRaises(PrepareCLIError) as caught:
                    validate_query_request(
                        "repository-overview",
                        None,
                        (),
                        symbol_kinds=symbol_kinds,
                        source_types=source_types,
                    )
                self.assertEqual(str(caught.exception), message)

    def test_every_other_operation_still_accepts_both_filters(self) -> None:
        for operation, query, ids in (
            ("repository-map", None, ()),
            ("search-symbols", "main", ()),
            ("changed-symbols", None, ()),
        ):
            with self.subTest(operation=operation):
                self.assertEqual(
                    validate_query_request(
                        operation,
                        query,
                        ids,
                        symbol_kinds=["definition"],
                        source_types=["source"],
                    ),
                    (query, ids),
                )


class OverviewBudgetTests(unittest.TestCase):
    """The table and the file layer share the budget, half of it to each."""

    def _group(self, path_prefix: str, **overrides: object) -> dict[str, object]:
        """One directory row with the exact nine keys the wire names."""
        row: dict[str, object] = {
            "path_prefix": path_prefix,
            "depth": 2,
            "file_count": 12,
            "definition_count": 120,
            "entry_point_count": 1,
            "document_count": 2,
            "configuration_count": 3,
            "languages": [],
            "representative_identity": None,
        }
        row.update(overrides)
        return row

    def _summary(
        self,
        group_count: int,
        finding_count: int = 0,
        *,
        folded: dict[str, object] | None = None,
        other_group_count: int = 0,
    ) -> dict[str, object]:
        groups = [self._group(f"tools/{index:02d}/") for index in range(group_count)]
        if folded is not None:
            groups.append(folded)
        return {
            "schema_version": "1",
            "mode": "query",
            "operation": "repository-overview",
            "status": "ready",
            "freshness": "exact",
            "index_identity": "sha256:" + "4" * 64,
            "findings": [
                {
                    "rank": index + 1,
                    "path": f"file{index:02d}.py",
                    "result_identity": "sha256:" + f"{index:064x}",
                }
                for index in range(finding_count)
            ],
            "returned_count": finding_count,
            "omitted_count": 0,
            "truncated": False,
            "output_characters": 0,
            "warnings": [],
            "required_authorizations": [],
            "next_safe_action": "use-index",
            "refresh": {"performed": False, "changed_path_count": 0, "duration_ms": 0},
            "groups": groups,
            "overview": {
                "root": "",
                "counted_file_count": 240,
                "other_group_count": other_group_count,
            },
        }

    def _length(self, value: dict[str, object]) -> int:
        return len(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )

    def _table_length(self, value: dict[str, object]) -> int:
        """The length of the table layer alone: its rows and its summary."""
        return self._length({"groups": value["groups"], "overview": value["overview"]})

    def _folded_one_row_at_a_time(
        self, summary: dict[str, object], budget: int
    ) -> dict[str, object]:
        """The reference fold: one row given up per measurement, no search."""
        reference = json.loads(json.dumps(summary))
        reported = 0
        for _attempt in range(8):
            measured = self._length(dict(reference, output_characters=reported))
            if measured == reported:
                break
            reported = measured
        if reported <= budget:
            # An answer already inside its budget is never folded, whatever
            # the two layers would have been entitled to.
            return reference
        table_budget = budget // OVERVIEW_BUDGET_SHARES
        while self._table_length(reference) > table_budget:
            groups = reference["groups"]
            keep = len(self._directory_rows(reference)) - 1
            folded = _overview_rows_folded_to(groups, keep)
            if folded is None:
                break
            rows, lost = folded
            reference["groups"] = rows
            reference["overview"] = dict(
                reference["overview"],
                other_group_count=int(reference["overview"]["other_group_count"]) + lost,
            )
        return reference

    def _directory_rows(self, fitted: dict[str, object]) -> list[dict[str, object]]:
        """The rows that still name a directory, so without the folded one."""
        groups = fitted["groups"]
        assert isinstance(groups, list)
        return [row for row in groups if row["path_prefix"] != "*"]

    def test_folding_turns_the_last_row_into_the_folded_row(self) -> None:
        groups = [
            self._group("src/", languages=[{"language": "Go", "file_count": 12}]),
            self._group(
                "web/",
                depth=1,
                file_count=3,
                definition_count=7,
                languages=[{"language": "Python", "file_count": 3}],
                representative_identity="sha256:" + "a" * 64,
            ),
        ]

        rows, lost = _overview_rows_folded_to(groups, 1)

        # The folded row keeps the nine keys, stands at the root depth, and
        # names no single file because it speaks for whole directories.
        self.assertEqual(lost, 1)
        self.assertEqual(
            rows[-1],
            {
                "path_prefix": "*",
                "depth": 0,
                "file_count": 3,
                "definition_count": 7,
                "entry_point_count": 1,
                "document_count": 2,
                "configuration_count": 3,
                "languages": [{"language": "Python", "file_count": 3}],
                "representative_identity": None,
            },
        )
        self.assertEqual([row["path_prefix"] for row in rows], ["src/", "*"])
        # The table it was handed is read, never written.
        self.assertEqual([row["path_prefix"] for row in groups], ["src/", "web/"])
        # One directory row is the floor: a table of nothing but `*` would
        # describe no repository at all, so the fold refuses.
        self.assertIsNone(_overview_rows_folded_to(rows, 0))
        self.assertIsNone(_overview_rows_folded_to(rows, 1))

    def test_folding_merges_into_the_row_the_engine_already_folded(self) -> None:
        # A 2.5.0 engine answers with a folded row of its own. A second `*`
        # row would be a table no reader could add up, so the tail merges into
        # the one that is already there.
        groups = [
            self._group("src/", languages=[{"language": "Go", "file_count": 12}]),
            self._group(
                "web/",
                file_count=3,
                definition_count=7,
                languages=[
                    {"language": "Python", "file_count": 2},
                    {"language": "Go", "file_count": 1},
                ],
                representative_identity="sha256:" + "a" * 64,
            ),
            self._group(
                "*",
                depth=0,
                file_count=5,
                definition_count=9,
                languages=[{"language": "Go", "file_count": 5}],
            ),
        ]

        rows, lost = _overview_rows_folded_to(groups, 1)

        self.assertEqual(lost, 1)
        self.assertEqual(
            rows,
            [
                self._group("src/", languages=[{"language": "Go", "file_count": 12}]),
                {
                    "path_prefix": "*",
                    "depth": 0,
                    "file_count": 8,
                    "definition_count": 16,
                    "entry_point_count": 2,
                    "document_count": 4,
                    "configuration_count": 6,
                    # Merged by language and ranked again: most files first,
                    # ties by name, exactly as a group's own list is ordered.
                    "languages": [
                        {"language": "Go", "file_count": 6},
                        {"language": "Python", "file_count": 2},
                    ],
                    "representative_identity": None,
                },
            ],
        )

    def test_the_merged_language_list_is_bounded_like_the_go_fold(self) -> None:
        # internal/render/render.go caps its merged language list at
        # policy.ProductionLimits().MaximumCollectionItems (64); the Python
        # fold must match, not merge an unbounded list.
        lists = [
            [{"language": f"lang{index:03d}", "file_count": 1}] for index in range(80)
        ]

        merged = _merge_overview_languages(*lists)

        self.assertEqual(len(merged), 64)
        # Ties by file_count (all 1) break by name, so the kept 64 are the
        # alphabetically-first 64 of the 80 candidates.
        self.assertEqual(
            [item["language"] for item in merged],
            [f"lang{index:03d}" for index in range(64)],
        )

    def test_each_layer_gets_at_most_half_of_the_budget(self) -> None:
        fitted = fit_overview_to_budget(self._summary(20, finding_count=6), 4000)

        self.assertLessEqual(fitted["output_characters"], 4000)
        self.assertEqual(fitted["output_characters"], self._length(fitted))
        self.assertEqual(fitted["warnings"], [])
        # The table folds only for as long as it is over its own half, so it
        # keeps as many rows as 2000 characters hold and the file layer is
        # never charged for them.
        self.assertLessEqual(self._table_length(fitted), 4000 // OVERVIEW_BUDGET_SHARES)
        self.assertEqual(len(self._directory_rows(fitted)), 9)
        self.assertEqual(fitted["groups"][-1]["path_prefix"], "*")
        # Every row the table lost is counted, so the reader can still add the
        # whole repository up.
        self.assertEqual(fitted["overview"]["other_group_count"], 11)
        # And the file layer, which fits in what the table left, pays nothing.
        self.assertEqual(fitted["returned_count"], 6)
        self.assertEqual(fitted["omitted_count"], 0)
        self.assertFalse(fitted["truncated"])

    def test_a_table_inside_its_half_is_never_folded_and_keeps_the_rest(self) -> None:
        # Six rows are far short of 2000 characters, so the table is handed on
        # exactly as the engine ranked it and the whole remainder of the budget
        # buys files - nineteen of them, not a fixed reserve of four.
        original = self._summary(6, finding_count=40)
        fitted = fit_overview_to_budget(original, 4000)

        self.assertLessEqual(fitted["output_characters"], 4000)
        self.assertEqual(fitted["output_characters"], self._length(fitted))
        self.assertEqual(fitted["warnings"], [])
        self.assertEqual(fitted["groups"], original["groups"])
        self.assertEqual(fitted["overview"]["other_group_count"], 0)
        self.assertNotIn("*", [row["path_prefix"] for row in fitted["groups"]])
        self.assertEqual(fitted["returned_count"], 19)
        self.assertEqual(fitted["omitted_count"], 21)
        self.assertTrue(fitted["truncated"])

    def test_the_file_layer_spends_only_what_the_table_left(self) -> None:
        fitted = fit_overview_to_budget(self._summary(20, finding_count=6), 2000)

        self.assertLessEqual(fitted["output_characters"], 2000)
        self.assertEqual(fitted["output_characters"], self._length(fitted))
        self.assertEqual(fitted["warnings"], [])
        # A thousand characters of table is three rows here; the six findings
        # fit in what is left, so none of them is dropped.
        self.assertLessEqual(self._table_length(fitted), 2000 // OVERVIEW_BUDGET_SHARES)
        self.assertEqual(len(self._directory_rows(fitted)), 3)
        self.assertEqual(fitted["overview"]["other_group_count"], 17)
        self.assertEqual(fitted["returned_count"], 6)

    def test_the_table_reaches_its_floor_before_the_file_layer_empties(self) -> None:
        fitted = fit_overview_to_budget(self._summary(20, finding_count=6), 1200)

        self.assertLessEqual(fitted["output_characters"], 1200)
        self.assertEqual(fitted["output_characters"], self._length(fitted))
        self.assertEqual(fitted["warnings"], [])
        # 600 characters hold no table at all, so the fold runs to its floor:
        # one directory row plus the row that speaks for the other nineteen.
        self.assertEqual(len(self._directory_rows(fitted)), 1)
        self.assertEqual(fitted["groups"][-1]["path_prefix"], "*")
        self.assertEqual(fitted["overview"]["other_group_count"], 19)
        # Only then does the file layer pay, and it says how much it lost.
        self.assertEqual(fitted["returned_count"], 2)
        self.assertTrue(fitted["truncated"])
        self.assertEqual(
            int(fitted["returned_count"]) + int(fitted["omitted_count"]), 6
        )

    def test_only_a_one_row_table_with_no_findings_reports_the_overrun(self) -> None:
        fitted = fit_overview_to_budget(self._summary(20, finding_count=6), 500)

        # Nothing is left to lose: one directory row, the folded row, and no
        # findings at all, so the overrun is reported rather than hidden.
        self.assertEqual(len(self._directory_rows(fitted)), 1)
        self.assertEqual(fitted["groups"][-1]["path_prefix"], "*")
        self.assertEqual(fitted["findings"], [])
        self.assertEqual(fitted["returned_count"], 0)
        self.assertEqual(fitted["omitted_count"], 6)
        self.assertTrue(fitted["truncated"])
        self.assertIn("output-budget-exceeded", fitted["warnings"])
        self.assertEqual(fitted["output_characters"], self._length(fitted))
        self.assertGreater(fitted["output_characters"], 500)

    def test_every_budget_either_fits_or_says_it_could_not(self) -> None:
        widths: list[int] = []
        for budget in (400, 800, 1200, 1600, 2000, 3000, 4000, 5000, 8000):
            with self.subTest(budget=budget):
                fitted = fit_overview_to_budget(
                    self._summary(20, finding_count=6), budget
                )
                widths.append(len(self._directory_rows(fitted)))

                self.assertEqual(fitted["output_characters"], self._length(fitted))
                if "output-budget-exceeded" in fitted["warnings"]:
                    # The warning is only honest once nothing is left to fold
                    # or drop.
                    self.assertEqual(len(self._directory_rows(fitted)), 1)
                    self.assertEqual(fitted["findings"], [])
                else:
                    self.assertLessEqual(fitted["output_characters"], budget)
                if 1 < len(self._directory_rows(fitted)) < 20:
                    # A table that gave rows up and is not at its one-row floor
                    # stopped exactly at its own half.
                    self.assertLessEqual(
                        self._table_length(fitted), budget // OVERVIEW_BUDGET_SHARES
                    )
                self.assertEqual(
                    len(self._directory_rows(fitted))
                    + int(fitted["overview"]["other_group_count"]),
                    20,
                )
        # A wider budget buys a wider table, which is the whole point of
        # sizing the table by the budget rather than by a fixed row count.
        self.assertEqual(widths, sorted(widths))

    def test_the_search_folds_exactly_where_folding_row_by_row_would(self) -> None:
        # The fast fold answers with the widest table that fits its share; the
        # reference below reaches the same table by measuring after every
        # single fold. Random tables, so the two are compared on shapes nobody
        # chose: mixed row widths, mixed languages, and a table the engine had
        # already folded once.
        generator = random.Random(20260905)
        languages = ("Go", "Python", "TypeScript", "Markdown", "JSON")
        for case in range(40):
            with self.subTest(case=case):
                count = generator.randrange(2, 40)
                groups = [
                    self._group(
                        "tools/" + "x" * generator.randrange(1, 24) + f"{index:02d}/",
                        depth=generator.randrange(1, 5),
                        file_count=generator.randrange(1, 500),
                        definition_count=generator.randrange(0, 5000),
                        languages=[
                            {"language": name, "file_count": generator.randrange(1, 40)}
                            for name in generator.sample(
                                languages, generator.randrange(0, len(languages))
                            )
                        ],
                    )
                    for index in range(count)
                ]
                summary = self._summary(0, finding_count=generator.randrange(0, 8))
                other = generator.choice((0, 3))
                if other:
                    groups.append(
                        self._group("*", depth=0, languages=[{"language": "Go", "file_count": 5}])
                    )
                summary["groups"] = groups
                summary["overview"]["other_group_count"] = other
                budget = generator.randrange(600, 12000)

                fitted = fit_overview_to_budget(summary, budget)
                reference = self._folded_one_row_at_a_time(summary, budget)

                self.assertEqual(fitted["groups"], reference["groups"])
                self.assertEqual(fitted["overview"], reference["overview"])

    def test_a_wide_table_folds_in_a_logarithmic_number_of_serializations(self) -> None:
        # The fold used to remeasure the whole answer once per folded row, so
        # a table at the wire's row bound cost thousands of serializations. A
        # binary search over the kept row count costs about log2(4096) of them,
        # which is what this counts - a measure that does not depend on how
        # fast the machine running the tests happens to be.
        summary = self._summary(0, finding_count=6)
        summary["groups"] = [self._group(f"tools/{index:04d}/") for index in range(4096)]
        original = context_operations._canonical_length
        serializations = 0

        def counted(value: dict[str, object]) -> int:
            nonlocal serializations
            serializations += 1
            return original(value)

        with patch.object(context_operations, "_canonical_length", counted):
            fitted = fit_overview_to_budget(summary, 4000)

        self.assertLess(serializations, 100)
        self.assertLessEqual(fitted["output_characters"], 4000)
        self.assertEqual(fitted["output_characters"], self._length(fitted))
        self.assertEqual(
            len(self._directory_rows(fitted))
            + int(fitted["overview"]["other_group_count"]),
            4096,
        )

    def test_a_wide_table_folds_well_inside_a_second(self) -> None:
        # The generous bound is a sanity check, not a benchmark: the row-by-row
        # fold took about a minute for this table, so anything in this range
        # says the search is doing the work.
        summary = self._summary(0, finding_count=6)
        summary["groups"] = [self._group(f"tools/{index:04d}/") for index in range(4096)]

        started = time.perf_counter()
        fitted = fit_overview_to_budget(summary, 4000)
        elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 1.0)
        self.assertLessEqual(fitted["output_characters"], 4000)

    def test_a_table_inside_the_budget_is_never_folded(self) -> None:
        original = self._summary(20, finding_count=6)
        fitted = fit_overview_to_budget(original, 12000)

        self.assertEqual(fitted["groups"], original["groups"])
        self.assertEqual(fitted["overview"]["other_group_count"], 0)
        self.assertEqual(fitted["returned_count"], 6)
        self.assertEqual(fitted["warnings"], [])
        self.assertEqual(fitted["output_characters"], self._length(fitted))
        self.assertLessEqual(fitted["output_characters"], 12000)

    def test_folding_leaves_the_answer_it_was_handed_alone(self) -> None:
        original = self._summary(20, finding_count=6)

        fit_overview_to_budget(original, 4000)

        self.assertEqual(len(original["groups"]), 20)
        self.assertEqual(original["overview"]["other_group_count"], 0)
        self.assertEqual(len(original["findings"]), 6)

    def test_a_table_inside_the_budget_only_reports_its_length(self) -> None:
        fitted = fit_overview_to_budget(self._summary(1), 12000)

        self.assertEqual(fitted["warnings"], [])
        self.assertEqual(fitted["output_characters"], self._length(fitted))
        self.assertLessEqual(fitted["output_characters"], 12000)

    def test_the_reported_length_counts_the_field_that_reports_it(self) -> None:
        original = self._summary(3)
        fitted = fit_overview_to_budget(original, 12000)

        self.assertEqual(fitted["groups"], original["groups"])
        self.assertEqual(fitted["overview"], original["overview"])
        self.assertEqual(
            fitted["output_characters"],
            self._length(dict(fitted, output_characters=fitted["output_characters"])),
        )


class NormalizeChangeBaseTests(unittest.TestCase):
    def test_a_padded_base_is_stripped_for_every_surface(self) -> None:
        # The CLI and the MCP server must resolve the same base for the same
        # request, so the normalization lives in one place.
        self.assertIsNone(normalize_change_base(None))
        self.assertEqual(normalize_change_base(" origin/main\t"), "origin/main")
        self.assertEqual(normalize_change_base(" " + "a" * 512 + " "), "a" * 512)

    def test_a_base_no_ref_could_match_is_refused(self) -> None:
        for base in ("", "   ", "\n", "a" * 513, "bad\nref", "bad\rref", "bad\x00ref"):
            with self.subTest(base=base):
                with self.assertRaises(PrepareCLIError) as caught:
                    normalize_change_base(base)
                self.assertEqual(str(caught.exception), "selected change base is invalid")


if __name__ == "__main__":
    unittest.main()
