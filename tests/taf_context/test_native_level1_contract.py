from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "taf-context"))

from taf_context.level1_models import Level1Request, parse_level1_result
from taf_context.provider_execution_models import ExecutionPolicy, parse_adapter_manifest
from taf_context.provider_process import query_provider


class NativeLevel1ContractTests(unittest.TestCase):
    def test_native_result_passes_frozen_python_result_parser_and_mutations_fail(self) -> None:
        binary = _required_native_binary()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            state = root / "state"
            repository.mkdir()
            (repository / ".git").mkdir()
            (repository / "sample.go").write_text(
                "package sample\n\nfunc NativePythonContract() {}\n",
                encoding="utf-8",
            )
            template = parse_adapter_manifest(
                (ROOT / "tools/taf-context-native/adapter/manifest.template.json").read_bytes()
            )
            request = _request("build", repository, state)
            request["request"]["provider_identity"] = template.provider_identity  # type: ignore[index]
            completed = _invoke(binary, request)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stderr, b"")
            result = parse_level1_result(completed.stdout.rstrip(b"\n"))
            self.assertEqual(result.provider_identity, "taf-context")
            self.assertEqual(result.request_identity, "native-conformance-001")
            self.assertLessEqual(result.output_characters, 4000)

            mutated = json.loads(completed.stdout)
            mutated["returned_count"] = 1
            with self.assertRaises(ValueError):
                parse_level1_result(json.dumps(mutated).encode("utf-8"))

    def test_native_query_output_passes_the_existing_broker_identity_check(self) -> None:
        binary = _required_native_binary()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            state = root / "state"
            repository.mkdir()
            (repository / ".git").mkdir()
            (repository / "sample.go").write_text(
                "package sample\n\nfunc NativePythonContract() {}\n",
                encoding="utf-8",
            )
            template = parse_adapter_manifest(
                (ROOT / "tools/taf-context-native/adapter/manifest.template.json").read_bytes()
            )
            build = _request("build", repository, state)
            built = _invoke(binary, build)
            self.assertEqual(built.returncode, 0, built.stderr)
            index_identity = json.loads(built.stdout)["index_identity"]

            query = _request("search-symbols", repository, state)
            request = query["request"]
            assert isinstance(request, dict)
            request["index_identity"] = index_identity
            request["query"] = "NativePythonContract"
            completed = _invoke(binary, query)
            self.assertEqual(completed.returncode, 0, completed.stderr)

            level1_request = Level1Request.from_dict(request)
            policy = ExecutionPolicy.from_dict({
                "schema_version": "1",
                "timeout_seconds": 1.0,
                "maximum_stdout_bytes": 262144,
                "maximum_stderr_bytes": 65536,
                "network_allowed": False,
                "fallback_allowed": False,
                "maximum_inspections": 1,
            })
            with mock.patch(
                "taf_context.provider_process._execute",
                return_value=(completed.stdout.rstrip(b"\n"), mock.sentinel.attempt),
            ):
                result, attempt = query_provider(
                    template,
                    ROOT / "tools/taf-context-native/adapter",
                    level1_request,
                    repository,
                    policy,
                )
            self.assertEqual(result.provider_identity, level1_request.provider_identity)
            self.assertIs(attempt, mock.sentinel.attempt)

    def test_native_rejects_the_legacy_provider_identity(self) -> None:
        binary = _required_native_binary()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            state = root / "state"
            repository.mkdir()
            (repository / ".git").mkdir()
            request = _request("estimate", repository, state)
            request["request"]["provider_identity"] = "taf.native.level1"  # type: ignore[index]
            completed = _invoke(binary, request)
            self.assertEqual(completed.returncode, 2)
            self.assertEqual(completed.stdout, b"")
            self.assertEqual(completed.stderr, b"invalid-native-level1-request\n")

    def test_adapter_template_is_local_and_declares_the_frozen_surface(self) -> None:
        raw = (ROOT / "tools/taf-context-native/adapter/manifest.template.json").read_bytes()
        template = parse_adapter_manifest(raw)
        self.assertEqual(template.adapter_identity, "taf.level1-native")
        self.assertEqual(template.provider_identity, "taf-context")
        self.assertEqual(template.executable, "bin/taf-level1")
        self.assertEqual(template.environment_allowlist, ())
        self.assertFalse(template.network_required)
        self.assertEqual(template.capabilities, (
            "build", "estimate", "metrics", "repository-map", "search-docs",
            "search-symbols", "source-snippets", "status", "update",
        ))
        self.assertEqual(tuple(item.value for item in template.supported_phases), (
            "build", "estimate", "inspect", "metrics", "query", "update",
        ))


def _required_native_binary() -> Path:
    value = os.environ.get("TAF_LEVEL1_BINARY")
    if not value:
        raise unittest.SkipTest("TAF_LEVEL1_BINARY is required for staged native conformance")
    binary = Path(value)
    if not binary.is_file():
        raise AssertionError(f"staged native binary is unavailable: {binary}")
    return binary


def _invoke(binary: Path, request: dict[str, object]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [str(binary)],
        input=json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _request(operation: str, repository: Path, state: Path) -> dict[str, object]:
    return {
        "phase": _phase_for_operation(operation),
        "repository_root": str(repository),
        "state_root": str(state),
        "changed_paths_document": None,
        "request": {
            "schema_version": "1",
            "request_identity": "native-conformance-001",
            "consumer_identity": "taf.work-recovery",
            "operation": operation,
            "repository_identity": "sha256:" + "c" * 64,
            "worktree_identity": "sha256:" + "c" * 64,
            "committed_head": "0123456789abcdef0123456789abcdef01234567",
            "dirty_overlay_fingerprint": "sha256:" + "c" * 64,
            "provider_identity": "taf-context",
            "index_identity": None,
            "required_capability": operation,
            "minimum_freshness": "exact",
            "query": None,
            "result_identities": [],
            "filters": {
                "path_prefixes": [], "languages": [], "symbol_kinds": [], "source_types": [],
            },
            "maximum_results": 8,
            "maximum_model_output_characters": 4000,
            "allow_inferred": False,
        },
    }


def _phase_for_operation(operation: str) -> str:
    return {
        "build": "build",
        "estimate": "estimate",
        "status": "inspect",
        "metrics": "metrics",
        "update": "update",
    }.get(operation, "query")
