"""Tests for the MCP stdio server: framing, handshakes, tool routing, validation."""

from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest

from taf_context.context_operations import (
    PrepareCLIError,
    QueryArguments,
    run_build,
    run_query,
)
from taf_context.mcp_server import (
    SERVER_NAME,
    SERVER_VERSION,
    NativeOperations,
    _query_arguments,
    serve,
    tool_definitions,
)
from taf_context.native_transport import OneShotTransport

from .repo_factory import init_committed_repo
from .test_prepare_cli import write_fake_native_engine

FIXTURE = Path(__file__).parent / "testdata" / "mcp-tools.json"
REPO = "/tmp/example-repository"


class FakeOperations:
    def __init__(self, *, fail_with: Exception | None = None, answer: object = None) -> None:
        self.calls: list[tuple] = []
        self.closed = False
        self.fail_with = fail_with
        self.answer = answer

    def _answer(self, mode: str) -> dict:
        if self.fail_with is not None:
            raise self.fail_with
        if self.answer is not None:
            return self.answer
        return {"schema_version": "1", "mode": mode, "next_safe_action": "use-index"}

    def inspect(self, repository: Path) -> dict:
        self.calls.append(("inspect", repository))
        return self._answer("inspect")

    def build(self, repository: Path) -> dict:
        self.calls.append(("build", repository))
        return self._answer("build")

    def query(self, repository: Path, arguments: QueryArguments) -> dict:
        self.calls.append(("query", repository, arguments))
        return self._answer("query")

    def close(self) -> None:
        self.closed = True


def run_server(
    messages: list[object], operations: FakeOperations | None = None
) -> tuple[list[dict], str, int, FakeOperations]:
    operations = operations or FakeOperations()
    stdin = io.BytesIO(
        b"".join(
            (m if isinstance(m, bytes) else json.dumps(m).encode("utf-8")) + b"\n"
            for m in messages
        )
    )
    stdout, stderr = io.BytesIO(), io.StringIO()
    code = serve(stdin, stdout, stderr, operations)
    lines = stdout.getvalue().split(b"\n")
    assert lines[-1] == b"", "stdout must end with a newline"
    return [json.loads(line) for line in lines[:-1]], stderr.getvalue(), code, operations


def request(identifier: int, method: str, params: dict | None = None) -> dict:
    message = {"jsonrpc": "2.0", "id": identifier, "method": method}
    if params is not None:
        message["params"] = params
    return message


def call(identifier: int, tool: str, arguments: dict) -> dict:
    return request(identifier, "tools/call", {"name": tool, "arguments": arguments})


class HandshakeTests(unittest.TestCase):
    def test_legacy_initialize_echoes_a_supported_version_and_lists_tools(self) -> None:
        responses, stderr, code, operations = run_server(
            [
                request(
                    1,
                    "initialize",
                    {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "0"},
                    },
                ),
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                request(2, "ping"),
                request(3, "tools/list"),
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(
            responses[0]["result"]["serverInfo"],
            {"name": SERVER_NAME, "version": SERVER_VERSION},
        )
        self.assertEqual(responses[0]["result"]["capabilities"], {"tools": {}})
        self.assertLess(len(responses[0]["result"]["instructions"]), 600)
        self.assertEqual(responses[1], {"jsonrpc": "2.0", "id": 2, "result": {}})
        self.assertEqual(
            [tool["name"] for tool in responses[2]["result"]["tools"]],
            [
                "inspect",
                "build",
                "repository_map",
                "search_symbols",
                "search_docs",
                "source_snippets",
                "related_symbols",
                "changed_symbols",
                "impact_candidates",
                "repository_overview",
            ],
        )
        self.assertTrue(operations.closed)

    def test_unknown_legacy_version_falls_back_to_the_latest_legacy(self) -> None:
        responses, _, _, _ = run_server(
            [
                request(
                    1,
                    "initialize",
                    {
                        "protocolVersion": "1999-01-01",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "0"},
                    },
                )
            ]
        )
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-11-25")

    def test_modern_discover_marks_results_complete(self) -> None:
        responses, _, _, _ = run_server(
            [
                request(
                    1,
                    "server/discover",
                    {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}},
                ),
                request(2, "tools/list"),
            ]
        )
        discover = responses[0]["result"]
        self.assertEqual(
            (discover["resultType"], discover["supportedVersions"]),
            ("complete", ["2026-07-28"]),
        )
        self.assertEqual(
            discover["_meta"]["io.modelcontextprotocol/serverInfo"],
            {"name": SERVER_NAME, "version": SERVER_VERSION},
        )
        self.assertEqual(responses[1]["result"]["resultType"], "complete")


