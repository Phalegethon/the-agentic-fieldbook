"""Graphify adapter conformance tests against frozen v0.9.50 shapes."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from taf_context.level1_models import Level1Request, parse_level1_result
from taf_context.provider_binding import AdapterBinding
from taf_context.provider_execution_models import (
    parse_adapter_manifest,
    parse_inspection_record,
)
from taf_context.provider_process import inspect_provider, query_provider

from .test_level1_models import request_wire
from .test_provider_process import policy, snapshot


ROOT = Path(__file__).parents[2]
ADAPTER = ROOT / "tools/taf-context/adapters/graphify/adapter.py"
MANIFEST = ROOT / "tools/taf-context/adapters/graphify/manifest.json"
FIXTURE = Path(__file__).with_name("fixture_graphify.py").resolve()


class GraphifyAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        temporary = Path(self.temporary.name).resolve()
        self.repo = temporary / "repo"
        (self.repo / "src").mkdir(parents=True)
        self.source = self.repo / "src/widget.py"
        self.source.write_text(
            "# one\nclass Widget:\n    pass\ndef helper():\n    return Widget()\n",
            encoding="utf-8",
        )
        self.state = temporary / "graphify-out"
        self.state.mkdir()
        self.graph = self.state / "graph.json"
        self.graph.write_text(
            json.dumps({
                "directed": True,
                "multigraph": False,
                "graph": {},
                "nodes": [],
                "links": [],
            }, separators=(",", ":")),
            encoding="utf-8",
        )
        self.manifest = self.state / "manifest.json"
        self.write_portable_manifest()
        self.log_sentinel = temporary / "query.log"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_portable_manifest(self, **changes: object) -> None:
        data: dict[str, object] = {
            "src/widget.py": {
                "mtime": self.source.stat().st_mtime,
                "seen": self.source.stat().st_mtime + 1.0,
                "ast_hash": hashlib.md5(self.source.read_bytes()).hexdigest(),
                "semantic_hash": "",
            }
        }
        data.update(changes)
        self.manifest.write_text(
            json.dumps(data, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )

    def envelope(
        self,
        mode: str,
        phase: str = "inspect",
        *,
        environment: dict[str, str] | None = None,
    ) -> dict[str, object]:
        value: dict[str, object] = {
            "phase": phase,
            "repository_root": str(self.repo),
            "provider_command": {
                "executable": str(Path(sys.executable).resolve()),
                "executable_digest": "sha256:" + "b" * 64,
                "arguments": [str(FIXTURE), mode, str(self.graph)],
                "state_roots": [str(self.state)],
                "environment": environment or {
                    "GRAPHIFY_QUERY_LOG_DISABLE": "1",
                },
                "binding_digest": "sha256:" + "a" * 64,
                "transport": "mcp-stdio",
            },
        }
        if phase == "inspect":
            value["snapshot"] = snapshot().to_dict()
        return value

    def run_adapter(
        self, envelope: dict[str, object]
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, str(ADAPTER)],
            input=(json.dumps(envelope, separators=(",", ":")) + "\n").encode(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=4,
        )

    def inspection(self, mode: str = "valid"):
        completed = self.run_adapter(self.envelope(mode))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return parse_inspection_record(completed.stdout.rstrip(b"\n")), completed

    def request(self, index_identity: str) -> Level1Request:
        wire = request_wire()
        wire.update({
            "provider_identity": "graphify",
            "index_identity": index_identity,
            "minimum_freshness": "unknown",
            "filters": {
                "path_prefixes": [],
                "languages": [],
                "symbol_kinds": [],
                "source_types": [],
            },
        })
        return Level1Request.from_dict(wire)

    def query(
        self,
        mode: str = "valid",
        *,
        request_mutation=None,
        environment: dict[str, str] | None = None,
    ):
        inspection, _ = self.inspection()
        request = self.request(inspection.index_identity)
        if request_mutation is not None:
            wire = request.to_dict()
            request_mutation(wire)
            request = Level1Request.from_dict(wire)
        envelope = self.envelope(mode, "query", environment=environment)
        envelope["request"] = request.to_dict()
        return self.run_adapter(envelope)

    def test_manifest_and_partial_inspection_are_exact(self) -> None:
        manifest = parse_adapter_manifest(MANIFEST.read_bytes())
        self.assertEqual(manifest.provider_version, "0.9.50")
        self.assertEqual(manifest.capabilities, ("search-symbols",))

        inspection, completed = self.inspection()
        self.assertEqual(inspection.readiness.value, "partial")
        self.assertEqual(inspection.committed_head, "0" * 40)
        self.assertEqual(inspection.capabilities, ("search-symbols",))
        self.assertIn("index-head-unverifiable", inspection.reason_codes)
        self.assertNotIn(str(self.repo), completed.stdout.decode())
        self.assertNotIn(str(self.state), completed.stdout.decode())

    def test_inspection_rejects_missing_nonportable_or_drifted_metadata(self) -> None:
        self.manifest.unlink()
        self.assertNotEqual(self.run_adapter(self.envelope("valid")).returncode, 0)

        self.write_portable_manifest(**{
            "/tmp/escape.py": {
                "mtime": 1.0,
                "seen": 2.0,
                "ast_hash": "a" * 32,
                "semantic_hash": "",
            }
        })
        self.assertNotEqual(self.run_adapter(self.envelope("valid")).returncode, 0)

        self.write_portable_manifest()
        os.utime(self.source, (self.source.stat().st_atime, self.source.stat().st_mtime + 10))
        self.assertNotEqual(self.run_adapter(self.envelope("valid")).returncode, 0)

    def test_query_returns_deterministic_bounded_uncertain_citations(self) -> None:
        completed = self.query("truncated")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = parse_level1_result(completed.stdout.rstrip(b"\n"))
        self.assertEqual(result.freshness.value, "unknown")
        self.assertEqual(
            [(item.path, item.start_line, item.end_line, item.qualified_name)
             for item in result.findings],
            [
                ("src/widget.py", 2, 3, "Widget"),
                ("src/widget.py", 4, 4, "helper"),
            ],
        )
        self.assertTrue(all(
            item.evidence_class.value == "uncertain" for item in result.findings
        ))
        self.assertEqual(result.omitted_count, 5)
        self.assertFalse(self.log_sentinel.exists())
        self.assertNotIn(str(self.graph), completed.stdout.decode())

    def test_inferred_edges_never_promote_node_evidence(self) -> None:
        completed = self.query("inferred-edge")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = parse_level1_result(completed.stdout.rstrip(b"\n"))
        self.assertTrue(all(
            item.evidence_class.value == "uncertain" for item in result.findings
        ))

    def test_query_rejects_schema_logging_binding_and_citation_attacks(self) -> None:
        for mode in (
            "schema-drift",
            "wrong-graph",
            "provider-error",
            "absolute-path",
            "citation-missing",
            "duplicate-citation",
            "excessive-subgraph",
        ):
            with self.subTest(mode=mode):
                completed = self.query(mode)
                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(completed.stdout, b"")

        completed = self.query(
            environment={"LANG": "C"}
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse(self.log_sentinel.exists())

    def test_query_rejects_changed_graph_and_unsupported_filters(self) -> None:
        inspection, _ = self.inspection()
        self.graph.write_text("{}", encoding="utf-8")
        envelope = self.envelope("valid", "query")
        envelope["request"] = self.request(inspection.index_identity).to_dict()
        self.assertNotEqual(self.run_adapter(envelope).returncode, 0)

        self.graph.write_text(
            '{"directed":true,"multigraph":false,"graph":{},"nodes":[],"links":[]}',
            encoding="utf-8",
        )

        def add_symbol_filter(wire: dict[str, object]) -> None:
            wire["filters"]["symbol_kinds"] = ["class"]

        self.assertNotEqual(
            self.query(request_mutation=add_symbol_filter).returncode, 0
        )

    @unittest.skipUnless(
        sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file(),
        "macOS sandbox-exec is required for adapter integration",
    )
    def test_core_sandbox_accepts_exact_graphify_mcp_child(self) -> None:
        provider = self.state / "fixture_graphify.py"
        shutil.copyfile(FIXTURE, provider)
        executable = Path(sys.executable).resolve()
        tool_root = (ROOT / "tools/taf-context").resolve()
        wire: dict[str, object] = {
            "schema_version": "1",
            "adapter_identity": "taf.graphify.v0_9_50",
            "provider_identity": "graphify",
            "adapter_root": str(tool_root),
            "provider_executable": str(executable),
            "provider_executable_digest": "sha256:" + hashlib.sha256(
                executable.read_bytes()
            ).hexdigest(),
            "provider_arguments": [
                str(provider.resolve()), "valid", str(self.graph),
            ],
            "provider_state_roots": [str(self.state)],
            "environment": {"GRAPHIFY_QUERY_LOG_DISABLE": "1"},
            "transport": "mcp-stdio",
        }
        wire["binding_digest"] = "sha256:" + hashlib.sha256(
            json.dumps(wire, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        binding = AdapterBinding.from_dict(wire)
        manifest = parse_adapter_manifest(MANIFEST.read_bytes())
        inspection, _ = inspect_provider(
            manifest, tool_root, snapshot(), self.repo,
            policy(timeout_seconds=3.0), binding=binding,
        )
        result, _ = query_provider(
            manifest, tool_root, self.request(inspection.index_identity),
            self.repo, policy(timeout_seconds=3.0), binding=binding,
        )
        self.assertEqual(result.findings[0].path, "src/widget.py")
        self.assertEqual(result.freshness.value, "unknown")


if __name__ == "__main__":
    unittest.main()
