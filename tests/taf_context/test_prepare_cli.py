"""User-facing tests for preparing bounded repository context."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from io import StringIO
import json
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

from taf_context import engine_session
from taf_context.cli import main
from taf_context.context_operations import QueryArguments, run_query
from taf_context.git_snapshot import collect_snapshot
from taf_context.mcp_server import _query_arguments
from taf_context.native_transport import OneShotTransport
from taf_context.prepare_cli import (
    FILTER_LANGUAGES,
    FILTER_SYMBOL_KINDS,
    PrepareCLIError,
    _platform_asset,
    _state_paths,
    _validate_query_arguments,
    normalize_filter_values,
    register_prepare_command,
)
from taf_context.refresh import CHANGE_DOCUMENT_NAME

from .repo_factory import commit_all, init_committed_repo, write


ROOT = Path(__file__).parents[2]


FIXED_NOW = datetime(2026, 8, 30, 18, 0, 0, tzinfo=timezone.utc)


def invoke(environment: dict[str, str], *argv: str) -> tuple[int, str, str]:
    stdout = StringIO()
    stderr = StringIO()
    code = main(
        list(argv),
        stdout=stdout,
        stderr=stderr,
        utc_clock=lambda: FIXED_NOW,
        environment=environment,
    )
    return code, stdout.getvalue(), stderr.getvalue()


def decoded(stdout: str) -> dict[str, object]:
    value = json.loads(stdout)
    if not isinstance(value, dict):
        raise AssertionError("stdout was not a JSON object")
    return value


def write_fake_native_engine(
    path: Path,
    invocation_log: Path | None = None,
    *,
    partial: bool = False,
    stale: bool = False,
    snippet_stale: bool = False,
    update_outcome: str = "ready",
) -> None:
    source = textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import hashlib
            import json
            from pathlib import Path
            import sys

            invocation_log = __INVOCATION_LOG__

            def answer(envelope):
                request = envelope["request"]
                operation = request["operation"]
                state = Path(envelope["state_root"])
                if invocation_log is not None:
                    with Path(invocation_log).open("a", encoding="utf-8") as stream:
                        stream.write(operation + "\\n")
                if operation == "build":
                    state.mkdir(parents=True, exist_ok=True)
                    (state / "fake-index").write_text("ready", encoding="utf-8")
                ready = operation == "build" or (
                    operation in {
                        "status",
                        "repository-map",
                        "search-symbols",
                        "search-docs",
                        "source-snippets",
                        "related-symbols",
                        "changed-symbols",
                        "repository-overview",
                    }
                    and (state / "fake-index").is_file()
                )
                CHANGED_TABLE = {
                    "app.py": [
                        ("module", "app", 1, 40),
                        ("definition", "app.first", 3, 9),
                        ("definition", "app.second", 11, 20),
                    ],
                    "web.py": [("definition", "web.handle", 5, 12)],
                }
                CALLERS = {"app.first": [("definition", "web.py", "web.handle", 5, 12, "call")]}
                IMPORTERS = {"app": [("import", "other.py", "app", 1, 1, "import")]}

                def fixture_identity(path, name):
                    return "sha256:" + hashlib.sha256((path + "\\x00" + name).encode("utf-8")).hexdigest()

                NAMES = {}
                for fixture_path, fixture_records in CHANGED_TABLE.items():
                    for _kind, _name, _start, _end in fixture_records:
                        NAMES[fixture_identity(fixture_path, _name)] = _name

                def fixture_finding(kind, path, name, start, end, relation=""):
                    return {
                        "rank": 0,
                        "result_identity": fixture_identity(path, name),
                        "path": path,
                        "start_line": start,
                        "end_line": end,
                        "language": "Python",
                        "record_kind": kind,
                        "source_type": "source",
                        "qualified_name": name,
                        "extraction_method": "fake-engine",
                        "evidence_class": "verified",
                        "preview": "",
                        "relation": relation,
                        "edge_evidence": "verified" if relation else "",
                        "reference_line": 12 if relation else 0,
                        "reference_count": 2 if relation else 0,
                    }
                if operation == "update":
                    document_path = state / envelope["changed_paths_document"]
                    document = json.loads(document_path.read_bytes().decode("utf-8"))
                    required = {"schema_version", "prior_index_identity", "before_repository_identity",
                                "before_worktree_identity", "before_committed_head", "before_dirty_overlay_fingerprint",
                                "after_repository_identity", "after_worktree_identity", "after_committed_head",
                                "after_dirty_overlay_fingerprint", "level0_change_manifest_identity", "changed_paths"}
                    fields = {key: value for key, value in document.items() if key != "level0_change_manifest_identity"}
                    text = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                    for raw, escaped in (("<", "\\\\u003c"), (">", "\\\\u003e"), ("&", "\\\\u0026"), (" ", "\\\\u2028"), (" ", "\\\\u2029")):
                        text = text.replace(raw, escaped)
                    expected = "sha256:" + hashlib.sha256(b"taf-level0-change-manifest-v1\\x00" + text.encode("utf-8")).hexdigest()
                    valid = (
                        set(document) == required
                        and document["level0_change_manifest_identity"] == expected
                        and document["prior_index_identity"] == request["index_identity"]
                        and document["after_committed_head"] == request["committed_head"]
                        and document["after_dirty_overlay_fingerprint"] == request["dirty_overlay_fingerprint"]
                        and (state / "fake-index").is_file()
                    )
                    outcome = __UPDATE_OUTCOME__ if valid else "stale"
                    new_identity = "sha256:" + hashlib.sha256(("fake-index:" + request["dirty_overlay_fingerprint"] + request["committed_head"]).encode()).hexdigest()
                    ready = outcome == "ready"
                    payload_override = {
                        "ready": {"status": "ready", "freshness": "exact", "next_safe_action": "use-index", "index_identity": new_identity},
                        "stale": {"status": "stale", "freshness": "structurally-stale", "next_safe_action": "rebuild-index", "index_identity": request["index_identity"]},
                        "error": {"status": "error", "freshness": "unknown", "next_safe_action": "rebuild-index", "index_identity": request["index_identity"]},
                    }[outcome]
                payload = {
                    "schema_version": request["schema_version"],
                    "request_identity": request["request_identity"],
                    "operation": operation,
                    "status": "partial" if ready and __PARTIAL__ else "ready" if ready else "partial",
                    "provider_identity": "taf-context",
                    "provider_version": "0.1.1",
                    "index_identity": (
                        "sha256:" + hashlib.sha256(b"fake-index").hexdigest()
                        if operation == "build" else request["index_identity"]
                        if ready else None
                    ),
                    "repository_identity": request["repository_identity"],
                    "worktree_identity": request["worktree_identity"],
                    "committed_head": request["committed_head"],
                    "dirty_overlay_fingerprint": request["dirty_overlay_fingerprint"],
                    "freshness": "exact" if ready else "partial",
                    "parser_versions": {},
                    "coverage": {
                        "path_coverage": 1.0,
                        "language_coverage": 1.0,
                        "indexed_path_count": 1,
                        "excluded_path_count": 0,
                        "unsupported_language_count": 0,
                        "parse_failure_count": 0,
                        "exclusion_reason_counts": {"incomplete-extraction": 1} if __PARTIAL__ else {},
                    },
                    "findings": [],
                    "returned_count": 0,
                    "omitted_count": 0,
                    "truncated": False,
                    "output_characters": 0,
                    "warnings": ["json-collection-limit"] if ready and __PARTIAL__ else [],
                    "next_safe_action": "use-index" if ready else "build-index",
                }
                if request["schema_version"] == "4":
                    # Every schema-4 result carries the table, refusals with an
                    # empty one, exactly as the engine does.
                    payload["groups"] = []
                    payload["overview"] = {
                        "root": "",
                        "counted_file_count": 0,
                        "other_group_count": 0,
                    }
                if operation == "update":
                    payload.update(payload_override)
                if __STALE__ and operation in {
                    "status",
                    "repository-map",
                    "search-symbols",
                    "search-docs",
                    "source-snippets",
                    "related-symbols",
                    "changed-symbols",
                    "repository-overview",
                }:
                    payload["status"] = "stale"
                    payload["freshness"] = "incrementally-stale"
                    payload["next_safe_action"] = "rebuild-index"
                if __SNIPPET_STALE__ and operation in {"source-snippets", "related-symbols"}:
                    # Mirrors the engine's snippetStale helper: an exact index
                    # that cannot verify the requested result identities, or an
                    # anchor no relationship may start from, reports stale with
                    # structurally-stale and update-index. A genuinely stale index
                    # (above) answers rebuild-index instead, which is what tells
                    # the two apart.
                    payload["status"] = "stale"
                    payload["freshness"] = "structurally-stale"
                    payload["next_safe_action"] = "update-index"
                def fixture_answer(found):
                    found.sort(key=lambda item: (item["path"], item["start_line"], item["qualified_name"]))
                    for rank, item in enumerate(found, start=1):
                        item["rank"] = rank
                    payload["findings"] = found
                    payload["returned_count"] = len(found)
                    payload["output_characters"] = 200 if found else 0

                if (
                    operation == "related-symbols"
                    and request["schema_version"] == "2"
                    and ready
                    and payload["status"] in {"ready", "partial"}
                ):
                    # One resolved edge, so the broker and the CLI carry the four
                    # schema-2 finding fields end to end. An anchor the change
                    # fixture knows answers from its own table instead, so the
                    # composition can be followed end to end.
                    direction = request["direction"]
                    table = {"callers": CALLERS, "importers": IMPORTERS}.get(direction, {})
                    related_found = {}
                    for identity in request["result_identities"]:
                        name = NAMES.get(identity)
                        if name is None:
                            if direction == "callers":
                                legacy = fixture_finding(
                                    "definition", "tools/example/caller.py", "caller.run", 10, 14, "call"
                                )
                                legacy["result_identity"] = "sha256:" + "e" * 64
                                related_found[legacy["result_identity"]] = legacy
                            continue
                        for kind, path, related_name, start, end, relation in table.get(name, []):
                            item = fixture_finding(kind, path, related_name, start, end, relation)
                            related_found[item["result_identity"]] = item
                    fixture_answer(list(related_found.values()))
                if (
                    operation == "changed-symbols"
                    and request["schema_version"] == "3"
                    and ready
                    and payload["status"] in {"ready", "partial"}
                ):
                    changed_found = []
                    for entry in request["changed_ranges"]:
                        for kind, name, start, end in CHANGED_TABLE.get(entry["path"], []):
                            spans = entry["ranges"]
                            if spans and not any(start <= high and end >= low for low, high in spans):
                                continue
                            changed_found.append(
                                fixture_finding(kind, entry["path"], name, start, end)
                            )
                    fixture_answer(changed_found)
                if (
                    operation == "repository-overview"
                    and request["schema_version"] == "4"
                    and ready
                    and payload["status"] in {"ready", "partial"}
                ):
                    # A canned two-group answer: the root files and one
                    # directory, each represented by its first ranked file.
                    overview_found = [
                        fixture_finding("module", "app.py", "app", 1, 40),
                        fixture_finding(
                            "definition", "web/handler.py", "web.handler.handle", 5, 12
                        ),
                    ]
                    fixture_answer(overview_found)
                    payload["groups"] = [
                        {
                            "path_prefix": ".",
                            "depth": 0,
                            "file_count": 1,
                            "definition_count": 2,
                            "entry_point_count": 0,
                            "document_count": 0,
                            "configuration_count": 0,
                            "languages": [{"language": "Python", "file_count": 1}],
                            "representative_identity": fixture_identity("app.py", "app"),
                        },
                        {
                            "path_prefix": "web/",
                            "depth": 1,
                            "file_count": 1,
                            "definition_count": 1,
                            "entry_point_count": 0,
                            "document_count": 0,
                            "configuration_count": 0,
                            "languages": [{"language": "Python", "file_count": 1}],
                            "representative_identity": fixture_identity(
                                "web/handler.py", "web.handler.handle"
                            ),
                        },
                    ]
                    payload["overview"] = {
                        "root": "",
                        "counted_file_count": 2,
                        "other_group_count": 0,
                    }
                return payload

            def respond(line):
                sys.stdout.write(
                    json.dumps(answer(json.loads(line)), sort_keys=True, separators=(",", ":")) + "\\n"
                )
                sys.stdout.flush()

            if sys.argv[1:] == ["--serve"]:
                # One `--serve` child answers many requests on one pair of
                # pipes, exactly as the real engine's session mode does.
                if invocation_log is not None:
                    with Path(invocation_log).open("a", encoding="utf-8") as stream:
                        stream.write("serve\\n")
                sys.stderr.write("__TAF_LEVEL1_SERVER_READY_V1__\\n")
                sys.stderr.flush()
                for served in sys.stdin.buffer:
                    if served.strip():
                        respond(served)
            elif sys.argv[1:]:
                sys.stderr.write("invalid-native-level1-request\\n")
                sys.exit(2)
            else:
                respond(sys.stdin.read())
            """
        ).replace("__INVOCATION_LOG__", repr(None if invocation_log is None else str(invocation_log))).replace(
            "__PARTIAL__", repr(partial)
        ).replace("__STALE__", repr(stale)).replace("__SNIPPET_STALE__", repr(snippet_stale)).replace(
            "__UPDATE_OUTCOME__", repr(update_outcome)
        )
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


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


