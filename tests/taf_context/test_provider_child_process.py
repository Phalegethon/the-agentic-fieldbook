"""Exact sandboxed provider child tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import unittest

from taf_context.provider_binding import AdapterBinding
from taf_context.provider_execution_models import AdapterManifest
from taf_context.provider_process import ProviderProcessError, inspect_provider

from .test_provider_process import policy, snapshot


def _binding(
    adapter: Path,
    provider: Path,
    state: Path,
    arguments: list[str],
) -> AdapterBinding:
    wire: dict[str, object] = {
        "schema_version": "1",
        "adapter_identity": "fixture.command-json",
        "provider_identity": "fixture.graph",
        "adapter_root": str(adapter.resolve()),
        "provider_executable": str(provider.resolve()),
        "provider_arguments": arguments,
        "provider_state_roots": [str(state.resolve())],
        "environment": {"LANG": "C", "LC_ALL": "C"},
        "transport": "cli-json",
    }
    canonical = json.dumps(
        wire, sort_keys=True, separators=(",", ":")
    ).encode()
    wire["binding_digest"] = "sha256:" + hashlib.sha256(canonical).hexdigest()
    return AdapterBinding.from_dict(wire)


@unittest.skipUnless(
    sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file(),
    "macOS sandbox-exec is required for child isolation tests",
)
class ProviderChildProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.adapter = root / "adapter"
        self.state = root / "state"
        self.repo = root / "repo"
        (self.adapter / "bin").mkdir(parents=True)
        self.state.mkdir()
        self.repo.mkdir()
        (self.repo / "README.md").write_text("# Fixture\n", encoding="utf-8")
        source = Path(__file__).with_name("fixture_command_provider.py")
        self.adapter_script = self.adapter / "bin/adapter"
        self.provider_script = self.adapter / "bin/provider-fixture.py"
        shutil.copyfile(source, self.adapter_script)
        shutil.copyfile(source, self.provider_script)
        self.adapter_script.chmod(0o700)
        self.provider_script.chmod(0o600)
        launcher = Path(sys.executable).resolve()
        self.python_app = (
            launcher.parent.parent
            / "Resources/Python.app/Contents/MacOS/Python"
        ).resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def manifest(self, mode: str) -> AdapterManifest:
        return AdapterManifest.from_dict(
            {
                "schema_version": "1",
                "adapter_identity": "fixture.command-json",
                "adapter_version": "1.0.0",
                "provider_identity": "fixture.graph",
                "provider_version": "2.0.0",
                "executable": "bin/adapter",
                "arguments": ["adapter", mode],
                "capabilities": ["repository-map", "search-symbols"],
                "supported_phases": ["inspect", "query"],
                "environment_allowlist": [],
                "locality": "local",
                "network_required": False,
            }
        )

    def binding(self, mode: str, *arguments: str) -> AdapterBinding:
        return _binding(
            self.adapter,
            self.python_app,
            self.state,
            [str(self.provider_script.resolve()), "provider", mode, *arguments],
        )

    def test_one_exact_bound_child_can_return_valid_inspection(self) -> None:
        record, attempt = inspect_provider(
            self.manifest("valid"), self.adapter, snapshot(), self.repo,
            policy(), binding=self.binding("valid"),
        )
        self.assertEqual(record.provider_identity, "fixture.graph")
        self.assertEqual(attempt.phase.value, "inspect")

    def test_repository_and_provider_state_writes_are_denied(self) -> None:
        for mode in ("write-repository", "write-state"):
            with self.subTest(mode=mode), self.assertRaisesRegex(
                ProviderProcessError, "provider-nonzero"
            ):
                inspect_provider(
                    self.manifest(mode), self.adapter, snapshot(), self.repo,
                    policy(), binding=self.binding(mode),
                )
        self.assertFalse((self.repo / "escape.txt").exists())
        self.assertFalse((self.state / "escape.txt").exists())

    def test_network_and_undeclared_children_are_denied(self) -> None:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = str(listener.getsockname()[1])
            with self.assertRaisesRegex(ProviderProcessError, "provider-nonzero"):
                inspect_provider(
                    self.manifest("network"), self.adapter, snapshot(), self.repo,
                    policy(), binding=self.binding("network", port),
                )
        for mode in ("wrong-child", "grandchild"):
            with self.subTest(mode=mode), self.assertRaisesRegex(
                ProviderProcessError, "provider-nonzero"
            ):
                inspect_provider(
                    self.manifest(mode), self.adapter, snapshot(), self.repo,
                    policy(), binding=self.binding(mode),
                )


if __name__ == "__main__":
    unittest.main()