class ToolListTests(unittest.TestCase):
    def test_tool_definitions_match_the_checked_in_fixture(self) -> None:
        self.assertEqual(tool_definitions(), json.loads(FIXTURE.read_text(encoding="utf-8")))

    def test_the_ten_tools_are_listed_in_a_stable_order(self) -> None:
        names = [tool["name"] for tool in tool_definitions()]
        self.assertEqual(
            names,
            [
                "inspect",
                "build",
                "repository_map",
                "search_symbols",
                "search_docs",
                "source_snippets",
                "related_symbols",
                "changed_symbols",
                "impact_candidates",
                "repository_overview",
            ],
        )
        self.assertEqual(SERVER_VERSION, "1.3.0")
        tools = {tool["name"]: tool for tool in tool_definitions()}
        related = tools["related_symbols"]["inputSchema"]
        self.assertEqual(related["required"], ["repo", "result_ids", "direction"])
        self.assertEqual(
            related["properties"]["direction"]["enum"],
            ["callers", "callees", "importers", "imports"],
        )
        for name in ("changed_symbols", "impact_candidates"):
            schema = tools[name]["inputSchema"]
            self.assertEqual(schema["required"], ["repo"])
            self.assertEqual(schema["properties"]["base"]["type"], "string")
            self.assertEqual(schema["properties"]["staged"]["type"], "boolean")
            self.assertEqual(schema["properties"]["staged"]["default"], False)
        # Filters narrow the change set; the composed operation takes only the
        # candidate budgets and the evidence switch.
        self.assertIn("path_prefixes", tools["changed_symbols"]["inputSchema"]["properties"])
        self.assertEqual(
            sorted(tools["impact_candidates"]["inputSchema"]["properties"]),
            [
                "allow_inferred",
                "base",
                "maximum_output_characters",
                "maximum_results",
                "repo",
                "staged",
            ],
        )

    def test_the_overview_tool_takes_only_the_path_shaped_filters(self) -> None:
        tools = {tool["name"]: tool for tool in tool_definitions()}
        schema = tools["repository_overview"]["inputSchema"]
        self.assertEqual(schema["required"], ["repo"])
        self.assertEqual(
            sorted(schema["properties"]),
            [
                "allow_inferred",
                "languages",
                "maximum_output_characters",
                "maximum_results",
                "path_prefixes",
                "repo",
            ],
        )
        # The warnings this operation can raise are named where the model
        # reads them.
        for warning in (
            "overview-root-first-prefix",
            "overview-root-not-a-directory",
            "output-budget-exceeded",
        ):
            self.assertIn(warning, tools["repository_overview"]["description"])

    def test_the_impact_tool_says_what_the_budget_does_to_the_changed_layer(self) -> None:
        # The changed layer keeps a share of the budget in a compact form, and
        # what it could not carry is counted rather than silently gone; a
        # model reading the answer must be told both.
        description = {tool["name"]: tool for tool in tool_definitions()}["impact_candidates"][
            "description"
        ]
        for phrase in ("changed_trimmed_count", "changed-list-trimmed"):
            self.assertIn(phrase, description)

    def test_the_two_layered_tools_default_to_a_larger_output_budget(self) -> None:
        # Both of these answer with two layers - a table and a file layer, a
        # change set and its candidates - so 4000 characters buy almost none
        # of the second one.
        tools = {tool["name"]: tool for tool in tool_definitions()}
        larger = {"repository_overview", "impact_candidates"}
        for name in sorted(larger):
            budget = tools[name]["inputSchema"]["properties"]["maximum_output_characters"]
            self.assertEqual(budget["default"], 8000, name)
            self.assertIn("8000", tools[name]["description"], name)
        for name, tool in tools.items():
            if name in {"inspect", "build"}:
                continue
            budget = tool["inputSchema"]["properties"]["maximum_output_characters"]
            expected_default = 8000 if name in larger else 4000
            self.assertEqual(budget["default"], expected_default, name)

    def test_the_overview_tool_defaults_to_a_larger_result_count(self) -> None:
        # The overview's file layer is a repository-wide sample, so it
        # defaults to more results than a search or a relationship tool.
        tools = {tool["name"]: tool for tool in tool_definitions()}
        overview = tools["repository_overview"]["inputSchema"]["properties"]["maximum_results"]
        self.assertEqual(overview["default"], 24)
        self.assertIn("24", tools["repository_overview"]["description"])
        # impact-candidates answers with the candidates the operation was
        # asked for, and the output budget already bounds them, so it too
        # defaults higher than the shared 8.
        impact = tools["impact_candidates"]["inputSchema"]["properties"]["maximum_results"]
        self.assertEqual(impact["default"], 16)
        self.assertIn("16", tools["impact_candidates"]["description"])
        for name, tool in tools.items():
            if name in {"inspect", "build", "repository_overview", "impact_candidates"}:
                continue
            if "maximum_results" not in tool["inputSchema"]["properties"]:
                continue
            count = tool["inputSchema"]["properties"]["maximum_results"]
            self.assertEqual(count["default"], 8, name)

    def test_build_is_the_only_writing_tool_and_requires_user_interaction(self) -> None:
        tools = {tool["name"]: tool for tool in tool_definitions()}
        self.assertEqual(tools["build"]["_meta"], {"anthropic/requiresUserInteraction": True})
        self.assertFalse(tools["build"]["annotations"]["readOnlyHint"])
        self.assertIn("confirm_state_write", tools["build"]["inputSchema"]["required"])
        for name, tool in tools.items():
            if name != "build":
                self.assertTrue(tool["annotations"]["readOnlyHint"], name)
                self.assertNotIn("_meta", tool)
            self.assertIn("repo", tool["inputSchema"]["required"])
            self.assertFalse(tool["inputSchema"]["additionalProperties"])


