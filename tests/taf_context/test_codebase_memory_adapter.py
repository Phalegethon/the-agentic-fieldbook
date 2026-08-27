"""Codebase Memory adapter conformance tests against frozen v0.10.8 shapes."""

from __future__ import annotations

import hashlib
import json
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
ADAPTER = ROOT / "tools/taf-context/adapters/codebase-memory/adapter.py"
MANIFEST = ROOT / "tools/taf-context/adapters/codebase-memory/manifest.json"
FIXTURE = Path(__file__).with_name("fixture_codebase_memory.py").resolve()


class CodebaseMemoryAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name, "repo")
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "src/widget.py").write_text(
            "# one\nclass Widget:\n    pass\n# four\n# five\n",
            encoding="utf-8",
        )
        self.repo = self.repo.resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def envelope(self, mode: str, phase: str = "inspect") -> dict[str, object]:
        value: dict[str, object] = {
            "phase": phase,
            "repository_root": str(self.repo),
            "provider_command": {
                "executable": str(Path(sys.executable).resolve()),
                "executable_digest": "sha256:" + "b" * 64,
                "arguments": [str(FIXTURE), mode],
                "state_roots": [str(Path(self.temporary.name).resolve())],
                "environment": {"TAF_FIXTURE_ROOT": str(self.repo)},
                "binding_digest": "sha256:" + "a" * 64,
                "transport": "cli-json",
            },
        }
        if phase == "inspect":
            value["snapshot"] = snapshot().to_dict()
        return value

    def run_adapter(self, envelope: dict[str, object]) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, str(ADAPTER)],
            input=(json.dumps(envelope, separators=(",", ":")) + "\n").encode(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=3,
        )

    def inspection(self, mode: str = "valid"):
        completed = self.run_adapter(self.envelope(mode))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return parse_inspection_record(completed.stdout.rstrip(b"\n")), completed

    def test_manifest_and_partial_inspection_are_exact(self) -> None:
        manifest = parse_adapter_manifest(MANIFEST.read_bytes())
        self.assertEqual(manifest.provider_version, "0.10.8")
        self.assertEqual(manifest.capabilities, ("repository-map", "search-symbols"))

        inspection, completed = self.inspection()
        self.assertEqual(inspection.readiness.value, "partial")
        self.assertEqual(inspection.committed_head, "0" * 40)
        self.assertIn("index-head-unverifiable", inspection.reason_codes)
        self.assertNotIn(str(self.repo), completed.stdout.decode())

    def test_inspection_rejects_ambiguous_unbounded_or_drifted_state(self) -> None:
        for mode in (
            "wrong-version", "missing-project", "ambiguous-project",
            "list-pagination", "list-schema-drift", "wrong-status-root",
            "provider-error",
        ):
            with self.subTest(mode=mode):
                completed = self.run_adapter(self.envelope(mode))
                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(completed.stdout, b"")
                self.assertNotIn(b"must-not-escape", completed.stderr)

    def request(self, index_identity: str) -> Level1Request:
        wire = request_wire()
        wire.update({
            "provider_identity": "codebase-memory-mcp",
            "index_identity": index_identity,
            "minimum_freshness": "unknown",
            "filters": {
                "path_prefixes": [], "languages": [],
                "symbol_kinds": [], "source_types": [],
            },
        })
        return Level1Request.from_dict(wire)

    def query(self, mode: str = "valid"):
        inspection, _ = self.inspection(mode if mode == "windows-path" else "valid")
        envelope = self.envelope(mode, "query")
        envelope["request"] = self.request(inspection.index_identity).to_dict()
        completed = self.run_adapter(envelope)
        return completed

    def test_query_returns_bounded_uncertain_citations_and_true_omissions(self) -> None:
        completed = self.query("query-pagination")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = parse_level1_result(completed.stdout.rstrip(b"\n"))
        self.assertEqual(result.freshness.value, "unknown")
        self.assertEqual(result.findings[0].path, "src/widget.py")
        self.assertEqual(result.findings[0].evidence_class.value, "uncertain")
        self.assertEqual(result.omitted_count, 6)
        self.assertTrue(result.truncated)
        self.assertNotIn(str(self.repo), completed.stdout.decode())

    def test_windows_separators_normalize_without_accepting_absolute_paths(self) -> None:
        completed = self.query("windows-path")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = parse_level1_result(completed.stdout.rstrip(b"\n"))
        self.assertEqual(result.findings[0].path, "src/widget.py")

    def test_query_rejects_schema_and_citation_attacks(self) -> None:
        for mode in (
            "absolute-path", "citation-missing", "duplicate-citation",
            "search-schema-drift", "coverage-truncated",
        ):
            with self.subTest(mode=mode):
                completed = self.query(mode)
                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(completed.stdout, b"")

    @unittest.skipUnless(
        sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file(),
        "macOS sandbox-exec is required for adapter integration",
    )
    def test_core_sandbox_and_citation_firewall_accept_the_adapter(self) -> None:
        state = Path(self.temporary.name, "state")
        state.mkdir()
        provider = state / "fixture_codebase_memory.py"
        shutil.copyfile(FIXTURE, provider)
        executable = Path(sys.executable).resolve()
        tool_root = (ROOT / "tools/taf-context").resolve()
        wire: dict[str, object] = {
            "schema_version": "1",
            "adapter_identity": "taf.codebase-memory.v0_10_8",
            "provider_identity": "codebase-memory-mcp",
            "adapter_root": str(tool_root),
            "provider_executable": str(executable),
            "provider_executable_digest": "sha256:" + hashlib.sha256(
                executable.read_bytes()
            ).hexdigest(),
            "provider_arguments": [str(provider.resolve()), "valid"],
            "provider_state_roots": [str(state.resolve())],
            "environment": {"TAF_FIXTURE_ROOT": str(self.repo)},
            "transport": "cli-json",
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
        request = self.request(inspection.index_identity)
        result, _ = query_provider(
            manifest, tool_root, request, self.repo,
            policy(timeout_seconds=3.0), binding=binding,
        )
        self.assertEqual(result.findings[0].path, "src/widget.py")
        self.assertEqual(result.freshness.value, "unknown")


if __name__ == "__main__":
    unittest.main()
