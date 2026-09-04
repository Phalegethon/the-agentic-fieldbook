"""Tests for the transport seam and the extracted broker operations."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from taf_context.change_ranges import ChangedPath
from taf_context.context_operations import (
    MAXIMUM_REQUEST_BYTES,
    PrepareCLIError,
    QueryArguments,
    bound_changed_selector,
    compose_impact_candidates,
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
from .test_prepare_cli import write_fake_native_engine


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
        self.assertLessEqual(int(trimmed["output_characters"]), 12000)
        self.assertEqual(
            int(trimmed["output_characters"]),
            len(json.dumps(trimmed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
        )

    def test_the_changed_list_shrinks_to_identities_before_a_candidate_is_dropped(self) -> None:
        composed = self._object()
        full = int(trim_to_budget(composed, 12000)["output_characters"])
        trimmed = trim_to_budget(composed, full - 40)

        self.assertEqual(len(trimmed["findings"]), len(composed["findings"]))
        self.assertEqual(
            trimmed["changed"],
            [{"result_identity": item["result_identity"]} for item in composed["changed"]],
        )
        self.assertIs(trimmed["truncated"], False)

    def test_candidates_are_dropped_from_the_tail_once_the_changed_list_is_bare(self) -> None:
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

    def test_the_changed_list_is_trimmed_before_the_last_candidate_goes(self) -> None:
        # The identity-only changed list alone exceeds the default budget at
        # the internal cap, so the candidates - the operation's answer - must
        # survive it.
        composed = self._wide_object(64, 1)
        trimmed = trim_to_budget(composed, 2000)

        self.assertLessEqual(int(trimmed["output_characters"]), 2000)
        self.assertEqual(len(trimmed["findings"]), 1)
        self.assertEqual(trimmed["returned_count"], 1)
        self.assertIn("changed-list-trimmed", trimmed["warnings"])
        self.assertNotIn("output-budget-exceeded", trimmed["warnings"])
        self.assertLess(len(trimmed["changed"]), 64)
        self.assertEqual(trimmed["changed_count"], 64)
        self.assertEqual(
            trimmed["changed"],
            [
                {"result_identity": item["result_identity"]}
                for item in composed["changed"][: len(trimmed["changed"])]
            ],
        )
        self.assertIs(trimmed["truncated"], False)
        self.assertEqual(trimmed["omitted_count"], composed["omitted_count"])

    def test_candidates_go_only_after_the_changed_list_is_empty(self) -> None:
        composed = self._wide_object(64, 4)
        trimmed = trim_to_budget(composed, 2000)

        self.assertLessEqual(int(trimmed["output_characters"]), 2000)
        self.assertEqual(trimmed["changed"], [])
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

    def test_a_budget_nothing_fits_in_is_reported_instead_of_looping(self) -> None:
        composed = self._wide_object(8, 2)
        trimmed = trim_to_budget(composed, 1)

        self.assertEqual(trimmed["changed"], [])
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

    def test_the_budget_holds_whenever_it_is_not_reported_as_exceeded(self) -> None:
        for changed_symbols, candidates in ((0, 0), (3, 2), (64, 1), (64, 8)):
            composed = self._wide_object(changed_symbols, candidates)
            for budget in (1, 300, 900, 2000, 4000, 12000):
                with self.subTest(changed=changed_symbols, candidates=candidates, budget=budget):
                    trimmed = trim_to_budget(composed, budget)
                    measured = len(json.dumps(
                        trimmed, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    ))
                    self.assertEqual(int(trimmed["output_characters"]), measured)
                    if "output-budget-exceeded" not in trimmed["warnings"]:
                        self.assertLessEqual(measured, budget)


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
    """The group table is never trimmed; an overrun is reported instead."""

    def _summary(self, group_count: int, finding_count: int = 0) -> dict[str, object]:
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
            "groups": [
                {
                    "path_prefix": f"tools/{index:02d}/",
                    "depth": 2,
                    "file_count": 12,
                    "definition_count": 120,
                    "entry_point_count": 1,
                    "document_count": 2,
                    "configuration_count": 3,
                    "languages": [],
                    "representative_identity": None,
                }
                for index in range(group_count)
            ],
            "overview": {
                "root": "",
                "counted_file_count": 240,
                "other_group_count": 4,
            },
        }

    def _length(self, value: dict[str, object]) -> int:
        return len(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )

    def test_a_seventeen_row_table_over_the_smallest_budget_is_reported(self) -> None:
        fitted = fit_overview_to_budget(self._summary(17, finding_count=4), 2000)

        self.assertEqual(len(fitted["groups"]), 17)
        self.assertIn("output-budget-exceeded", fitted["warnings"])
        self.assertEqual(fitted["output_characters"], self._length(fitted))
        self.assertGreater(fitted["output_characters"], 2000)
        # Zero findings is not enough for the table alone to fit, so every
        # finding is dropped and the omission is reported honestly.
        self.assertEqual(fitted["findings"], [])
        self.assertEqual(fitted["returned_count"], 0)
        self.assertTrue(fitted["truncated"])
        self.assertEqual(fitted["omitted_count"], 4)

    def test_a_middle_band_answer_drops_only_enough_findings_to_fit(self) -> None:
        fitted = fit_overview_to_budget(self._summary(17, finding_count=4), 4000)

        self.assertEqual(len(fitted["groups"]), 17)
        self.assertEqual(fitted["warnings"], [])
        self.assertEqual(fitted["output_characters"], self._length(fitted))
        self.assertLessEqual(fitted["output_characters"], 4000)
        # The table plus overview fits under 4000 on its own, so only enough
        # findings are dropped from the tail to bring the whole answer under
        # budget; the survivors keep their original, contiguous ranks.
        self.assertEqual([item["rank"] for item in fitted["findings"]], [1, 2])
        self.assertEqual(fitted["returned_count"], 2)
        self.assertEqual(fitted["omitted_count"], 2)
        self.assertTrue(fitted["truncated"])

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
