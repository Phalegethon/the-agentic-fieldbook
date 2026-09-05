"""The plugin registers the repo-context MCP server and its entry script starts cleanly."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).parents[2]
ENTRY = ROOT / "tools" / "taf-context" / "taf_context_mcp.py"


class McpManifestTests(unittest.TestCase):
    def test_mcp_json_points_at_the_entry_script_through_the_plugin_root_variable(self) -> None:
        manifest = json.loads((ROOT / ".claude-plugin" / "mcp.json").read_text(encoding="utf-8"))
        server = manifest["mcpServers"]["repo-context"]
        self.assertEqual(server["command"], "python3")
        self.assertEqual(len(server["args"]), 1)
        self.assertTrue(server["args"][0].startswith("${CLAUDE_PLUGIN_ROOT}/"))
        relative = server["args"][0].removeprefix("${CLAUDE_PLUGIN_ROOT}/")
        self.assertEqual((ROOT / relative).resolve(), ENTRY.resolve())
        self.assertTrue(ENTRY.is_file())

    def test_codex_mcp_json_points_at_the_same_entry_script_through_plugin_root(self) -> None:
        manifest = json.loads((ROOT / ".codex-plugin" / "mcp.json").read_text(encoding="utf-8"))
        server = manifest["mcpServers"]["repo-context"]
        self.assertEqual(server["command"], "python3")
        self.assertEqual(len(server["args"]), 1)
        self.assertTrue(server["args"][0].startswith("${PLUGIN_ROOT}/"))
        relative = server["args"][0].removeprefix("${PLUGIN_ROOT}/")
        self.assertEqual((ROOT / relative).resolve(), ENTRY.resolve())

    def test_claude_plugin_manifest_references_the_claude_mcp_file(self) -> None:
        value = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(value["mcpServers"], "./.claude-plugin/mcp.json")

    def test_codex_plugin_manifest_references_its_own_mcp_file(self) -> None:
        value = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(value["mcpServers"], "./.codex-plugin/mcp.json")

    def test_entry_script_answers_initialize_quickly_and_exits_on_eof(self) -> None:
        request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}}
        with tempfile.TemporaryDirectory() as scratch:
            environment = dict(os.environ)
            # `main` now performs a best-effort launcher-target refresh at
            # startup; a nonexistent state root keeps it a no-op so this smoke
            # test never touches real (or even shared test-guard) TAF state.
            environment["TAF_STATE_HOME"] = str(Path(scratch) / "state")
            started = time.perf_counter()
            completed = subprocess.run(
                [sys.executable, str(ENTRY)],
                input=json.dumps(request).encode("utf-8") + b"\n",
                env=environment,
                capture_output=True,
                timeout=20,
                check=False,
            )
            elapsed = time.perf_counter() - started
        self.assertEqual(completed.returncode, 0, completed.stderr)
        lines = completed.stdout.split(b"\n")
        self.assertEqual(lines[1:], [b""])
        self.assertEqual(json.loads(lines[0])["result"]["serverInfo"]["name"], "taf-repo-context")
        self.assertLess(elapsed, 5.0)  # the bound in the spec is 0.2 s; measured by the benchmark, not asserted here

    def test_entry_script_rejects_arguments(self) -> None:
        completed = subprocess.run([sys.executable, str(ENTRY), "--serve"], capture_output=True, timeout=20, check=False)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, b"")


if __name__ == "__main__":
    unittest.main()
