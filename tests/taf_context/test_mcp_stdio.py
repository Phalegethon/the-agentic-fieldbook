"""Bounded MCP stdio transport contract tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import unittest

from taf_context.mcp_stdio import (
    MCPPolicy,
    MCPProcessError,
    MCPToolSchema,
    call_mcp_tool,
)


FIXTURE = Path(__file__).with_name("fixture_mcp_provider.py")
TOOL = {
    "name": "query_graph",
    "description": "Query the bound graph",
    "inputSchema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    },
    "outputSchema": {
        "type": "object",
        "properties": {"nodes": {"type": "array"}},
        "required": ["nodes"],
    },
}


def schema_digest(value: dict[str, object]) -> str:
    material = {
        "name": value["name"],
        "inputSchema": value["inputSchema"],
        "outputSchema": value["outputSchema"],
    }
    wire = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(wire).hexdigest()


def policy(**changes: object) -> MCPPolicy:
    values: dict[str, object] = {
        "protocol_version": "2025-06-18",
        "expected_tool_schema_digest": schema_digest(TOOL),
        "timeout_seconds": 1.0,
        "maximum_stdout_bytes": 4096,
        "maximum_stderr_bytes": 4096,
        "maximum_message_bytes": 4096,
        "maximum_notifications": 8,
        "maximum_content_characters": 4096,
        "environment": (),
    }
    values.update(changes)
    return MCPPolicy(**values)


def call(mode: str, **policy_changes: object):
    return call_mcp_tool(
        (sys.executable, str(FIXTURE), mode),
        "query_graph",
        {"query": "Widget"},
        policy(**policy_changes),
    )


class MCPStdioTests(unittest.TestCase):
    def test_valid_handshake_schema_and_single_call(self) -> None:
        result = call("valid")
        self.assertEqual(result.tool, "query_graph")
        self.assertEqual(result.structured_content, {"nodes": []})
        self.assertEqual(result.text_content, ('{"nodes":[]}',))
        self.assertEqual(result.tool_schema.schema_digest, schema_digest(TOOL))
        self.assertGreater(result.stdout_bytes, 0)
        self.assertEqual(result.notification_count, 0)

    def test_schema_model_is_canonical_and_strict(self) -> None:
        schema = MCPToolSchema.from_dict(TOOL)
        self.assertEqual(schema.schema_digest, schema_digest(TOOL))
        for mutation in (
            {**TOOL, "extra": True},
            {**TOOL, "name": "../escape"},
            {**TOOL, "inputSchema": []},
        ):
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                MCPToolSchema.from_dict(mutation)

    def test_protocol_and_message_attacks_fail_closed(self) -> None:
        cases = {
            "wrong-jsonrpc": "invalid-jsonrpc",
            "wrong-id": "unexpected-response-id",
            "server-request": "server-request-unsupported",
            "notification-flood": "notification-budget-exceeded",
            "invalid-utf8": "invalid-utf8",
            "invalid-json": "invalid-json",
            "partial-frame": "partial-frame",
            "nonfinite": "invalid-json",
            "duplicate-key": "invalid-json",
        }
        for mode, reason in cases.items():
            with self.subTest(mode=mode), self.assertRaisesRegex(
                MCPProcessError, reason
            ):
                call(mode)

    def test_tool_discovery_attacks_fail_closed(self) -> None:
        cases = {
            "duplicate-tool": "duplicate-tool",
            "unknown-tool": "tool-unavailable",
            "schema-drift": "tool-schema-mismatch",
            "pagination": "tool-pagination-unsupported",
        }
        for mode, reason in cases.items():
            with self.subTest(mode=mode), self.assertRaisesRegex(
                MCPProcessError, reason
            ):
                call(mode)

    def test_process_and_output_budgets_fail_closed(self) -> None:
        cases = (
            ("timeout", "timeout", {}),
            ("early-exit", "early-exit", {}),
            ("stdout-overflow", "stdout-budget-exceeded", {}),
            ("stderr-overflow", "stderr-budget-exceeded", {}),
            (
                "text-overflow",
                "content-budget-exceeded",
                {
                    "maximum_stdout_bytes": 16384,
                    "maximum_message_bytes": 16384,
                },
            ),
            ("resource-content", "unsupported-content", {}),
        )
        for mode, reason, changes in cases:
            with self.subTest(mode=mode), self.assertRaisesRegex(
                MCPProcessError, reason
            ):
                call(mode, timeout_seconds=0.15, **changes)

    def test_tool_errors_are_not_returned_as_evidence(self) -> None:
        for mode in ("tool-rpc-error", "tool-is-error"):
            with self.subTest(mode=mode), self.assertRaisesRegex(
                MCPProcessError, "tool-error"
            ):
                call(mode)

    def test_policy_rejects_unbounded_or_unsafe_values(self) -> None:
        for change in (
            {"timeout_seconds": 0.0},
            {"maximum_stdout_bytes": 300000},
            {"maximum_notifications": 9},
            {"expected_tool_schema_digest": "latest"},
            {"environment": (("TOKEN", "secret"),)},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                policy(**change)


if __name__ == "__main__":
    unittest.main()
