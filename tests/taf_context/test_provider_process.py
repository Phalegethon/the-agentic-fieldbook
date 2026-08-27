"""Fail-closed active provider process tests."""

from __future__ import annotations

from dataclasses import replace
import shutil
import tempfile
import unittest
from pathlib import Path

from taf_context.level1_models import Level1Request
from taf_context.models import RepositorySnapshot
from taf_context.provider_execution_models import AdapterManifest, ExecutionPolicy
from taf_context.provider_process import ProviderProcessError, inspect_provider, query_provider

from .test_level1_models import request_wire


REPO = "sha256:" + "1" * 64
WORKTREE = "sha256:" + "2" * 64
DIRTY = "sha256:" + "3" * 64
HEAD = "a" * 40


def snapshot() -> RepositorySnapshot:
    return RepositorySnapshot(
        "1", REPO, "root", "sha256:root", "git", "common",
        "sha256:common", WORKTREE, HEAD, "main", DIRTY, True,
        ("README.md",), (), (), (), 0, 0, 0, 0, (("Markdown", 1),),
        (), (), 0, 0, 0, (),
    )


def policy(**changes: object) -> ExecutionPolicy:
    value: dict[str, object] = {
        "schema_version": "1", "timeout_seconds": 1.0,
        "maximum_stdout_bytes": 262144, "maximum_stderr_bytes": 65536,
        "network_allowed": False, "fallback_allowed": True,
        "maximum_inspections": 2,
    }
    value.update(changes)
    return ExecutionPolicy.from_dict(value)


class ProviderProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.adapter = root / "adapter"
        (self.adapter / "bin").mkdir(parents=True)
        source = Path(__file__).with_name("fixture_provider.py")
        target = self.adapter / "bin/provider"
        shutil.copyfile(source, target)
        target.chmod(0o700)
        self.repo = root / "repo"
        self.repo.mkdir()
        (self.repo / "README.md").write_text("# Fixture\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def manifest(self, mode: str) -> AdapterManifest:
        return AdapterManifest.from_dict({
            "schema_version": "1", "adapter_identity": "fixture.stdio",
            "adapter_version": "1.0.0", "provider_identity": "fixture.graph",
            "provider_version": "2.0.0", "executable": "bin/provider",
            "arguments": [mode],
            "capabilities": ["repository-map", "search-symbols"],
            "supported_phases": ["describe", "inspect", "query"],
            "environment_allowlist": [], "locality": "local",
            "network_required": False,
        })

    def test_valid_inspection_is_bounded_and_identity_checked(self) -> None:
        record, attempt = inspect_provider(
            self.manifest("valid"), self.adapter, snapshot(), self.repo, policy()
        )
        self.assertEqual(record.provider_identity, "fixture.graph")
        self.assertEqual(attempt.status.value, "succeeded")
        self.assertGreater(attempt.stdout_bytes, 0)

    def test_hostile_inspection_outputs_fail_closed(self) -> None:
        for mode, reason in (
            ("wrong-identity", "inspection-identity-mismatch"),
            ("duplicate", "invalid-inspection-output"),
            ("multiple", "invalid-stdout-framing"),
            ("oversized-stderr", "stderr-oversized"),
        ):
            with self.subTest(mode=mode), self.assertRaisesRegex(ProviderProcessError, reason):
                inspect_provider(self.manifest(mode), self.adapter, snapshot(), self.repo, policy())

    def test_timeout_and_repository_mutation_fail_closed(self) -> None:
        with self.assertRaisesRegex(ProviderProcessError, "provider-timeout"):
            inspect_provider(
                self.manifest("timeout"), self.adapter, snapshot(), self.repo,
                policy(timeout_seconds=0.05),
            )
        with self.assertRaisesRegex(ProviderProcessError, "repository-mutated"):
            inspect_provider(
                self.manifest("write-repo"), self.adapter, snapshot(), self.repo,
                policy(),
            )

    def test_query_result_must_match_frozen_level1_request(self) -> None:
        wire = request_wire()
        wire["provider_identity"] = "fixture.graph"
        request = Level1Request.from_dict(wire)
        result, attempt = query_provider(
            self.manifest("valid"), self.adapter, request, self.repo, policy()
        )
        self.assertEqual(result.request_identity, request.request_identity)
        self.assertEqual(result.provider_identity, "fixture.graph")
        self.assertEqual(attempt.phase.value, "query")


if __name__ == "__main__":
    unittest.main()
