"""Opt-in end-to-end check of the repo-context MCP server against a copy of this checkout."""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).parents[2]
ENTRY = ROOT / "tools" / "taf-context" / "taf_context_mcp.py"
RESPONSE_TIMEOUT_SECONDS = 60.0
EXIT_TIMEOUT_SECONDS = 30.0


class McpClient:
    def __init__(self, environment: dict[str, str], stderr_path: Path) -> None:
        self._stderr = stderr_path.open("wb")
        self.process = subprocess.Popen([sys.executable, str(ENTRY)], env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr)
        self._next = 0
        # A dedicated reader keeps the response wait bounded: a server that
        # stays alive but stops answering must fail the test, not hang it.
        self._lines: queue.Queue[bytes] = queue.Queue()
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        for line in iter(self.process.stdout.readline, b""):
            self._lines.put(line)
        self._lines.put(b"")  # end of stream

    def request(self, method: str, params: dict | None = None) -> dict:
        self._next += 1
        message = {"jsonrpc": "2.0", "id": self._next, "method": method}
        if params is not None:
            message["params"] = params
        self.process.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
        self.process.stdin.flush()
        try:
            line = self._lines.get(timeout=RESPONSE_TIMEOUT_SECONDS)
        except queue.Empty:
            raise TimeoutError(f"no answer to {method} within {RESPONSE_TIMEOUT_SECONDS}s")
        assert line, f"the server closed stdout without answering {method}"
        response = json.loads(line)
        assert response["id"] == self._next, response
        return response

    def call(self, tool: str, **arguments: object) -> dict:
        response = self.request("tools/call", {"name": tool, "arguments": arguments})
        assert "result" in response, response
        result = response["result"]
        assert not result.get("isError"), result
        return result["structuredContent"]

    def close(self) -> int:
        self.process.stdin.close()
        try:
            code = self.process.wait(timeout=EXIT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            # Never leave the server (and the engine it started) behind, and
            # never mask the failure the caller is already unwinding from.
            self.process.kill()
            code = self.process.wait()
        self._stderr.close()
        return code


@unittest.skipUnless(
    os.environ.get("TAF_DOGFOOD") == "1" and os.environ.get("TAF_LEVEL1_BINARY"),
    "set TAF_DOGFOOD=1 and TAF_LEVEL1_BINARY to run the MCP dogfood",
)
class DogfoodMcpTests(unittest.TestCase):
    def test_one_session_builds_queries_refreshes_and_exits_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory) / "repo"
            subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(ROOT), str(work)], check=True, capture_output=True)
            environment = {
                "HOME": directory,
                "PATH": os.environ.get("PATH", ""),
                "LANG": "en_US.UTF-8",
                "LC_ALL": "en_US.UTF-8",
                "TAF_LEVEL1_BINARY": os.environ["TAF_LEVEL1_BINARY"],
                "TAF_STATE_HOME": str(Path(directory) / "state"),
            }
            stderr_path = Path(directory) / "server.stderr"
            client = McpClient(environment, stderr_path)
            try:
                initialized = client.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "dogfood", "version": "0"}})
                self.assertEqual(initialized["result"]["serverInfo"]["name"], "taf-repo-context")
                inspected = client.call("inspect", repo=str(work))
                self.assertEqual(inspected["next_safe_action"], "build-index")
                built = client.call("build", repo=str(work), confirm_state_write=True)
                self.assertEqual(built["next_safe_action"], "use-index")
                found = client.call("search_symbols", repo=str(work), query="collect_snapshot", symbol_kinds=["definition"])
                self.assertEqual(found["status"], "ready")
                self.assertTrue(any(f["qualified_name"].endswith("collect_snapshot") for f in found["findings"]))
                snippet = client.call("source_snippets", repo=str(work), result_ids=[found["findings"][0]["result_identity"]])
                # The engine reports "partial" (not "ready") when the snippet
                # had to be shortened to fit the output character budget; both
                # are usable results (see context_operations.run_query, which
                # accepts the same pair). This repository's real source lines
                # for collect_snapshot exceed the default budget, so "partial"
                # is the actual outcome here, not a defect.
                self.assertIn(snippet["status"], {"ready", "partial"})
                if snippet["status"] == "partial":
                    # Both engine paths that shorten a snippet pair "partial"
                    # with "refine-query" (internal/engine/engine.go and
                    # internal/engine/snippets.go); nothing else may produce it.
                    self.assertEqual(snippet["next_safe_action"], "refine-query")
                # The search returns the broker copy of collect_snapshot next to
                # the vendored Level 0 one; only the broker copy is called from
                # this repository, so the anchor is picked by path rather than
                # by rank.
                anchor = next(
                    finding
                    for finding in found["findings"]
                    if finding["path"] == "tools/taf-context/taf_context/git_snapshot.py"
                )
                related = client.call(
                    "related_symbols",
                    repo=str(work),
                    result_ids=[anchor["result_identity"]],
                    direction="callers",
                )
                self.assertIn(related["status"], {"ready", "partial"})
                self.assertTrue(
                    any(
                        finding["relation"] == "call" and finding["edge_evidence"] == "verified"
                        for finding in related["findings"]
                    ),
                    related["findings"],
                )
                target = work / "tools" / "taf-context" / "taf_context" / "state_paths.py"
                target.write_text(target.read_text(encoding="utf-8") + "\n\ndef mcp_dogfood_marker():\n    return 1\n", encoding="utf-8")
                refreshed = client.call("search_symbols", repo=str(work), query="mcp_dogfood_marker")
                self.assertTrue(refreshed["refresh"]["performed"])
                self.assertEqual(refreshed["refresh"]["changed_path_count"], 1)
                self.assertTrue(any(f["qualified_name"].endswith("mcp_dogfood_marker") for f in refreshed["findings"]))
                again = client.call("search_symbols", repo=str(work), query="mcp_dogfood_marker")
                self.assertFalse(again["refresh"]["performed"])
            finally:
                code = client.close()
            self.assertEqual(code, 0)
            stderr = stderr_path.read_text(encoding="utf-8")
            starts = [line for line in stderr.splitlines() if line.startswith("engine-start pid=")]
            self.assertEqual(len(starts), 1, stderr)
            pid = int(starts[0].split("=")[1])
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)


if __name__ == "__main__":
    unittest.main()