class PrepareRepoContextCommandTests(unittest.TestCase):
    def test_explicit_state_home_does_not_require_posix_home_variable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = _state_paths({"TAF_STATE_HOME": directory})

        self.assertEqual(paths.root, Path(directory))

    def test_unpublished_windows_arm_runtime_is_reported_as_unsupported(self) -> None:
        # platform is imported locally inside _platform_asset (kept off the query
        # path), so patch the real module rather than a module attribute.
        with mock.patch("taf_context.context_operations.sys.platform", "win32"), mock.patch(
            "platform.machine", return_value="ARM64"
        ):
            with self.assertRaisesRegex(PrepareCLIError, "unsupported"):
                _platform_asset()

    def test_activate_downloads_verified_runtime_builds_context_and_is_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            state_home = root / "state"
            release = root / "release"
            release.mkdir()
            source_binary = root / "source-taf-level1"
            invocation_log = root / "native-invocations.log"
            write_fake_native_engine(source_binary, invocation_log)
            payload = source_binary.read_bytes()
            for system in ("darwin", "linux", "windows"):
                for machine in ("amd64", "arm64"):
                    suffix = ".exe" if system == "windows" else ""
                    name = f"taf-level1_0.5.0_{system}_{machine}{suffix}"
                    (release / name).write_bytes(payload)
                    (release / f"{name}.sha256").write_text(
                        f"{hashlib.sha256(payload).hexdigest()}  {name}\n",
                        encoding="ascii",
                    )
            environment = {
                "HOME": str(root / "home"),
                "PATH": "",
                "TAF_NATIVE_RELEASE_BASE_URL": "http://attacker.invalid",
                "TAF_STATE_HOME": str(state_home),
            }

            denied = invoke(
                environment,
                "prepare",
                "activate",
                "--repo",
                str(repo),
            )
            self.assertEqual(denied[0], 2)
            self.assertIn("explicit network confirmation required", denied[2])
            self.assertFalse(state_home.exists())

            with mock.patch(
                "taf_context.prepare_cli._NATIVE_RELEASE_BASE_URL",
                release.as_uri(),
                create=True,
            ):
                code, stdout, stderr = invoke(
                    environment,
                    "prepare",
                    "activate",
                    "--repo",
                    str(repo),
                    "--confirm-network",
                    "--confirm-state-write",
                )
            self.assertEqual((code, stderr), (0, ""))
            activated = decoded(stdout)
            self.assertEqual(activated["mode"], "activate")
            self.assertEqual(activated["context"]["status"], "ready")
            self.assertEqual(activated["next_safe_action"], "use-index")
            self.assertEqual(activated["required_authorizations"], [])

            code, stdout, stderr = invoke(
                {key: value for key, value in environment.items() if key != "TAF_NATIVE_RELEASE_BASE_URL"},
                "prepare",
                "inspect",
                "--repo",
                str(repo),
            )
            self.assertEqual((code, stderr), (0, ""))
            inspected = decoded(stdout)
            self.assertEqual(inspected["engine"]["source"], "managed")
            self.assertEqual(inspected["context"]["status"], "ready")
            self.assertEqual(inspected["next_safe_action"], "use-index")
            self.assertEqual(inspected["required_authorizations"], [])
            self.assertEqual(
                invocation_log.read_text(encoding="utf-8").splitlines(),
                ["build", "status"],
            )

    def test_ready_context_can_be_queried_without_rebuilding_or_estimating(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            native = root / "taf-level1"
            invocation_log = root / "native-invocations.log"
            write_fake_native_engine(native, invocation_log)
            environment = {
                "TAF_LEVEL1_BINARY": str(native),
                "TAF_STATE_HOME": str(root / "state"),
            }

            built = invoke(
                environment,
                "prepare",
                "build",
                "--repo",
                str(repo),
                "--confirm-state-write",
            )
            self.assertEqual((built[0], built[2]), (0, ""))

            code, stdout, stderr = invoke(
                environment,
                "prepare",
                "query",
                "--repo",
                str(repo),
                "--operation",
                "search-symbols",
                "--query",
                "Widget",
                "--maximum-results",
                "3",
            )

            self.assertEqual((code, stderr), (0, ""))
            result = decoded(stdout)
            self.assertEqual(result["mode"], "query")
            self.assertEqual(result["operation"], "search-symbols")
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["required_authorizations"], [])
            self.assertLessEqual(len(stdout), 4000)
            self.assertEqual(
                invocation_log.read_text(encoding="utf-8").splitlines(),
                ["build", "search-symbols"],
            )

    def test_related_symbols_query_carries_the_direction_and_the_edge_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            native = root / "taf-level1"
            invocation_log = root / "native-invocations.log"
            write_fake_native_engine(native, invocation_log)
            environment = {
                "TAF_LEVEL1_BINARY": str(native),
                "TAF_STATE_HOME": str(root / "state"),
            }
            anchor = "sha256:" + "a" * 64

            built = invoke(
                environment, "prepare", "build", "--repo", str(repo), "--confirm-state-write"
            )
            self.assertEqual((built[0], built[2]), (0, ""))

            code, stdout, stderr = invoke(
                environment,
                "prepare",
                "query",
                "--repo",
                str(repo),
                "--operation",
                "related-symbols",
                "--result-id",
                anchor,
                "--direction",
                "callers",
            )

            self.assertEqual((code, stderr), (0, ""))
            result = decoded(stdout)
            self.assertEqual(result["operation"], "related-symbols")
            self.assertEqual(result["status"], "ready")
            findings = result["findings"]
            assert isinstance(findings, list)
            self.assertEqual(
                {key: findings[0][key] for key in (
                    "relation", "edge_evidence", "reference_line", "reference_count"
                )},
                {
                    "relation": "call",
                    "edge_evidence": "verified",
                    "reference_line": 12,
                    "reference_count": 2,
                },
            )
            self.assertEqual(
                invocation_log.read_text(encoding="utf-8").splitlines(),
                ["build", "related-symbols"],
            )

    def test_repository_overview_query_carries_the_group_table(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            native = root / "taf-level1"
            invocation_log = root / "native-invocations.log"
            write_fake_native_engine(native, invocation_log)
            environment = {
                "TAF_LEVEL1_BINARY": str(native),
                "TAF_STATE_HOME": str(root / "state"),
            }
            built = invoke(
                environment, "prepare", "build", "--repo", str(repo), "--confirm-state-write"
            )
            self.assertEqual((built[0], built[2]), (0, ""))

            code, stdout, stderr = invoke(
                environment,
                "prepare",
                "query",
                "--repo",
                str(repo),
                "--operation",
                "repository-overview",
                "--path-prefix",
                "web",
                "--maximum-output-characters",
                "12000",
            )

            self.assertEqual((code, stderr), (0, ""))
            result = decoded(stdout)
            self.assertEqual(result["operation"], "repository-overview")
            self.assertEqual(result["status"], "ready")
            self.assertEqual(
                [group["path_prefix"] for group in result["groups"]], [".", "web/"]
            )
            self.assertEqual(
                result["groups"][0]["representative_identity"],
                result["findings"][0]["result_identity"],
            )
            self.assertEqual(
                result["overview"],
                {"root": "", "counted_file_count": 2, "other_group_count": 0},
            )
            self.assertEqual(result["warnings"], [])
            self.assertEqual(
                invocation_log.read_text(encoding="utf-8").splitlines(),
                ["build", "repository-overview"],
            )

    def test_the_overview_accepts_no_symbol_shaped_filter_and_no_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            native = root / "taf-level1"
            write_fake_native_engine(native)
            environment = {
                "TAF_LEVEL1_BINARY": str(native),
                "TAF_STATE_HOME": str(root / "state"),
            }
            invoke(environment, "prepare", "build", "--repo", str(repo), "--confirm-state-write")

            for extra, message in (
                (["--symbol-kind", "definition"], "does not accept --symbol-kind"),
                (["--source-type", "source"], "does not accept --source-type"),
                (["--query", "main"], "does not accept --query"),
                (["--result-id", "sha256:" + "a" * 64], "does not accept --result-id"),
                (["--base", "origin/main"], "does not accept --base"),
            ):
                with self.subTest(extra=extra):
                    code, stdout, stderr = invoke(
                        environment,
                        "prepare", "query", "--repo", str(repo),
                        "--operation", "repository-overview", *extra,
                    )
                    self.assertEqual(code, 2)
                    self.assertEqual(stdout, "")
                    self.assertIn(message, stderr)

    def test_changed_symbols_query_reports_its_base_and_the_touched_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, base = change_fixture(root)
            native = root / "taf-level1"
            invocation_log = root / "native-invocations.log"
            write_fake_native_engine(native, invocation_log)
            environment = {
                "TAF_LEVEL1_BINARY": str(native),
                "TAF_STATE_HOME": str(root / "state"),
            }
            invoke(environment, "prepare", "build", "--repo", str(repo), "--confirm-state-write")

            code, stdout, stderr = invoke(
                environment,
                "prepare", "query", "--repo", str(repo),
                "--operation", "changed-symbols", "--base", base,
            )

            self.assertEqual((code, stderr), (0, ""))
            result = decoded(stdout)
            self.assertEqual(result["operation"], "changed-symbols")
            self.assertEqual(result["status"], "ready")
            self.assertEqual(
                result["base"],
                {"requested": base, "ref": base, "sha": base, "source": "explicit", "warning": None},
            )
            findings = result["findings"]
            assert isinstance(findings, list)
            self.assertEqual(
                [(item["qualified_name"], item["start_line"]) for item in findings],
                [("app", 1), ("app.first", 3)],
            )
            self.assertEqual(result["warnings"], [])
            # A change query answers over one served child, not a process per
            # call, so the log records the session before the operation.
            self.assertEqual(
                invocation_log.read_text(encoding="utf-8").splitlines(),
                ["build", "serve", "changed-symbols"],
            )

    def test_a_stale_index_refuses_the_change_operations(self) -> None:
        # A change query names no result identity, so the identity refusal
        # cannot apply and a stale index is the only refusal left; both
        # operations must give the same answer the direct ones give.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, base = change_fixture(root)
            fresh_binary = root / "taf-level1"
            write_fake_native_engine(fresh_binary)
            stale_binary = root / "taf-level1-stale"
            write_fake_native_engine(stale_binary, stale=True)
            environment = {
                "TAF_LEVEL1_BINARY": str(fresh_binary),
                "TAF_STATE_HOME": str(root / "state"),
            }
            code, _stdout, stderr = invoke(
                environment, "prepare", "build", "--repo", str(repo), "--confirm-state-write"
            )
            self.assertEqual((code, stderr), (0, ""))

            environment["TAF_LEVEL1_BINARY"] = str(stale_binary)
            for operation in ("changed-symbols", "impact-candidates"):
                with self.subTest(operation=operation):
                    code, stdout, stderr = invoke(
                        environment,
                        "prepare", "query", "--repo", str(repo),
                        "--operation", operation, "--base", base,
                    )

                    self.assertEqual((code, stdout), (2, ""))
                    self.assertIn("ready context is required; run prepare inspect", stderr)

    def test_a_padded_base_resolves_exactly_as_the_bare_one_does(self) -> None:
        # The MCP server strips a base before the broker sees it, so the CLI
        # must too: the same request cannot mean two things on two surfaces.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, base = change_fixture(root)
            native = root / "taf-level1"
            write_fake_native_engine(native)
            environment = {
                "TAF_LEVEL1_BINARY": str(native),
                "TAF_STATE_HOME": str(root / "state"),
            }
            invoke(environment, "prepare", "build", "--repo", str(repo), "--confirm-state-write")

            bare_code, bare_stdout, bare_stderr = invoke(
                environment,
                "prepare", "query", "--repo", str(repo),
                "--operation", "changed-symbols", "--base", base,
            )
            padded_code, padded_stdout, padded_stderr = invoke(
                environment,
                "prepare", "query", "--repo", str(repo),
                "--operation", "changed-symbols", "--base", f"  {base}\t",
            )

            self.assertEqual((bare_code, bare_stderr), (0, ""))
            self.assertEqual((padded_code, padded_stderr), (0, ""))
            self.assertEqual(padded_stdout, bare_stdout)
            self.assertEqual(decoded(padded_stdout)["base"]["requested"], base)

            blank_code, blank_stdout, blank_stderr = invoke(
                environment,
                "prepare", "query", "--repo", str(repo),
                "--operation", "impact-candidates", "--base", "   ",
            )
            self.assertEqual((blank_code, blank_stdout), (2, ""))
            self.assertIn("selected change base is invalid", blank_stderr)

    def test_impact_candidates_query_attributes_every_candidate_to_its_anchors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, base = change_fixture(root)
            native = root / "taf-level1"
            invocation_log = root / "native-invocations.log"
            write_fake_native_engine(native, invocation_log)
            environment = {
                "TAF_LEVEL1_BINARY": str(native),
                "TAF_STATE_HOME": str(root / "state"),
            }
            invoke(environment, "prepare", "build", "--repo", str(repo), "--confirm-state-write")

            code, stdout, stderr = invoke(
                environment,
                "prepare", "query", "--repo", str(repo),
                "--operation", "impact-candidates", "--base", base,
            )

            self.assertEqual((code, stderr), (0, ""))
            result = decoded(stdout)
            self.assertEqual(result["operation"], "impact-candidates")
            self.assertEqual((result["status"], result["next_safe_action"]), ("ready", "use-index"))
            self.assertEqual(result["changed_count"], 2)
            # The changed set's own omissions are reported next to its count;
            # this fixture returns every changed symbol, so it omits none.
            self.assertEqual(result["changed_omitted_count"], 0)
            keys = list(result)
            self.assertEqual(keys[keys.index("changed_count") + 1], "changed_omitted_count")
            self.assertEqual(
                [item["qualified_name"] for item in result["changed"]], ["app", "app.first"]
            )
            findings = result["findings"]
            assert isinstance(findings, list)
            self.assertEqual(
                [
                    (
                        item["qualified_name"],
                        item["record_kind"],
                        item["edge_evidence"],
                        [anchor["qualified_name"] for anchor in item["anchors"]],
                    )
                    for item in findings
                ],
                [
                    ("app", "import", "verified", ["app"]),
                    ("web.handle", "definition", "verified", ["app.first"]),
                ],
            )
            self.assertEqual(result["returned_count"], 2)
            self.assertEqual((result["omitted_count"], result["truncated"]), (0, False))
            self.assertEqual(
                int(result["output_characters"]), len(json.dumps(
                    result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ))
            )
            # One changed-symbols call, then one relationship call per anchor:
            # callers for the definition, importers for the module and the
            # definition.
            self.assertEqual(
                invocation_log.read_text(encoding="utf-8").splitlines(),
                [
                    "build",
                    "serve",
                    "changed-symbols",
                    "related-symbols",
                    "related-symbols",
                    "related-symbols",
                ],
            )

    def test_change_operation_argument_rules_are_reported_before_the_engine_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            native = root / "taf-level1"
            invocation_log = root / "native-invocations.log"
            write_fake_native_engine(native, invocation_log)
            environment = {
                "TAF_LEVEL1_BINARY": str(native),
                "TAF_STATE_HOME": str(root / "state"),
            }
            anchor = "sha256:" + "a" * 64
            invoke(environment, "prepare", "build", "--repo", str(repo), "--confirm-state-write")

            cases = (
                (
                    ("--operation", "search-symbols", "--query", "Widget", "--base", "HEAD"),
                    "selected query operation does not accept --base",
                ),
                (
                    ("--operation", "changed-symbols", "--query", "Widget"),
                    "selected query operation does not accept --query",
                ),
                (
                    ("--operation", "changed-symbols", "--result-id", anchor),
                    "selected query operation does not accept --result-id",
                ),
                (
                    ("--operation", "impact-candidates", "--direction", "callers"),
                    "selected query operation does not accept --direction",
                ),
            )
            for arguments, message in cases:
                with self.subTest(arguments=arguments):
                    code, _stdout, stderr = invoke(
                        environment, "prepare", "query", "--repo", str(repo), *arguments
                    )
                    self.assertEqual(code, 2)
                    self.assertIn(message, stderr)
            self.assertEqual(invocation_log.read_text(encoding="utf-8").splitlines(), ["build"])

    def test_an_unresolvable_change_base_is_refused_without_a_query(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            native = root / "taf-level1"
            invocation_log = root / "native-invocations.log"
            write_fake_native_engine(native, invocation_log)
            environment = {
                "TAF_LEVEL1_BINARY": str(native),
                "TAF_STATE_HOME": str(root / "state"),
            }
            invoke(environment, "prepare", "build", "--repo", str(repo), "--confirm-state-write")

            code, _stdout, stderr = invoke(
                environment,
                "prepare", "query", "--repo", str(repo),
                "--operation", "changed-symbols", "--base", "no-such-ref",
            )

            self.assertEqual(code, 2)
            self.assertIn("selected change base could not be resolved", stderr)
            self.assertEqual(invocation_log.read_text(encoding="utf-8").splitlines(), ["build"])

    def test_direction_and_anchor_rules_are_reported_before_the_engine_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            native = root / "taf-level1"
            invocation_log = root / "native-invocations.log"
            write_fake_native_engine(native, invocation_log)
            environment = {
                "TAF_LEVEL1_BINARY": str(native),
                "TAF_STATE_HOME": str(root / "state"),
            }
            anchor = "sha256:" + "a" * 64
            invoke(
                environment, "prepare", "build", "--repo", str(repo), "--confirm-state-write"
            )

            cases = (
                (
                    ("--operation", "related-symbols", "--result-id", anchor),
                    "related-symbols requires --direction",
                ),
                (
                    ("--operation", "related-symbols", "--direction", "callers"),
                    "related-symbols requires at least one --result-id",
                ),
                (
                    (
                        "--operation",
                        "search-symbols",
                        "--query",
                        "Widget",
                        "--direction",
                        "callers",
                    ),
                    "selected query operation does not accept --direction",
                ),
            )
            for arguments, message in cases:
                with self.subTest(arguments=arguments):
                    code, _stdout, stderr = invoke(
                        environment, "prepare", "query", "--repo", str(repo), *arguments
                    )
                    self.assertEqual(code, 2)
                    self.assertIn(message, stderr)
            self.assertEqual(
                invocation_log.read_text(encoding="utf-8").splitlines(), ["build"]
            )

    def test_partial_context_is_bound_inspectable_and_queryable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            native = root / "taf-level1"
            invocation_log = root / "native-invocations.log"
            write_fake_native_engine(native, invocation_log, partial=True)
            environment = {
                "TAF_LEVEL1_BINARY": str(native),
                "TAF_STATE_HOME": str(root / "state"),
            }

            code, stdout, stderr = invoke(
                environment,
                "prepare",
                "build",
                "--repo",
                str(repo),
                "--confirm-state-write",
            )
            self.assertEqual((code, stderr), (0, ""))
            built = decoded(stdout)
            self.assertEqual(built["context"]["status"], "partial")
            self.assertEqual(built["context"]["freshness"], "exact")
            self.assertEqual(built["next_safe_action"], "use-index")
            self.assertEqual(built["context"]["coverage"]["parse_failure_count"], 0)
            self.assertIn("json-collection-limit", built["warnings"])

            code, stdout, stderr = invoke(
                environment,
                "prepare",
                "inspect",
                "--repo",
                str(repo),
            )
            self.assertEqual((code, stderr), (0, ""))
            inspected = decoded(stdout)
            self.assertEqual(inspected["context"]["status"], "partial")
            self.assertEqual(inspected["next_safe_action"], "use-index")
            self.assertEqual(inspected["required_authorizations"], [])

            code, stdout, stderr = invoke(
                environment,
                "prepare",
                "query",
                "--repo",
                str(repo),
                "--operation",
                "repository-map",
            )
            self.assertEqual((code, stderr), (0, ""))
            queried = decoded(stdout)
            self.assertEqual(queried["status"], "partial")
            self.assertEqual(queried["next_safe_action"], "use-index")
            self.assertIn("json-collection-limit", queried["warnings"])
            self.assertEqual(
                invocation_log.read_text(encoding="utf-8").splitlines(),
                ["build", "status", "repository-map"],
            )

    def test_stale_query_result_is_an_error_and_does_not_touch_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            state_home = root / "state"
            fresh_binary = root / "taf-level1"
            write_fake_native_engine(fresh_binary)
            stale_binary = root / "taf-level1-stale"
            invocation_log = root / "native-invocations.log"
            write_fake_native_engine(stale_binary, invocation_log, stale=True)
            environment = {
                "HOME": str(root / "home"),
                "PATH": "",
                "TAF_LEVEL1_BINARY": str(fresh_binary),
                "TAF_STATE_HOME": str(state_home),
            }
            code, _stdout, stderr = invoke(environment, "prepare", "build", "--repo", str(repo), "--confirm-state-write")
            self.assertEqual((code, stderr), (0, ""))
            binding = next(state_home.glob("repositories/*/*/binding.json"))
            old = 1_600_000_000
            os.utime(binding, (old, old))

            environment["TAF_LEVEL1_BINARY"] = str(stale_binary)
            code, stdout, stderr = invoke(
                environment, "prepare", "query", "--repo", str(repo),
                "--operation", "search-symbols", "--query", "Widget",
            )

            self.assertEqual((code, stdout), (2, ""))
            self.assertIn("ready context is required", stderr)
            self.assertEqual(binding.stat().st_mtime, old)
            self.assertEqual(
                invocation_log.read_text(encoding="utf-8").splitlines(),
                ["search-symbols"],
            )

    def test_unverifiable_snippet_identities_are_refused_with_a_specific_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            state_home = root / "state"
            fresh_binary = root / "taf-level1"
            write_fake_native_engine(fresh_binary)
            snippet_stale_binary = root / "taf-level1-snippet-stale"
            invocation_log = root / "native-invocations.log"
            write_fake_native_engine(snippet_stale_binary, invocation_log, snippet_stale=True)
            environment = {
                "HOME": str(root / "home"),
                "PATH": "",
                "TAF_LEVEL1_BINARY": str(fresh_binary),
                "TAF_STATE_HOME": str(state_home),
            }
            code, _stdout, stderr = invoke(environment, "prepare", "build", "--repo", str(repo), "--confirm-state-write")
            self.assertEqual((code, stderr), (0, ""))
            binding = next(state_home.glob("repositories/*/*/binding.json"))
            old = 1_600_000_000
            os.utime(binding, (old, old))

            environment["TAF_LEVEL1_BINARY"] = str(snippet_stale_binary)
            result_id = "sha256:" + ("a" * 64)
            code, stdout, stderr = invoke(
                environment, "prepare", "query", "--repo", str(repo),
                "--operation", "source-snippets", "--result-id", result_id,
            )

            self.assertEqual((code, stdout), (2, ""))
            self.assertIn(
                "result identities could not be verified against the current index; re-run the search query",
                stderr,
            )
            self.assertEqual(binding.stat().st_mtime, old)
            code, stdout, stderr = invoke(
                environment, "prepare", "query", "--repo", str(repo),
                "--operation", "related-symbols", "--result-id", result_id,
                "--direction", "callers",
            )

            self.assertEqual((code, stdout), (2, ""))
            self.assertIn(
                "result identities could not be verified against the current index; re-run the search query",
                stderr,
            )
            self.assertEqual(binding.stat().st_mtime, old)
            self.assertEqual(
                invocation_log.read_text(encoding="utf-8").splitlines(),
                ["source-snippets", "related-symbols"],
            )

    def test_a_stale_index_is_reported_as_stale_for_identity_operations(self) -> None:
        # A genuinely stale index and an index that cannot use the requested
        # identities are different situations with different next steps, and
        # the engine tells them apart: only the identity case answers
        # "update-index". The identity operations must not misreport the first
        # as the second.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            state_home = root / "state"
            fresh_binary = root / "taf-level1"
            write_fake_native_engine(fresh_binary)
            stale_binary = root / "taf-level1-stale"
            write_fake_native_engine(stale_binary, stale=True)
            environment = {
                "HOME": str(root / "home"),
                "PATH": "",
                "TAF_LEVEL1_BINARY": str(fresh_binary),
                "TAF_STATE_HOME": str(state_home),
            }
            code, _stdout, stderr = invoke(environment, "prepare", "build", "--repo", str(repo), "--confirm-state-write")
            self.assertEqual((code, stderr), (0, ""))

            environment["TAF_LEVEL1_BINARY"] = str(stale_binary)
            result_id = "sha256:" + ("a" * 64)
            for argv in (
                ("--operation", "source-snippets", "--result-id", result_id),
                ("--operation", "related-symbols", "--result-id", result_id, "--direction", "callers"),
            ):
                code, stdout, stderr = invoke(
                    environment, "prepare", "query", "--repo", str(repo), *argv
                )
                self.assertEqual((code, stdout), (2, ""))
                self.assertIn("ready context is required; run prepare inspect", stderr)
                self.assertNotIn("result identities could not be verified", stderr)

    def test_query_requires_an_existing_exact_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            native = root / "taf-level1"
            invocation_log = root / "native-invocations.log"
            write_fake_native_engine(native, invocation_log)

            code, stdout, stderr = invoke(
                {
                    "TAF_LEVEL1_BINARY": str(native),
                    "TAF_STATE_HOME": str(root / "state"),
                },
                "prepare",
                "query",
                "--repo",
                str(repo),
                "--operation",
                "repository-map",
            )

            self.assertEqual((code, stdout), (2, ""))
            self.assertIn("ready context is required", stderr)
            self.assertFalse(invocation_log.exists())

    def test_activate_rejects_corrupt_download_without_installing_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            state_home = root / "state"
            release = root / "release"
            release.mkdir()
            for system in ("darwin", "linux", "windows"):
                for machine in ("amd64", "arm64"):
                    suffix = ".exe" if system == "windows" else ""
                    name = f"taf-level1_0.5.0_{system}_{machine}{suffix}"
                    (release / name).write_bytes(b"corrupt")
                    (release / f"{name}.sha256").write_text(
                        f"{'0' * 64}  {name}\n", encoding="ascii"
                    )
            environment = {
                "HOME": str(root / "home"),
                "PATH": "",
                "TAF_NATIVE_RELEASE_BASE_URL": "http://attacker.invalid",
                "TAF_STATE_HOME": str(state_home),
            }

            with mock.patch(
                "taf_context.prepare_cli._NATIVE_RELEASE_BASE_URL",
                release.as_uri(),
                create=True,
            ):
                code, _stdout, stderr = invoke(
                    environment,
                    "prepare",
                    "activate",
                    "--repo",
                    str(repo),
                    "--confirm-network",
                    "--confirm-state-write",
                )

            self.assertEqual(code, 2)
            self.assertIn("native engine checksum mismatch", stderr)
            self.assertFalse(any(state_home.rglob("taf-level1")))

    def test_plugin_skill_entrypoint_runs_inspect_in_a_real_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(root / "home"),
                    "PATH": str(Path(shutil.which("git") or "/usr/bin/git").parent),
                    "TAF_STATE_HOME": str(root / "state"),
                }
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(
                        ROOT
                        / "skills"
                        / "prepare-repo-context"
                        / "scripts"
                        / "prepare_repo_context.py"
                    ),
                    "inspect",
                    "--repo",
                    str(repo),
                ],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual((completed.returncode, completed.stderr), (0, ""))
            self.assertEqual(decoded(completed.stdout)["next_safe_action"], "install-native-engine")

    def test_inspect_without_native_engine_is_read_only_and_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            state_home = root / "state"
            environment = {
                "HOME": str(root / "home"),
                "PATH": "",
                "TAF_STATE_HOME": str(state_home),
            }

            code, stdout, stderr = invoke(
                environment,
                "prepare",
                "inspect",
                "--repo",
                str(repo),
            )

            self.assertEqual((code, stderr), (0, ""))
            result = decoded(stdout)
            self.assertEqual(result["mode"], "inspect")
            self.assertEqual(result["repository"]["tracked_file_count"], 1)
            self.assertFalse(result["repository"]["dirty"])
            self.assertEqual(result["engine"]["availability"], "unavailable")
            self.assertEqual(result["next_safe_action"], "install-native-engine")
            self.assertEqual(result["required_authorizations"], ["network", "state-write"])
            self.assertFalse(state_home.exists())
            self.assertLessEqual(len(stdout), 4000)

    def test_inspect_uses_available_native_engine_without_persistent_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            state_home = root / "state"
            binary = root / "taf-level1"
            write_fake_native_engine(binary)
            environment = {
                "HOME": str(root / "home"),
                "PATH": "",
                "TAF_LEVEL1_BINARY": str(binary),
                "TAF_STATE_HOME": str(state_home),
            }

            code, stdout, stderr = invoke(
                environment,
                "prepare",
                "inspect",
                "--repo",
                str(repo),
            )

            self.assertEqual((code, stderr), (0, ""))
            result = decoded(stdout)
            self.assertEqual(result["engine"]["availability"], "available")
            self.assertEqual(result["estimate"]["eligible_path_count"], 1)
            self.assertEqual(result["context"]["freshness"], "partial")
            self.assertEqual(result["next_safe_action"], "build-index")
            self.assertEqual(result["required_authorizations"], ["state-write"])
            self.assertFalse(state_home.exists())

    def test_build_requires_explicit_write_confirmation_then_returns_ready_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            state_home = root / "state"
            binary = root / "taf-level1"
            write_fake_native_engine(binary)
            environment = {
                "HOME": str(root / "home"),
                "PATH": "",
                "TAF_LEVEL1_BINARY": str(binary),
                "TAF_STATE_HOME": str(state_home),
            }

            denied = invoke(
                environment,
                "prepare",
                "build",
                "--repo",
                str(repo),
            )
            self.assertEqual(denied[0], 2)
            self.assertIn("explicit state-write confirmation required", denied[2])
            self.assertFalse(state_home.exists())

            code, stdout, stderr = invoke(
                environment,
                "prepare",
                "build",
                "--repo",
                str(repo),
                "--confirm-state-write",
            )

            self.assertEqual((code, stderr), (0, ""))
            result = decoded(stdout)
            self.assertEqual(result["mode"], "build")
            self.assertEqual(result["context"]["status"], "ready")
            self.assertEqual(result["context"]["freshness"], "exact")
            self.assertEqual(result["next_safe_action"], "use-index")
            self.assertEqual(result["required_authorizations"], [])
            self.assertTrue(state_home.is_dir())

    def test_inspect_output_has_no_provider_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            binary = root / "taf-level1"
            write_fake_native_engine(binary)
            environment = {
                "HOME": str(root / "home"),
                "PATH": "",
                "TAF_LEVEL1_BINARY": str(binary),
                "TAF_STATE_HOME": str(root / "state"),
            }

            code, stdout, stderr = invoke(environment, "prepare", "inspect", "--repo", str(repo))

            self.assertEqual((code, stderr), (0, ""))
            result = decoded(stdout)
            self.assertNotIn("providers", result)
            self.assertEqual(result["engine"]["availability"], "available")

    def test_inspect_reports_state_usage_without_creating_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            state_home = root / "state"
            environment = {
                "HOME": str(root / "home"),
                "PATH": "",
                "TAF_STATE_HOME": str(state_home),
            }

            code, stdout, stderr = invoke(environment, "prepare", "inspect", "--repo", str(repo))

            self.assertEqual((code, stderr), (0, ""))
            result = decoded(stdout)
            self.assertEqual(
                result["state"],
                {"root_bytes": 0, "entry_count": 0, "orphan_count": 0, "stale_runtime_count": 0},
            )
            self.assertFalse(state_home.exists())

    def test_successful_use_refreshes_binding_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            state_home = root / "state"
            binary = root / "taf-level1"
            write_fake_native_engine(binary)
            environment = {
                "HOME": str(root / "home"),
                "PATH": "",
                "TAF_LEVEL1_BINARY": str(binary),
                "TAF_STATE_HOME": str(state_home),
            }
            code, _stdout, stderr = invoke(environment, "prepare", "build", "--repo", str(repo), "--confirm-state-write")
            self.assertEqual((code, stderr), (0, ""))
            binding = next(state_home.glob("repositories/*/*/binding.json"))
            old = 1_600_000_000
            os.utime(binding, (old, old))

            code, _stdout, stderr = invoke(environment, "prepare", "inspect", "--repo", str(repo))
            self.assertEqual((code, stderr), (0, ""))
            self.assertGreater(binding.stat().st_mtime, old)

            os.utime(binding, (old, old))
            code, _stdout, stderr = invoke(
                environment, "prepare", "query", "--repo", str(repo), "--operation", "repository-map"
            )
            self.assertEqual((code, stderr), (0, ""))
            self.assertGreater(binding.stat().st_mtime, old)

    def test_stale_context_does_not_refresh_binding_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            state_home = root / "state"
            fresh_binary = root / "taf-level1"
            write_fake_native_engine(fresh_binary)
            stale_binary = root / "taf-level1-stale"
            write_fake_native_engine(stale_binary, stale=True)
            environment = {
                "HOME": str(root / "home"),
                "PATH": "",
                "TAF_LEVEL1_BINARY": str(fresh_binary),
                "TAF_STATE_HOME": str(state_home),
            }
            code, _stdout, stderr = invoke(environment, "prepare", "build", "--repo", str(repo), "--confirm-state-write")
            self.assertEqual((code, stderr), (0, ""))
            binding = next(state_home.glob("repositories/*/*/binding.json"))
            old = 1_600_000_000
            os.utime(binding, (old, old))

            environment["TAF_LEVEL1_BINARY"] = str(stale_binary)
            code, stdout, stderr = invoke(environment, "prepare", "inspect", "--repo", str(repo))
            self.assertEqual((code, stderr), (0, ""))
            self.assertEqual(decoded(stdout)["next_safe_action"], "rebuild-index")
            self.assertEqual(binding.stat().st_mtime, old)

    def test_remove_is_a_dry_run_until_state_write_is_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            state_home = root / "state"
            binary = root / "taf-level1"
            write_fake_native_engine(binary)
            environment = {
                "HOME": str(root / "home"),
                "PATH": "",
                "TAF_LEVEL1_BINARY": str(binary),
                "TAF_STATE_HOME": str(state_home),
            }
            code, _stdout, stderr = invoke(environment, "prepare", "build", "--repo", str(repo), "--confirm-state-write")
            self.assertEqual((code, stderr), (0, ""))
            entry = next(state_home.glob("repositories/*/*/binding.json")).parent

            code, stdout, stderr = invoke(environment, "prepare", "remove", "--repo", str(repo))
            self.assertEqual((code, stderr), (0, ""))
            result = decoded(stdout)
            self.assertEqual(result["mode"], "remove")
            self.assertTrue(result["dry_run"])
            self.assertEqual([c["category"] for c in result["candidates"]], ["worktree-entry"])
            self.assertEqual(result["removed"], [])
            self.assertEqual(result["required_authorizations"], ["state-write"])
            self.assertTrue(entry.exists())

            code, stdout, stderr = invoke(environment, "prepare", "remove", "--repo", str(repo), "--confirm-state-write")
            self.assertEqual((code, stderr), (0, ""))
            result = decoded(stdout)
            self.assertFalse(result["dry_run"])
            self.assertEqual([c["category"] for c in result["removed"]], ["worktree-entry"])
            self.assertEqual(result["required_authorizations"], [])
            self.assertFalse(entry.exists())

            code, stdout, stderr = invoke(environment, "prepare", "inspect", "--repo", str(repo))
            self.assertEqual((code, stderr), (0, ""))
            self.assertEqual(decoded(stdout)["next_safe_action"], "build-index")

    def test_dry_run_with_nothing_to_reclaim_needs_no_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = {"HOME": directory, "PATH": "", "TAF_STATE_HOME": str(Path(directory) / "state")}
            code, stdout, stderr = invoke(environment, "prepare", "gc")
            self.assertEqual((code, stderr), (0, ""))
            result = decoded(stdout)
            self.assertEqual(result["candidates"], [])
            self.assertEqual(result["required_authorizations"], [])
            self.assertEqual(result["next_safe_action"], "none")

    def test_gc_dry_run_then_confirmed_removal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            state_home = root / "state"
            environment = {"HOME": str(root / "home"), "PATH": "", "TAF_STATE_HOME": str(state_home)}
            orphan = state_home / "repositories" / ("b" * 64) / ("1" * 64) / "native" / "generations" / "g"
            orphan.mkdir(parents=True)
            (orphan / "index.bin").write_bytes(b"x" * 10)
            (state_home / "runtime" / "0.0.1" / "darwin-arm64").mkdir(parents=True)
            (state_home / "runtime" / "0.0.1" / "darwin-arm64" / "taf-level1").write_bytes(b"old")

            code, stdout, stderr = invoke(environment, "prepare", "gc")
            self.assertEqual((code, stderr), (0, ""))
            result = decoded(stdout)
            self.assertEqual(result["mode"], "gc")
            self.assertTrue(result["dry_run"])
            self.assertEqual(
                sorted(c["category"] for c in result["candidates"]),
                ["empty-parent", "orphan-entry", "stale-runtime"],
            )
            self.assertTrue(orphan.exists())

            code, stdout, stderr = invoke(environment, "prepare", "gc", "--unused-for", "30", "--confirm-state-write")
            self.assertEqual((code, stderr), (0, ""))
            result = decoded(stdout)
            self.assertFalse(result["dry_run"])
            self.assertEqual(len(result["removed"]), 3)
            self.assertFalse(orphan.exists())
            self.assertFalse((state_home / "runtime" / "0.0.1").exists())

            code, stdout, stderr = invoke(environment, "prepare", "inspect", "--repo", str(repo))
            self.assertEqual((code, stderr), (0, ""))
            self.assertEqual(decoded(stdout)["state"]["orphan_count"], 0)

    def test_gc_rejects_negative_unused_for(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = {"HOME": directory, "PATH": "", "TAF_STATE_HOME": str(Path(directory) / "state")}
            code, _stdout, stderr = invoke(environment, "prepare", "gc", "--unused-for", "-1")
            self.assertEqual(code, 2)
            self.assertIn("invalid-unused-for", stderr)

    def _prepared(self, root: Path, **fake_kwargs: object) -> tuple[Path, dict[str, str], Path, Path]:
        """Build a ready context with a logging fake engine; return (repo, environment, invocation_log, binding_path)."""
        repo = init_committed_repo(root / "repo")
        state_home = root / "state"
        binary = root / "taf-level1"
        invocation_log = root / "native-invocations.log"
        write_fake_native_engine(binary, invocation_log, **fake_kwargs)
        environment = {
            "HOME": str(root / "home"),
            "PATH": "",
            "TAF_LEVEL1_BINARY": str(binary),
            "TAF_STATE_HOME": str(state_home),
        }
        code, _stdout, stderr = invoke(environment, "prepare", "build", "--repo", str(repo), "--confirm-state-write")
        self.assertEqual((code, stderr), (0, ""))
        binding_path = next(state_home.glob("repositories/*/*/binding.json"))
        return repo, environment, invocation_log, binding_path

    def test_unchanged_repository_queries_with_one_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, environment, invocation_log, _binding_path = self._prepared(root)

            code, stdout, stderr = invoke(
                environment, "prepare", "query", "--repo", str(repo),
                "--operation", "search-symbols", "--query", "Widget",
            )

            self.assertEqual((code, stderr), (0, ""))
            result = decoded(stdout)
            self.assertEqual(result["status"], "ready")
            self.assertEqual(
                result["refresh"], {"performed": False, "changed_path_count": 0, "duration_ms": 0}
            )
            self.assertEqual(
                invocation_log.read_text(encoding="utf-8").splitlines(), ["build", "search-symbols"]
            )

    def test_edit_triggers_update_then_query_and_rewrites_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, environment, invocation_log, binding_path = self._prepared(root)
            old_value = json.loads(binding_path.read_text(encoding="utf-8"))
            write(repo / "tracked.txt", "edited\n")

            code, stdout, stderr = invoke(
                environment, "prepare", "query", "--repo", str(repo),
                "--operation", "search-symbols", "--query", "Widget",
            )

            self.assertEqual((code, stderr), (0, ""))
            result = decoded(stdout)
            self.assertEqual(result["status"], "ready")
            refresh = result["refresh"]
            self.assertEqual(set(refresh), {"performed", "changed_path_count", "duration_ms"})
            self.assertTrue(refresh["performed"])
            self.assertEqual(refresh["changed_path_count"], 1)
            self.assertIsInstance(refresh["duration_ms"], int)
            self.assertGreaterEqual(refresh["duration_ms"], 0)
            self.assertEqual(
                invocation_log.read_text(encoding="utf-8").splitlines(), ["build", "update", "search-symbols"]
            )
            new_value = json.loads(binding_path.read_text(encoding="utf-8"))
            self.assertNotEqual(new_value["index_identity"], old_value["index_identity"])
            self.assertEqual(new_value["head_sha"], old_value["head_sha"])
            self.assertEqual(new_value["dirty_paths"], ["tracked.txt"])
            current_snapshot = collect_snapshot(repo)
            self.assertEqual(new_value["dirty_fingerprint"], current_snapshot.dirty_fingerprint)
            state_root = binding_path.parent / "native"
            self.assertFalse((state_root / CHANGE_DOCUMENT_NAME).exists())

    def test_commit_triggers_update_with_committed_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, environment, invocation_log, binding_path = self._prepared(root)
            write(repo / "tracked.txt", "edited\n")
            new_head = commit_all(repo, "change")

            code, stdout, stderr = invoke(
                environment, "prepare", "query", "--repo", str(repo), "--operation", "repository-map",
            )

            self.assertEqual((code, stderr), (0, ""))
            self.assertEqual(
                invocation_log.read_text(encoding="utf-8").splitlines(),
                ["build", "update", "repository-map"],
            )
            value = json.loads(binding_path.read_text(encoding="utf-8"))
            self.assertEqual(value["head_sha"], new_head)

            code, stdout, stderr = invoke(
                environment, "prepare", "query", "--repo", str(repo), "--operation", "repository-map",
            )
            self.assertEqual((code, stderr), (0, ""))
            self.assertEqual(
                invocation_log.read_text(encoding="utf-8").splitlines(),
                ["build", "update", "repository-map", "repository-map"],
            )

    def test_stale_update_is_refused_without_touching_binding_or_leaving_the_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, environment, _invocation_log, binding_path = self._prepared(root)
            write(repo / "tracked.txt", "edited\n")
            old = 1_600_000_000
            os.utime(binding_path, (old, old))
            before_bytes = binding_path.read_bytes()
            stale_binary = root / "taf-level1-stale"
            invocation_log = root / "native-invocations-stale.log"
            write_fake_native_engine(stale_binary, invocation_log, update_outcome="stale")
            environment["TAF_LEVEL1_BINARY"] = str(stale_binary)

            code, stdout, stderr = invoke(
                environment, "prepare", "query", "--repo", str(repo),
                "--operation", "search-symbols", "--query", "Widget",
            )

            self.assertEqual((code, stdout), (2, ""))
            self.assertIn("ready context is required; run prepare inspect", stderr)
            self.assertEqual(binding_path.read_bytes(), before_bytes)
            self.assertEqual(binding_path.stat().st_mtime, old)
            state_root = binding_path.parent / "native"
            self.assertFalse((state_root / CHANGE_DOCUMENT_NAME).exists())
            self.assertEqual(invocation_log.read_text(encoding="utf-8").splitlines(), ["update"])

    def test_update_error_retries_once_then_names_the_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, environment, _invocation_log, binding_path = self._prepared(root)
            write(repo / "tracked.txt", "edited\n")
            before_bytes = binding_path.read_bytes()
            error_binary = root / "taf-level1-error"
            invocation_log = root / "native-invocations-error.log"
            write_fake_native_engine(error_binary, invocation_log, update_outcome="error")
            environment["TAF_LEVEL1_BINARY"] = str(error_binary)

            code, stdout, stderr = invoke(
                environment, "prepare", "query", "--repo", str(repo),
                "--operation", "search-symbols", "--query", "Widget",
            )

            self.assertEqual((code, stdout), (2, ""))
            self.assertIn(
                "incremental refresh failed; run prepare build --confirm-state-write", stderr
            )
            self.assertEqual(invocation_log.read_text(encoding="utf-8").splitlines(), ["update", "update"])
            self.assertEqual(binding_path.read_bytes(), before_bytes)
            state_root = binding_path.parent / "native"
            self.assertFalse((state_root / CHANGE_DOCUMENT_NAME).exists())

    def test_inspect_refreshes_and_reports_use_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, environment, invocation_log, _binding_path = self._prepared(root)
            write(repo / "tracked.txt", "edited\n")

            code, stdout, stderr = invoke(environment, "prepare", "inspect", "--repo", str(repo))

            self.assertEqual((code, stderr), (0, ""))
            result = decoded(stdout)
            self.assertEqual(result["next_safe_action"], "use-index")
            self.assertEqual(result["context"]["status"], "ready")
            self.assertTrue(result["refresh"]["performed"])
            self.assertEqual(
                invocation_log.read_text(encoding="utf-8").splitlines(), ["build", "update", "status"]
            )

    def test_inspect_falls_back_to_rebuild_when_the_refresh_is_refused(self) -> None:
        # Global constraint: inspect never fails because of a refresh refusal.
        # When the update itself reports the index structurally stale, inspect
        # swallows that PrepareCLIError and continues with today's status /
        # estimate reporting instead of raising.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, environment, _invocation_log, binding_path = self._prepared(root)
            write(repo / "tracked.txt", "edited\n")
            before_bytes = binding_path.read_bytes()
            stale_binary = root / "taf-level1-stale"
            invocation_log = root / "native-invocations-stale.log"
            write_fake_native_engine(stale_binary, invocation_log, update_outcome="stale", stale=True)
            environment["TAF_LEVEL1_BINARY"] = str(stale_binary)

            code, stdout, stderr = invoke(environment, "prepare", "inspect", "--repo", str(repo))

            self.assertEqual((code, stderr), (0, ""))
            result = decoded(stdout)
            self.assertEqual(result["next_safe_action"], "rebuild-index")
            self.assertEqual(
                result["refresh"], {"performed": False, "changed_path_count": 0, "duration_ms": 0}
            )
            self.assertEqual(binding_path.read_bytes(), before_bytes)
            self.assertEqual(
                invocation_log.read_text(encoding="utf-8").splitlines(), ["update", "status", "estimate"]
            )

    def test_schema_1_binding_with_stale_index_keeps_todays_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, environment, _invocation_log, binding_path = self._prepared(root)
            value = json.loads(binding_path.read_text(encoding="utf-8"))
            legacy = {
                key: value[key] for key in ("repository_identity", "worktree_identity", "index_identity")
            }
            legacy["schema_version"] = "1"
            binding_path.write_text(json.dumps(legacy), encoding="utf-8")
            write(repo / "tracked.txt", "edited\n")
            stale_binary = root / "taf-level1-stale"
            invocation_log = root / "native-invocations-stale.log"
            write_fake_native_engine(stale_binary, invocation_log, stale=True)
            environment["TAF_LEVEL1_BINARY"] = str(stale_binary)

            code, stdout, stderr = invoke(
                environment, "prepare", "query", "--repo", str(repo),
                "--operation", "search-symbols", "--query", "Widget",
            )

            self.assertEqual((code, stdout), (2, ""))
            self.assertIn("ready context is required", stderr)
            self.assertEqual(invocation_log.read_text(encoding="utf-8").splitlines(), ["search-symbols"])

    def test_refresh_prunes_aged_unreferenced_generations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, environment, _invocation_log, binding_path = self._prepared(root)
            state_root = binding_path.parent / "native"
            generations = state_root / "generations"
            aged = generations / ("0" * 64)
            recent = generations / ("1" * 64)
            current_generation = generations / ("c" * 64)
            for path in (aged, recent, current_generation):
                path.mkdir(parents=True)
            (state_root / "CURRENT").write_text(("c" * 64) + "\n", encoding="utf-8")
            now = time.time()
            os.utime(aged, (now - 120, now - 120))
            os.utime(recent, (now - 5, now - 5))
            write(repo / "tracked.txt", "edited\n")

            code, stdout, stderr = invoke(
                environment, "prepare", "query", "--repo", str(repo),
                "--operation", "search-symbols", "--query", "Widget",
            )

            self.assertEqual((code, stderr), (0, ""))
            self.assertFalse(aged.exists())
            self.assertTrue(recent.exists())
            self.assertTrue(current_generation.exists())

    def test_refresh_never_deletes_the_generation_current_named_before_the_update(self) -> None:
        # The store may reuse an existing generation directory when identical
        # content recurs, so a generation's mtime can be old even though it
        # was CURRENT an instant ago. Simulate the real engine rotating
        # CURRENT to a fresh token as a side effect of `update`, and confirm
        # the generation that was CURRENT right before the call survives
        # pruning even though its mtime alone looks aged and unreferenced.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, environment, _invocation_log, binding_path = self._prepared(root)
            state_root = binding_path.parent / "native"
            generations = state_root / "generations"
            generations.mkdir(parents=True, exist_ok=True)
            pre_update_current = "d" * 64
            protected = generations / pre_update_current
            protected.mkdir()
            (state_root / "CURRENT").write_text(pre_update_current + "\n", encoding="utf-8")
            now = time.time()
            os.utime(protected, (now - 120, now - 120))
            write(repo / "tracked.txt", "edited\n")

            from taf_context import prepare_cli

            real_invoke = prepare_cli._invoke_native

            def rotate_current_after_update(transport, operation, *args, **kwargs):
                result = real_invoke(transport, operation, *args, **kwargs)
                if operation == "update":
                    (state_root / "CURRENT").write_text(("e" * 64) + "\n", encoding="utf-8")
                return result

            with mock.patch(
                "taf_context.context_operations._invoke_native", side_effect=rotate_current_after_update
            ):
                code, stdout, stderr = invoke(
                    environment, "prepare", "query", "--repo", str(repo),
                    "--operation", "search-symbols", "--query", "Widget",
                )

            self.assertEqual((code, stderr), (0, ""))
            self.assertTrue(protected.exists())


class QueryArgumentTests(unittest.TestCase):
    def test_query_defaults_to_a_four_thousand_character_budget(self) -> None:
        parser = argparse.ArgumentParser()
        register_prepare_command(parser.add_subparsers(dest="command"))
        args = parser.parse_args(["prepare", "query", "--repo", ".", "--operation", "repository-map"])
        # The flag carries no default of its own: the budget an unbudgeted
        # query answers with belongs to the operation.
        self.assertIsNone(args.maximum_output_characters)
        self.assertEqual(_validate_query_arguments(args)[3], 4000)

    def test_filter_values_are_lower_cased_deduplicated_and_sorted(self) -> None:
        self.assertEqual(
            normalize_filter_values(["Go", "python", "GO"], "--language", FILTER_LANGUAGES),
            ["go", "python"],
        )
        self.assertEqual(
            normalize_filter_values(["Definition", "heading"], "--symbol-kind", FILTER_SYMBOL_KINDS),
            ["definition", "heading"],
        )

    def test_invalid_filter_value_names_the_flag_and_lists_valid_values(self) -> None:
        with self.assertRaises(PrepareCLIError) as caught:
            normalize_filter_values(["cobol"], "--language", FILTER_LANGUAGES)
        message = str(caught.exception)
        self.assertIn("--language", message)
        self.assertIn("'cobol'", message)
        self.assertIn("go, javascript, json, markdown, python, rust, toml, typescript", message)

    def test_query_accepts_mixed_case_filters_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            binary = root / "taf-level1"
            write_fake_native_engine(binary)
            environment = {"TAF_LEVEL1_BINARY": str(binary), "TAF_STATE_HOME": str(root / "state")}
            code, _stdout, stderr = invoke(environment, "prepare", "build", "--repo", str(repo), "--confirm-state-write")
            self.assertEqual((code, stderr), (0, ""))
            code, stdout, stderr = invoke(
                environment, "prepare", "query", "--repo", str(repo), "--operation", "search-symbols",
                "--query", "Widget", "--language", "Go", "--symbol-kind", "Definition",
                "--source-type", "Source",
            )
            self.assertEqual((code, stderr), (0, ""))
            self.assertEqual(decoded(stdout)["operation"], "search-symbols")
            code, _stdout, stderr = invoke(
                environment, "prepare", "query", "--repo", str(repo), "--operation", "search-symbols",
                "--query", "Widget", "--symbol-kind", "widget",
            )
            self.assertNotEqual(code, 0)
            self.assertIn("valid values", stderr)


class BindingSchemaTests(unittest.TestCase):
    def _built(self, root: Path, extra_untracked: tuple[str, ...] = ()) -> tuple[Path, Path, dict[str, str]]:
        repo = init_committed_repo(root / "repo")
        write(repo / "notes.txt", "untracked\n")
        for name in extra_untracked:
            write(repo / name, "also untracked\n")
        binary = root / "taf-level1"
        write_fake_native_engine(binary)
        environment = {"HOME": str(root / "home"), "PATH": "", "TAF_LEVEL1_BINARY": str(binary), "TAF_STATE_HOME": str(root / "state")}
        code, _stdout, stderr = invoke(environment, "prepare", "build", "--repo", str(repo), "--confirm-state-write")
        self.assertEqual((code, stderr), (0, ""))
        binding = next((root / "state").glob("repositories/*/*/binding.json"))
        return repo, binding, environment

    def test_build_writes_schema_2_with_head_and_dirty_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, binding, _environment = self._built(Path(directory))
            value = json.loads(binding.read_text(encoding="utf-8"))
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(value["schema_version"], "2")
            self.assertEqual(value["head_sha"], head)
            self.assertRegex(value["dirty_fingerprint"], r"^sha256:[0-9a-f]{64}$")
            self.assertEqual(value["dirty_paths"], ["notes.txt"])
            self.assertEqual(set(value), {"schema_version", "repository_identity", "worktree_identity", "index_identity", "head_sha", "dirty_fingerprint", "dirty_paths"})

    def test_schema_1_binding_is_accepted_without_delta_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, binding, environment = self._built(Path(directory))
            value = json.loads(binding.read_text(encoding="utf-8"))
            legacy = {key: value[key] for key in ("repository_identity", "worktree_identity", "index_identity")}
            legacy["schema_version"] = "1"
            binding.write_text(json.dumps(legacy), encoding="utf-8")
            code, stdout, stderr = invoke(environment, "prepare", "query", "--repo", str(repo), "--operation", "repository-map")
            self.assertEqual((code, stderr), (0, ""))
            self.assertEqual(decoded(stdout)["status"], "ready")

    def test_too_many_dirty_paths_writes_null(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch("taf_context.refresh.MAXIMUM_BINDING_DIRTY_PATHS", 1):
                _repo, binding, _environment = self._built(Path(directory), extra_untracked=("second.txt",))
            self.assertIsNone(json.loads(binding.read_text(encoding="utf-8"))["dirty_paths"])

    def test_dirty_paths_at_the_cap_are_not_nulled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch("taf_context.refresh.MAXIMUM_BINDING_DIRTY_PATHS", 2):
                _repo, binding, _environment = self._built(Path(directory), extra_untracked=("second.txt",))
            self.assertEqual(json.loads(binding.read_text(encoding="utf-8"))["dirty_paths"], ["notes.txt", "second.txt"])

    def test_malformed_schema_2_fields_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, binding, environment = self._built(Path(directory))
            value = json.loads(binding.read_text(encoding="utf-8"))
            value["head_sha"] = "not-a-commit"
            binding.write_text(json.dumps(value), encoding="utf-8")
            code, stdout, stderr = invoke(environment, "prepare", "query", "--repo", str(repo), "--operation", "repository-map")
            self.assertEqual((code, stdout), (2, ""))
            self.assertIn("context binding is invalid", stderr)


class ChangeQuerySessionTests(unittest.TestCase):
    """The CLI answers a change query over one reused engine session."""

    def _recording_session(self) -> tuple[type, list[Path], list[Path]]:
        started: list[Path] = []
        closed: list[Path] = []

        class Recording(engine_session.Level1Session):
            def __init__(self, binary: Path, **keywords: object) -> None:
                super().__init__(binary, **keywords)
                started.append(binary)

            def close(self) -> None:
                closed.append(self._binary)
                super().close()

        return Recording, started, closed

    def test_impact_candidates_runs_over_one_session_and_answers_the_same(self) -> None:
        # Composition costs one engine call per changed symbol, so the CLI
        # reuses one `--serve` child instead of spawning a process per call.
        # The answer must not change with the transport.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, base = change_fixture(root)
            native = root / "taf-level1"
            invocation_log = root / "native-invocations.log"
            write_fake_native_engine(native, invocation_log)
            environment = {
                "TAF_LEVEL1_BINARY": str(native),
                "TAF_STATE_HOME": str(root / "state"),
            }
            invoke(environment, "prepare", "build", "--repo", str(repo), "--confirm-state-write")
            arguments = QueryArguments(
                operation="impact-candidates",
                query=None,
                result_identities=(),
                direction=None,
                base=base,
                path_prefixes=[],
                languages=[],
                symbol_kinds=[],
                source_types=[],
                maximum_results=8,
                maximum_output_characters=4000,
                allow_inferred=False,
            )
            one_shot = run_query(
                repo, arguments, environment=environment, transport_for=OneShotTransport
            )
            invocation_log.write_text("", encoding="utf-8")
            Recording, started, closed = self._recording_session()

            with mock.patch.object(engine_session, "Level1Session", Recording):
                code, stdout, stderr = invoke(
                    environment,
                    "prepare", "query", "--repo", str(repo),
                    "--operation", "impact-candidates", "--base", base,
                )

            self.assertEqual((code, stderr), (0, ""))
            self.assertEqual(decoded(stdout), one_shot)
            self.assertEqual(len(started), 1)
            self.assertEqual(closed, started)
            # One served child answered the changed-symbols call and every
            # relationship call after it.
            self.assertEqual(
                invocation_log.read_text(encoding="utf-8").splitlines(),
                ["serve", "changed-symbols", "related-symbols", "related-symbols", "related-symbols"],
            )

    def test_the_session_is_closed_when_the_query_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, base = change_fixture(root)
            native = root / "taf-level1"
            write_fake_native_engine(native)
            stale_binary = root / "taf-level1-stale"
            write_fake_native_engine(stale_binary, stale=True)
            environment = {
                "TAF_LEVEL1_BINARY": str(native),
                "TAF_STATE_HOME": str(root / "state"),
            }
            invoke(environment, "prepare", "build", "--repo", str(repo), "--confirm-state-write")
            environment["TAF_LEVEL1_BINARY"] = str(stale_binary)
            Recording, started, closed = self._recording_session()

            with mock.patch.object(engine_session, "Level1Session", Recording):
                code, stdout, stderr = invoke(
                    environment,
                    "prepare", "query", "--repo", str(repo),
                    "--operation", "changed-symbols", "--base", base,
                )

            self.assertEqual((code, stdout), (2, ""))
            self.assertIn("ready context is required; run prepare inspect", stderr)
            self.assertEqual(len(started), 1)
            self.assertEqual(len(closed), 1)

    def test_the_other_query_operations_keep_spawning_one_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            native = root / "taf-level1"
            write_fake_native_engine(native)
            environment = {
                "TAF_LEVEL1_BINARY": str(native),
                "TAF_STATE_HOME": str(root / "state"),
            }
            invoke(environment, "prepare", "build", "--repo", str(repo), "--confirm-state-write")
            Recording, started, _closed = self._recording_session()

            with mock.patch.object(engine_session, "Level1Session", Recording):
                code, _stdout, stderr = invoke(
                    environment,
                    "prepare", "query", "--repo", str(repo),
                    "--operation", "search-symbols", "--query", "line",
                )

            self.assertEqual((code, stderr), (0, ""))
            self.assertEqual(started, [])


class QueryArgumentInvariantTests(unittest.TestCase):
    """Invariant 6: the CLI and the MCP server ask the engine the same question."""

    def _cli_arguments(self, parser: argparse.ArgumentParser, *argv: str) -> QueryArguments:
        args = parser.parse_args(list(argv))
        query_text, result_identities, base, output_characters = _validate_query_arguments(args)
        return QueryArguments(
            operation=args.operation,
            query=query_text,
            result_identities=result_identities,
            direction=args.direction,
            base=base,
            path_prefixes=sorted(set(args.path_prefix)),
            languages=normalize_filter_values(args.language, "--language", FILTER_LANGUAGES),
            symbol_kinds=normalize_filter_values(
                args.symbol_kind, "--symbol-kind", FILTER_SYMBOL_KINDS
            ),
            source_types=sorted(set(args.source_type)),
            maximum_results=args.maximum_results,
            maximum_output_characters=output_characters,
            allow_inferred=args.allow_inferred,
        )

    def test_both_surfaces_answer_an_overview_query_identically(self) -> None:
        parser = argparse.ArgumentParser()
        register_prepare_command(parser.add_subparsers(dest="command", required=True))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            native = root / "taf-level1"
            write_fake_native_engine(native)
            environment = {
                "TAF_LEVEL1_BINARY": str(native),
                "TAF_STATE_HOME": str(root / "state"),
            }
            invoke(environment, "prepare", "build", "--repo", str(repo), "--confirm-state-write")

            cli = self._cli_arguments(
                parser,
                "prepare", "query", "--repo", str(repo),
                "--operation", "repository-overview",
                "--path-prefix", "tools", "--language", "Python",
                "--maximum-results", "16", "--maximum-output-characters", "8000",
            )
            mcp = _query_arguments(
                "repository-overview",
                {
                    "repo": str(repo),
                    "path_prefixes": ["tools"],
                    "languages": ["python"],
                    "maximum_results": 16,
                    "maximum_output_characters": 8000,
                },
            )

            self.assertEqual(cli, mcp)
            self.assertEqual(
                run_query(repo, cli, environment=environment, transport_for=OneShotTransport),
                run_query(repo, mcp, environment=environment, transport_for=OneShotTransport),
            )

    def test_both_surfaces_default_the_output_budget_by_operation(self) -> None:
        """The overview's table alone fills 4000 characters, so both default to 8000."""
        parser = argparse.ArgumentParser()
        register_prepare_command(parser.add_subparsers(dest="command", required=True))
        for operation, expected in (("repository-overview", 8000), ("repository-map", 4000)):
            with self.subTest(operation=operation):
                cli = self._cli_arguments(
                    parser,
                    "prepare", "query", "--repo", "/repo", "--operation", operation,
                )
                mcp = _query_arguments(operation, {"repo": "/repo"})
                self.assertEqual(cli.maximum_output_characters, expected)
                self.assertEqual(mcp.maximum_output_characters, expected)
                self.assertEqual(cli, mcp)

    def test_both_surfaces_answer_a_change_query_identically(self) -> None:
        parser = argparse.ArgumentParser()
        register_prepare_command(parser.add_subparsers(dest="command", required=True))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, base = change_fixture(root)
            native = root / "taf-level1"
            write_fake_native_engine(native)
            environment = {
                "TAF_LEVEL1_BINARY": str(native),
                "TAF_STATE_HOME": str(root / "state"),
            }
            invoke(environment, "prepare", "build", "--repo", str(repo), "--confirm-state-write")

            for operation in ("changed-symbols", "impact-candidates"):
                with self.subTest(operation=operation):
                    cli = self._cli_arguments(
                        parser,
                        "prepare", "query", "--repo", str(repo),
                        "--operation", operation, "--base", f"  {base} ",
                    )
                    mcp = _query_arguments(operation, {"repo": str(repo), "base": f" {base}  "})

                    self.assertEqual(cli, mcp)
                    self.assertEqual(
                        run_query(
                            repo, cli, environment=environment, transport_for=OneShotTransport
                        ),
                        run_query(
                            repo, mcp, environment=environment, transport_for=OneShotTransport
                        ),
                    )


class QueryPathImportTests(unittest.TestCase):
    def test_importing_the_cli_does_not_load_installer_modules(self) -> None:
        # Network, temp-file, and platform modules are only needed by activate
        # and by build's binding write; loading them on every query costs about
        # 20 ms of a 55 ms import. Measured in a fresh interpreter so the test
        # process's own imports cannot mask a regression.
        probe = (
            "import sys, taf_context.cli; "
            "print(sorted(name for name in ('platform', 'tempfile', 'urllib.error', 'urllib.request') if name in sys.modules))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=str(ROOT / "tools" / "taf-context"),
            env={"PATH": os.environ.get("PATH", ""), "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"},
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(completed.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