class ToolCallTests(unittest.TestCase):
    def test_each_tool_routes_to_its_operation_with_normalized_arguments(self) -> None:
        sha = "sha256:" + "b" * 64
        responses, _, _, operations = run_server(
            [
                call(1, "inspect", {"repo": REPO}),
                call(2, "build", {"repo": REPO, "confirm_state_write": True}),
                call(3, "repository_map", {"repo": REPO, "path_prefixes": ["b/", "a/"]}),
                call(
                    4,
                    "search_symbols",
                    {
                        "repo": REPO,
                        "query": " Main ",
                        "languages": ["Go", "go"],
                        "symbol_kinds": ["Definition"],
                        "maximum_results": 3,
                    },
                ),
                call(
                    5,
                    "search_docs",
                    {
                        "repo": REPO,
                        "query": "install",
                        "source_types": ["document"],
                        "maximum_output_characters": 8000,
                        "allow_inferred": True,
                    },
                ),
                call(6, "source_snippets", {"repo": REPO, "result_ids": [sha, sha]}),
            ]
        )
        self.assertEqual(
            [r["result"]["isError"] if "isError" in r["result"] else False for r in responses],
            [False] * 6,
        )
        for response in responses:
            self.assertEqual(
                json.loads(response["result"]["content"][0]["text"]),
                response["result"]["structuredContent"],
            )
        kinds = [entry[0] for entry in operations.calls]
        self.assertEqual(kinds, ["inspect", "build", "query", "query", "query", "query"])
        self.assertEqual(operations.calls[0][1], Path(REPO))
        _, _, repository_map = operations.calls[2]
        self.assertEqual(
            (
                repository_map.operation,
                repository_map.path_prefixes,
                repository_map.query,
                repository_map.maximum_output_characters,
            ),
            ("repository-map", ["a/", "b/"], None, 4000),
        )
        _, _, symbols = operations.calls[3]
        self.assertEqual(
            (
                symbols.operation,
                symbols.query,
                symbols.languages,
                symbols.symbol_kinds,
                symbols.maximum_results,
            ),
            ("search-symbols", "Main", ["go"], ["definition"], 3),
        )
        _, _, docs = operations.calls[4]
        self.assertEqual(
            (
                docs.operation,
                docs.source_types,
                docs.maximum_output_characters,
                docs.allow_inferred,
            ),
            ("search-docs", ["document"], 8000, True),
        )
        _, _, snippets = operations.calls[5]
        self.assertEqual(
            (snippets.operation, snippets.result_identities), ("source-snippets", (sha,))
        )

    def test_the_overview_tool_routes_its_filters_and_budgets(self) -> None:
        responses, _, _, operations = run_server(
            [
                call(
                    1,
                    "repository_overview",
                    {
                        "repo": REPO,
                        "path_prefixes": ["tools/", "skills/"],
                        "languages": ["Go", "go"],
                        "maximum_results": 32,
                        "maximum_output_characters": 12000,
                    },
                ),
                call(2, "repository_overview", {"repo": REPO}),
            ]
        )

        self.assertEqual([entry[0] for entry in operations.calls], ["query", "query"])
        _, _, narrowed = operations.calls[0]
        self.assertEqual(
            (
                narrowed.operation,
                narrowed.path_prefixes,
                narrowed.languages,
                narrowed.symbol_kinds,
                narrowed.source_types,
                narrowed.query,
                narrowed.result_identities,
                narrowed.direction,
                narrowed.base,
                narrowed.maximum_results,
                narrowed.maximum_output_characters,
            ),
            (
                "repository-overview",
                ["skills/", "tools/"],
                ["go"],
                [],
                [],
                None,
                (),
                None,
                None,
                32,
                12000,
            ),
        )
        _, _, plain = operations.calls[1]
        self.assertEqual(
            (plain.operation, plain.maximum_results, plain.maximum_output_characters),
            # repository_overview's group table alone is about 3700 characters of
            # canonical JSON, so it gets a tool-specific budget default of 8000
            # (not the other tools' 4000) to leave room for the ranked file
            # layer, and a result-count default of 24 (not the other tools' 8)
            # because that file layer is a repository-wide sample.
            ("repository-overview", 24, 8000),
        )
        for response in responses:
            self.assertNotIn("error", response)

    def test_the_overview_tool_refuses_a_symbol_shaped_argument(self) -> None:
        responses, _, _, operations = run_server(
            [
                call(1, "repository_overview", {"repo": REPO, "symbol_kinds": ["definition"]}),
                call(2, "repository_overview", {"repo": REPO, "source_types": ["source"]}),
                call(3, "repository_overview", {"repo": REPO, "maximum_results": 65}),
                call(4, "repository_overview", {"repo": REPO, "languages": ["cobol"]}),
            ]
        )

        self.assertEqual([response["error"]["code"] for response in responses], [-32602] * 4)
        self.assertIn("unknown argument symbol_kinds", responses[0]["error"]["message"])
        self.assertIn("unknown argument source_types", responses[1]["error"]["message"])
        self.assertEqual(operations.calls, [])

    def test_the_change_tools_route_their_base_and_their_budgets(self) -> None:
        responses, _, _, operations = run_server(
            [
                call(
                    1,
                    "changed_symbols",
                    {"repo": REPO, "base": " origin/main ", "languages": ["Go"]},
                ),
                call(
                    2,
                    "impact_candidates",
                    {
                        "repo": REPO,
                        "base": "9bce09b",
                        "allow_inferred": True,
                        "maximum_results": 24,
                        "maximum_output_characters": 12000,
                    },
                ),
                call(3, "changed_symbols", {"repo": REPO}),
            ]
        )
        for response in responses:
            self.assertNotIn("error", response)
        _, _, changed = operations.calls[0]
        self.assertEqual(
            (changed.operation, changed.base, changed.languages, changed.query),
            ("changed-symbols", "origin/main", ["go"], None),
        )
        _, _, impact = operations.calls[1]
        self.assertEqual(
            (
                impact.operation,
                impact.base,
                impact.allow_inferred,
                impact.maximum_results,
                impact.maximum_output_characters,
            ),
            ("impact-candidates", "9bce09b", True, 24, 12000),
        )
        _, _, without_base = operations.calls[2]
        self.assertIsNone(without_base.base)

    def test_an_unusable_base_is_an_invalid_argument(self) -> None:
        responses, _, _, operations = run_server(
            [
                call(1, "changed_symbols", {"repo": REPO, "base": "   "}),
                call(2, "impact_candidates", {"repo": REPO, "base": "bad\nref"}),
                call(3, "changed_symbols", {"repo": REPO, "base": 7}),
                call(4, "search_symbols", {"repo": REPO, "query": "x", "base": "HEAD"}),
            ]
        )
        self.assertEqual([response["error"]["code"] for response in responses], [-32602] * 4)
        self.assertEqual(operations.calls, [])

    def test_the_change_tools_route_staged(self) -> None:
        responses, _, _, operations = run_server(
            [
                call(1, "changed_symbols", {"repo": REPO, "staged": True}),
                call(
                    2,
                    "impact_candidates",
                    {"repo": REPO, "staged": True, "allow_inferred": True},
                ),
                call(3, "changed_symbols", {"repo": REPO, "staged": False, "base": "origin/main"}),
            ]
        )
        for response in responses:
            self.assertNotIn("error", response)
        _, _, changed = operations.calls[0]
        self.assertEqual(
            (changed.operation, changed.staged, changed.base),
            ("changed-symbols", True, None),
        )
        _, _, impact = operations.calls[1]
        self.assertEqual(
            (impact.operation, impact.staged, impact.allow_inferred),
            ("impact-candidates", True, True),
        )
        _, _, based = operations.calls[2]
        self.assertEqual((based.staged, based.base), (False, "origin/main"))

    def test_staged_and_base_together_are_an_invalid_argument(self) -> None:
        responses, _, _, operations = run_server(
            [
                call(1, "changed_symbols", {"repo": REPO, "staged": True, "base": "origin/main"}),
                call(2, "impact_candidates", {"repo": REPO, "base": "HEAD", "staged": True}),
                call(3, "changed_symbols", {"repo": REPO, "staged": "yes"}),
            ]
        )
        self.assertEqual([response["error"]["code"] for response in responses], [-32602] * 3)
        self.assertEqual(operations.calls, [])

    def test_related_symbols_routes_its_anchors_and_direction(self) -> None:
        sha = "sha256:" + "c" * 64
        responses, _, _, operations = run_server(
            [
                call(
                    1,
                    "related_symbols",
                    {
                        "repo": REPO,
                        "result_ids": [sha, sha],
                        "direction": "callers",
                        "path_prefixes": ["tools/"],
                        "allow_inferred": True,
                    },
                )
            ]
        )
        self.assertNotIn("error", responses[0])
        _, _, related = operations.calls[0]
        self.assertEqual(
            (
                related.operation,
                related.result_identities,
                related.direction,
                related.query,
                related.path_prefixes,
                related.allow_inferred,
            ),
            ("related-symbols", (sha,), "callers", None, ["tools/"], True),
        )

    def test_related_symbols_arguments_fail_closed(self) -> None:
        sha = "sha256:" + "c" * 64
        cases = [
            (call(1, "related_symbols", {"repo": REPO, "result_ids": [sha]}), "direction"),
            (
                call(
                    2,
                    "related_symbols",
                    {"repo": REPO, "result_ids": [sha], "direction": "sideways"},
                ),
                "direction",
            ),
            (
                call(3, "related_symbols", {"repo": REPO, "result_ids": [], "direction": "callers"}),
                "result_ids",
            ),
            (
                call(
                    4,
                    "related_symbols",
                    {"repo": REPO, "result_ids": ["nope"], "direction": "callers"},
                ),
                "result_ids",
            ),
            (
                call(
                    5,
                    "related_symbols",
                    {
                        "repo": REPO,
                        "result_ids": ["sha256:" + f"{index:064x}" for index in range(17)],
                        "direction": "callers",
                    },
                ),
                "result_ids",
            ),
            (
                call(6, "search_symbols", {"repo": REPO, "query": "x", "direction": "callers"}),
                "direction",
            ),
        ]
        responses, _, _, operations = run_server([message for message, _ in cases])
        self.assertEqual(operations.calls, [])
        for response, (_, needle) in zip(responses, cases):
            self.assertEqual(response["error"]["code"], -32602, response)
            self.assertIn(needle, response["error"]["message"])

    def test_invalid_arguments_are_json_rpc_errors_naming_the_argument(self) -> None:
        cases = [
            (call(1, "inspect", {"repo": "relative/path"}), "repo"),
            (call(2, "build", {"repo": REPO}), "confirm_state_write"),
            (call(3, "build", {"repo": REPO, "confirm_state_write": False}), "confirm_state_write"),
            (call(4, "search_symbols", {"repo": REPO}), "query"),
            (call(5, "search_symbols", {"repo": REPO, "query": "   "}), "query"),
            (call(6, "source_snippets", {"repo": REPO, "result_ids": []}), "result_ids"),
            (call(7, "source_snippets", {"repo": REPO, "result_ids": ["nope"]}), "result_ids"),
            (call(8, "search_docs", {"repo": REPO, "query": "x", "languages": ["cobol"]}), "languages"),
            (call(9, "search_docs", {"repo": REPO, "query": "x", "maximum_results": 65}), "maximum_results"),
            (
                call(
                    10,
                    "search_docs",
                    {"repo": REPO, "query": "x", "maximum_output_characters": 5000},
                ),
                "maximum_output_characters",
            ),
            (call(11, "inspect", {"repo": REPO, "extra": 1}), "extra"),
            (call(12, "unknown_tool", {"repo": REPO}), "unknown_tool"),
            (call(13, "inspect", {}), "repo"),
        ]
        responses, _, _, operations = run_server([message for message, _ in cases])
        self.assertEqual(operations.calls, [])
        for response, (_, needle) in zip(responses, cases):
            self.assertEqual(response["error"]["code"], -32602, response)
            self.assertIn(needle, response["error"]["message"])

    def test_operation_failures_are_tool_errors_with_the_cli_message(self) -> None:
        responses, _, _, _ = run_server(
            [call(1, "inspect", {"repo": REPO})],
            FakeOperations(
                fail_with=PrepareCLIError("ready context is required; run prepare inspect")
            ),
        )
        result = responses[0]["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["content"],
            [{"type": "text", "text": "ready context is required; run prepare inspect"}],
        )
        self.assertNotIn("structuredContent", result)

    def test_unexpected_failures_do_not_leak_details_to_the_model(self) -> None:
        responses, stderr, _, _ = run_server(
            [call(1, "inspect", {"repo": REPO})],
            FakeOperations(fail_with=RuntimeError("/private/path/secret")),
        )
        self.assertTrue(responses[0]["result"]["isError"])
        self.assertEqual(
            responses[0]["result"]["content"][0]["text"],
            "repository context operation failed",
        )
        self.assertIn("RuntimeError", stderr)

    def test_an_unserializable_result_is_a_tool_error_not_an_uncaught_exception(self) -> None:
        responses, stderr, code, operations = run_server(
            [call(1, "inspect", {"repo": REPO}), call(2, "inspect", {"repo": REPO})],
            FakeOperations(answer={"unrepresentable": {1, 2}}),
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(responses), 2)  # the session survives and answers again
        for response in responses:
            self.assertTrue(response["result"]["isError"])
            self.assertEqual(
                response["result"]["content"][0]["text"], "repository context operation failed"
            )
            self.assertNotIn("structuredContent", response["result"])
        self.assertIn("TypeError", stderr)
        self.assertTrue(operations.closed)

    def test_argument_rejections_leave_a_stderr_diagnostic(self) -> None:
        responses, stderr, _, _ = run_server(
            [
                call(1, "build", {"repo": REPO, "confirm_state_write": False}),
                call(2, "unknown_tool", {"repo": REPO}),
            ]
        )
        self.assertEqual([response["error"]["code"] for response in responses], [-32602, -32602])
        self.assertEqual(len(stderr.splitlines()), 2)
        self.assertRegex(stderr.splitlines()[0], r"^build \d+ms invalid-arguments$")
        self.assertRegex(stderr.splitlines()[1], r"^unknown_tool \d+ms invalid-arguments$")


