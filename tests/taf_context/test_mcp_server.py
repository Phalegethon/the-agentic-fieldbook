"""Tests for the MCP stdio server: framing, handshakes, tool routing, validation."""

from __future__ import annotations

import io
import json
from pathlib import Path
import unittest

from taf_context.context_operations import PrepareCLIError, QueryArguments
from taf_context.mcp_server import (
    SERVER_NAME,
    SERVER_VERSION,
    NativeOperations,
    serve,
    tool_definitions,
)

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
            (repository_map.operation, repository_map.path_prefixes, repository_map.query),
            ("repository-map", ["a/", "b/"], None),
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