class OverviewInvariantTests(unittest.TestCase):
    """Invariant 6 through the real dispatch: one object, two surfaces."""

    def test_the_dispatch_answers_a_folded_overview_exactly_as_the_cli_does(self) -> None:
        # The CLI and the tool call share `run_query`, but only this path also
        # crosses the JSON-RPC framing, the argument normalization and the
        # result envelope, so the fold is compared where a caller actually
        # meets it.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = init_committed_repo(root / "repo")
            binary = root / "taf-level1"
            write_fake_native_engine(binary, wide_overview=True)
            environment = {
                "HOME": str(root),
                "PATH": "",
                "TAF_LEVEL1_BINARY": str(binary),
                "TAF_STATE_HOME": str(root / "state"),
            }
            run_build(
                repository, environment=environment, transport_for=OneShotTransport
            )
            arguments = {
                "repo": str(repository),
                "maximum_results": 6,
                "maximum_output_characters": 4000,
            }
            operations = NativeOperations(environment, log=lambda _line: None)
            try:
                responses, _, code, _ = run_server(
                    [call(1, "repository_overview", arguments)], operations
                )
            finally:
                operations.close()

            self.assertEqual(code, 0)
            answered = responses[0]["result"]["structuredContent"]
            self.assertNotIn("isError", responses[0]["result"])
            self.assertEqual(
                json.loads(responses[0]["result"]["content"][0]["text"]), answered
            )
            self.assertEqual(
                answered,
                run_query(
                    repository,
                    _query_arguments("repository-overview", arguments),
                    environment=environment,
                    transport_for=OneShotTransport,
                ),
            )
            # A folded answer, so the comparison is about the fold and not
            # about a table that fitted anyway.
            self.assertEqual(answered["groups"][-1]["path_prefix"], "*")
            self.assertGreater(answered["overview"]["other_group_count"], 0)
            self.assertEqual(answered["warnings"], [])
            self.assertLessEqual(answered["output_characters"], 4000)


class NativeOperationsTests(unittest.TestCase):
    def test_close_closes_every_session_even_when_one_raises(self) -> None:
        closed: list[str] = []
        logged: list[str] = []

        class Session:
            def __init__(self, name: str, *, failing: bool = False) -> None:
                self.name = name
                self.failing = failing

            def close(self) -> None:
                closed.append(self.name)
                if self.failing:
                    raise OSError("the child would not go away")

        operations = NativeOperations({}, log=logged.append)
        operations._sessions = {
            Path("/bin/first"): Session("first", failing=True),
            Path("/bin/second"): Session("second"),
        }
        operations.close()
        self.assertEqual(closed, ["first", "second"])
        self.assertEqual(operations._sessions, {})
        self.assertEqual(len(logged), 1)
        self.assertIn("OSError", logged[0])


class FramingTests(unittest.TestCase):
    def test_protocol_errors(self) -> None:
        responses, _, code, _ = run_server(
            [
                b"{not json",
                [request(1, "ping"), request(2, "ping")],
                request(3, "no/such/method"),
                {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}},
                {"jsonrpc": "2.0", "id": 9, "result": {}},
                b"",
                request(4, "ping"),
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual([r.get("id") for r in responses], [None, None, 3, 4])
        self.assertEqual([r["error"]["code"] for r in responses[:3]], [-32700, -32600, -32601])
        self.assertEqual(responses[3], {"jsonrpc": "2.0", "id": 4, "result": {}})

    def test_stdout_carries_only_single_line_json_and_diagnostics_go_to_stderr(self) -> None:
        operations = FakeOperations()
        responses, stderr, _, _ = run_server([call(1, "inspect", {"repo": REPO})], operations)
        self.assertEqual(len(responses), 1)
        self.assertRegex(stderr, r"inspect \d+ms ok")

    def test_every_stdout_byte_is_ascii(self) -> None:
        # A line reader that splits on U+2028 (Python's str.splitlines does)
        # cannot mis-frame a message that carries no non-ASCII byte at all.
        note = "one\u2028two ünï"
        stdin = io.BytesIO(json.dumps(call(1, "inspect", {"repo": REPO})).encode("utf-8") + b"\n")
        stdout, stderr = io.BytesIO(), io.StringIO()
        serve(stdin, stdout, stderr, FakeOperations(answer={"mode": "inspect", "note": note}))
        raw = stdout.getvalue()
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError as exc:  # pragma: no cover - the assertion message
            self.fail(f"stdout carried a non-ASCII byte: {exc}")
        self.assertEqual(len(text.splitlines()), 1)
        message = json.loads(text)
        self.assertEqual(message["result"]["structuredContent"]["note"], note)
        self.assertEqual(json.loads(message["result"]["content"][0]["text"])["note"], note)


if __name__ == "__main__":
    unittest.main()
