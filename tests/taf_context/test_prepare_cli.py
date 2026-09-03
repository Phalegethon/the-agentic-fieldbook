"""User-facing tests for preparing bounded repository context."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from io import StringIO
import json
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from taf_context.cli import main
from taf_context.prepare_cli import (
    FILTER_LANGUAGES,
    FILTER_SYMBOL_KINDS,
    PrepareCLIError,
    _platform_asset,
    _state_paths,
    normalize_filter_values,
    register_prepare_command,
)

from .repo_factory import init_committed_repo, write


ROOT = Path(__file__).parents[2]


FIXED_NOW = datetime(2026, 8, 30, 18, 0, 0, tzinfo=timezone.utc)


def invoke(environment: dict[str, str], *argv: str) -> tuple[int, str, str]:
    stdout = StringIO()
    stderr = StringIO()
    code = main(
        list(argv),
        stdout=stdout,
        stderr=stderr,
        utc_clock=lambda: FIXED_NOW,
        environment=environment,
    )
    return code, stdout.getvalue(), stderr.getvalue()


def decoded(stdout: str) -> dict[str, object]:
    value = json.loads(stdout)
    if not isinstance(value, dict):
        raise AssertionError("stdout was not a JSON object")
    return value


def write_fake_native_engine(
    path: Path,
    invocation_log: Path | None = None,
    *,
    partial: bool = False,
    stale: bool = False,
    snippet_stale: bool = False,
) -> None:
    source = textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import hashlib
            import json
            from pathlib import Path
            import sys

            envelope = json.loads(sys.stdin.read())
            request = envelope["request"]
            operation = request["operation"]
            state = Path(envelope["state_root"])
            invocation_log = __INVOCATION_LOG__
            if invocation_log is not None:
                with Path(invocation_log).open("a", encoding="utf-8") as stream:
                    stream.write(operation + "\\n")
            if operation == "build":
                state.mkdir(parents=True, exist_ok=True)
                (state / "fake-index").write_text("ready", encoding="utf-8")
            ready = operation == "build" or (
                operation in {
                    "status",
                    "repository-map",
                    "search-symbols",
                    "search-docs",
                    "source-snippets",
                }
                and (state / "fake-index").is_file()
            )
            payload = {
                "schema_version": "1",
                "request_identity": request["request_identity"],
                "operation": operation,
                "status": "partial" if ready and __PARTIAL__ else "ready" if ready else "partial",
                "provider_identity": "taf-context",
                "provider_version": "0.1.1",
                "index_identity": (
                    "sha256:" + hashlib.sha256(b"fake-index").hexdigest()
                    if operation == "build" else request["index_identity"]
                    if ready else None
                ),
                "repository_identity": request["repository_identity"],
                "worktree_identity": request["worktree_identity"],
                "committed_head": request["committed_head"],
                "dirty_overlay_fingerprint": request["dirty_overlay_fingerprint"],
                "freshness": "exact" if ready else "partial",
                "parser_versions": {},
                "coverage": {
                    "path_coverage": 1.0,
                    "language_coverage": 1.0,
                    "indexed_path_count": 1,
                    "excluded_path_count": 0,
                    "unsupported_language_count": 0,
                    "parse_failure_count": 0,
                    "exclusion_reason_counts": {"incomplete-extraction": 1} if __PARTIAL__ else {},
                },
                "findings": [],
                "returned_count": 0,
                "omitted_count": 0,
                "truncated": False,
                "output_characters": 0,
                "warnings": ["json-collection-limit"] if ready and __PARTIAL__ else [],
                "next_safe_action": "use-index" if ready else "build-index",
            }
            if __STALE__ and operation in {
                "status",
                "repository-map",
                "search-symbols",
                "search-docs",
                "source-snippets",
            }:
                payload["status"] = "stale"
                payload["freshness"] = "incrementally-stale"
                payload["next_safe_action"] = "rebuild-index"
            if __SNIPPET_STALE__ and operation == "source-snippets":
                # Mirrors the engine's snippetStale helper: an exact index
                # that cannot verify the requested result identities reports
                # the same status/freshness/next_safe_action as a genuinely
                # stale index (structurally-stale, update-index).
                payload["status"] = "stale"
                payload["freshness"] = "structurally-stale"
                payload["next_safe_action"] = "update-index"
            sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\\n")
            """
        ).replace("__INVOCATION_LOG__", repr(None if invocation_log is None else str(invocation_log))).replace(
            "__PARTIAL__", repr(partial)
        ).replace("__STALE__", repr(stale)).replace("__SNIPPET_STALE__", repr(snippet_stale))
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class PrepareRepoContextCommandTests(unittest.TestCase):
    def test_explicit_state_home_does_not_require_posix_home_variable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = _state_paths({"TAF_STATE_HOME": directory})

        self.assertEqual(paths.root, Path(directory))

    def test_unpublished_windows_arm_runtime_is_reported_as_unsupported(self) -> None:
        # platform is imported locally inside _platform_asset (kept off the query
        # path), so patch the real module rather than a prepare_cli attribute.
        with mock.patch("taf_context.prepare_cli.sys.platform", "win32"), mock.patch(
            "platform.machine", return_value="ARM64"
        ):
            with self.assertRaisesRegex(PrepareCLIError, "unsupported"):
                _platform_asset()

    def test_activate_downloads_verified_runtime_builds_context_and_is_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            state_home = root / "state"
            release = root / "release"
            release.mkdir()
            source_binary = root / "source-taf-level1"
            invocation_log = root / "native-invocations.log"
            write_fake_native_engine(source_binary, invocation_log)
            payload = source_binary.read_bytes()
            for system in ("darwin", "linux", "windows"):
                for machine in ("amd64", "arm64"):
                    suffix = ".exe" if system == "windows" else ""
                    name = f"taf-level1_0.1.1_{system}_{machine}{suffix}"
                    (release / name).write_bytes(payload)
                    (release / f"{name}.sha256").write_text(
                        f"{hashlib.sha256(payload).hexdigest()}  {name}\n",
                        encoding="ascii",
                    )
            environment = {
                "HOME": str(root / "home"),
                "PATH": "",
                "TAF_NATIVE_RELEASE_BASE_URL": "http://attacker.invalid",
                "TAF_STATE_HOME": str(state_home),
            }

            denied = invoke(
                environment,
                "prepare",
                "activate",
                "--repo",
                str(repo),
            )
            self.assertEqual(denied[0], 2)
            self.assertIn("explicit network confirmation required", denied[2])
            self.assertFalse(state_home.exists())

            with mock.patch(
                "taf_context.prepare_cli._NATIVE_RELEASE_BASE_URL",
                release.as_uri(),
                create=True,
            ):
                code, stdout, stderr = invoke(
                    environment,
                    "prepare",
                    "activate",
                    "--repo",
                    str(repo),
                    "--confirm-network",
                    "--confirm-state-write",
                )
            self.assertEqual((code, stderr), (0, ""))
            activated = decoded(stdout)
            self.assertEqual(activated["mode"], "activate")
            self.assertEqual(activated["context"]["status"], "ready")
            self.assertEqual(activated["next_safe_action"], "use-index")
            self.assertEqual(activated["required_authorizations"], [])

            code, stdout, stderr = invoke(
                {key: value for key, value in environment.items() if key != "TAF_NATIVE_RELEASE_BASE_URL"},
                "prepare",
                "inspect",
                "--repo",
                str(repo),
            )
            self.assertEqual((code, stderr), (0, ""))
            inspected = decoded(stdout)
            self.assertEqual(inspected["engine"]["source"], "managed")
            self.assertEqual(inspected["context"]["status"], "ready")
            self.assertEqual(inspected["next_safe_action"], "use-index")
            self.assertEqual(inspected["required_authorizations"], [])
            self.assertEqual(
                invocation_log.read_text(encoding="utf-8").splitlines(),
                ["build", "status"],
            )

    def test_ready_context_can_be_queried_without_rebuilding_or_estimating(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            native = root / "taf-level1"
            invocation_log = root / "native-invocations.log"
            write_fake_native_engine(native, invocation_log)
            environment = {
                "TAF_LEVEL1_BINARY": str(native),
                "TAF_STATE_HOME": str(root / "state"),
            }

            built = invoke(
                environment,
                "prepare",
                "build",
                "--repo",
                str(repo),
                "--confirm-state-write",
            )
            self.assertEqual((built[0], built[2]), (0, ""))

            code, stdout, stderr = invoke(
                environment,
                "prepare",
                "query",
                "--repo",
                str(repo),
                "--operation",
                "search-symbols",
                "--query",
                "Widget",
                "--maximum-results",
                "3",
            )

            self.assertEqual((code, stderr), (0, ""))
            result = decoded(stdout)
            self.assertEqual(result["mode"], "query")
            self.assertEqual(result["operation"], "search-symbols")
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["required_authorizations"], [])
            self.assertLessEqual(len(stdout), 4000)
            self.assertEqual(
                invocation_log.read_text(encoding="utf-8").splitlines(),
                ["build", "search-symbols"],
            )

    def test_partial_context_is_bound_inspectable_and_queryable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            native = root / "taf-level1"
            invocation_log = root / "native-invocations.log"
            write_fake_native_engine(native, invocation_log, partial=True)
            environment = {
                "TAF_LEVEL1_BINARY": str(native),
                "TAF_STATE_HOME": str(root / "state"),
            }

            code, stdout, stderr = invoke(
                environment,
                "prepare",
                "build",
                "--repo",
                str(repo),
                "--confirm-state-write",
            )
            self.assertEqual((code, stderr), (0, ""))
            built = decoded(stdout)
            self.assertEqual(built["context"]["status"], "partial")
            self.assertEqual(built["context"]["freshness"], "exact")
            self.assertEqual(built["next_safe_action"], "use-index")
            self.assertEqual(built["context"]["coverage"]["parse_failure_count"], 0)
            self.assertIn("json-collection-limit", built["warnings"])

            code, stdout, stderr = invoke(
                environment,
                "prepare",
                "inspect",
                "--repo",
                str(repo),
            )
            self.assertEqual((code, stderr), (0, ""))
            inspected = decoded(stdout)
            self.assertEqual(inspected["context"]["status"], "partial")
            self.assertEqual(inspected["next_safe_action"], "use-index")
            self.assertEqual(inspected["required_authorizations"], [])

            code, stdout, stderr = invoke(
                environment,
                "prepare",
                "query",
                "--repo",
                str(repo),
                "--operation",
                "repository-map",
            )
            self.assertEqual((code, stderr), (0, ""))
            queried = decoded(stdout)
            self.assertEqual(queried["status"], "partial")
            self.assertEqual(queried["next_safe_action"], "use-index")
            self.assertIn("json-collection-limit", queried["warnings"])
            self.assertEqual(
                invocation_log.read_text(encoding="utf-8").splitlines(),
                ["build", "status", "repository-map"],
            )

    def test_stale_query_result_is_an_error_and_does_not_touch_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            state_home = root / "state"
            fresh_binary = root / "taf-level1"
            write_fake_native_engine(fresh_binary)
            stale_binary = root / "taf-level1-stale"
            invocation_log = root / "native-invocations.log"
            write_fake_native_engine(stale_binary, invocation_log, stale=True)
            environment = {
                "HOME": str(root / "home"),
                "PATH": "",
                "TAF_LEVEL1_BINARY": str(fresh_binary),
                "TAF_STATE_HOME": str(state_home),
            }
            code, _stdout, stderr = invoke(environment, "prepare", "build", "--repo", str(repo), "--confirm-state-write")
            self.assertEqual((code, stderr), (0, ""))
            binding = next(state_home.glob("repositories/*/*/binding.json"))
            old = 1_600_000_000
            os.utime(binding, (old, old))

            environment["TAF_LEVEL1_BINARY"] = str(stale_binary)
            code, stdout, stderr = invoke(
                environment, "prepare", "query", "--repo", str(repo),
                "--operation", "search-symbols", "--query", "Widget",
            )

            self.assertEqual((code, stdout), (2, ""))
            self.assertIn("ready context is required", stderr)
            self.assertEqual(binding.stat().st_mtime, old)
            self.assertEqual(
                invocation_log.read_text(encoding="utf-8").splitlines(),
                ["search-symbols"],
            )

    def test_unverifiable_snippet_identities_are_refused_with_a_specific_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            state_home = root / "state"
            fresh_binary = root / "taf-level1"
            write_fake_native_engine(fresh_binary)
            snippet_stale_binary = root / "taf-level1-snippet-stale"
            invocation_log = root / "native-invocations.log"
            write_fake_native_engine(snippet_stale_binary, invocation_log, snippet_stale=True)
            environment = {
                "HOME": str(root / "home"),
                "PATH": "",
                "TAF_LEVEL1_BINARY": str(fresh_binary),
                "TAF_STATE_HOME": str(state_home),
            }
            code, _stdout, stderr = invoke(environment, "prepare", "build", "--repo", str(repo), "--confirm-state-write")
            self.assertEqual((code, stderr), (0, ""))
            binding = next(state_home.glob("repositories/*/*/binding.json"))
            old = 1_600_000_000
            os.utime(binding, (old, old))

            environment["TAF_LEVEL1_BINARY"] = str(snippet_stale_binary)
            result_id = "sha256:" + ("a" * 64)
            code, stdout, stderr = invoke(
                environment, "prepare", "query", "--repo", str(repo),
                "--operation", "source-snippets", "--result-id", result_id,
            )

            self.assertEqual((code, stdout), (2, ""))
            self.assertIn(
                "result identities could not be verified against the current index; re-run the search query",
                stderr,
            )
            self.assertEqual(binding.stat().st_mtime, old)
            self.assertEqual(
                invocation_log.read_text(encoding="utf-8").splitlines(),
                ["source-snippets"],
            )

    def test_query_requires_an_existing_exact_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            native = root / "taf-level1"
            invocation_log = root / "native-invocations.log"
            write_fake_native_engine(native, invocation_log)

            code, stdout, stderr = invoke(
                {
                    "TAF_LEVEL1_BINARY": str(native),
                    "TAF_STATE_HOME": str(root / "state"),
                },
                "prepare",
                "query",
                "--repo",
                str(repo),
                "--operation",
                "repository-map",
            )

            self.assertEqual((code, stdout), (2, ""))
            self.assertIn("ready context is required", stderr)
            self.assertFalse(invocation_log.exists())

    def test_activate_rejects_corrupt_download_without_installing_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            state_home = root / "state"
            release = root / "release"
            release.mkdir()
            for system in ("darwin", "linux", "windows"):
                for machine in ("amd64", "arm64"):
                    suffix = ".exe" if system == "windows" else ""
                    name = f"taf-level1_0.1.1_{system}_{machine}{suffix}"
                    (release / name).write_bytes(b"corrupt")
                    (release / f"{name}.sha256").write_text(
                        f"{'0' * 64}  {name}\n", encoding="ascii"
                    )
            environment = {
                "HOME": str(root / "home"),
                "PATH": "",
                "TAF_NATIVE_RELEASE_BASE_URL": "http://attacker.invalid",
                "TAF_STATE_HOME": str(state_home),
            }

            with mock.patch(
                "taf_context.prepare_cli._NATIVE_RELEASE_BASE_URL",
                release.as_uri(),
                create=True,
            ):
                code, _stdout, stderr = invoke(
                    environment,
                    "prepare",
                    "activate",
                    "--repo",
                    str(repo),
                    "--confirm-network",
                    "--confirm-state-write",
                )

            self.assertEqual(code, 2)
            self.assertIn("native engine checksum mismatch", stderr)
            self.assertFalse(any(state_home.rglob("taf-level1")))

    def test_plugin_skill_entrypoint_runs_inspect_in_a_real_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(root / "home"),
                    "PATH": str(Path(shutil.which("git") or "/usr/bin/git").parent),
                    "TAF_STATE_HOME": str(root / "state"),
                }
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(
                        ROOT
                        / "skills"
                        / "prepare-repo-context"
                        / "scripts"
                        / "prepare_repo_context.py"
                    ),
                    "inspect",
                    "--repo",
                    str(repo),
                ],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual((completed.returncode, completed.stderr), (0, ""))
            self.assertEqual(decoded(completed.stdout)["next_safe_action"], "install-native-engine")

    def test_inspect_without_native_engine_is_read_only_and_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            state_home = root / "state"
            environment = {
                "HOME": str(root / "home"),
                "PATH": "",
                "TAF_STATE_HOME": str(state_home),
            }

            code, stdout, stderr = invoke(
                environment,
                "prepare",
                "inspect",
                "--repo",
                str(repo),
            )

            self.assertEqual((code, stderr), (0, ""))
            result = decoded(stdout)
            self.assertEqual(result["mode"], "inspect")
            self.assertEqual(result["repository"]["tracked_file_count"], 1)
            self.assertFalse(result["repository"]["dirty"])
            self.assertEqual(result["engine"]["availability"], "unavailable")
            self.assertEqual(result["next_safe_action"], "install-native-engine")
            self.assertEqual(result["required_authorizations"], ["network", "state-write"])
            self.assertFalse(state_home.exists())
            self.assertLessEqual(len(stdout), 4000)

    def test_inspect_uses_available_native_engine_without_persistent_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            state_home = root / "state"
            binary = root / "taf-level1"
            write_fake_native_engine(binary)
            environment = {
                "HOME": str(root / "home"),
                "PATH": "",
                "TAF_LEVEL1_BINARY": str(binary),
                "TAF_STATE_HOME": str(state_home),
            }

            code, stdout, stderr = invoke(
                environment,
                "prepare",
                "inspect",
                "--repo",
                str(repo),
            )

            self.assertEqual((code, stderr), (0, ""))
            result = decoded(stdout)
            self.assertEqual(result["engine"]["availability"], "available")
            self.assertEqual(result["estimate"]["eligible_path_count"], 1)
            self.assertEqual(result["context"]["freshness"], "partial")
            self.assertEqual(result["next_safe_action"], "build-index")
            self.assertEqual(result["required_authorizations"], ["state-write"])
            self.assertFalse(state_home.exists())

    def test_build_requires_explicit_write_confirmation_then_returns_ready_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            state_home = root / "state"
            binary = root / "taf-level1"
            write_fake_native_engine(binary)
            environment = {
                "HOME": str(root / "home"),
                "PATH": "",
                "TAF_LEVEL1_BINARY": str(binary),
                "TAF_STATE_HOME": str(state_home),
            }

            denied = invoke(
                environment,
                "prepare",
                "build",
                "--repo",
                str(repo),
            )
            self.assertEqual(denied[0], 2)
            self.assertIn("explicit state-write confirmation required", denied[2])
            self.assertFalse(state_home.exists())

            code, stdout, stderr = invoke(
                environment,
                "prepare",
                "build",
                "--repo",
                str(repo),
                "--confirm-state-write",
            )

            self.assertEqual((code, stderr), (0, ""))
            result = decoded(stdout)
            self.assertEqual(result["mode"], "build")
            self.assertEqual(result["context"]["status"], "ready")
            self.assertEqual(result["context"]["freshness"], "exact")
            self.assertEqual(result["next_safe_action"], "use-index")
            self.assertEqual(result["required_authorizations"], [])
            self.assertTrue(state_home.is_dir())

    def test_inspect_output_has_no_provider_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            binary = root / "taf-level1"
            write_fake_native_engine(binary)
            environment = {
                "HOME": str(root / "home"),
                "PATH": "",
                "TAF_LEVEL1_BINARY": str(binary),
                "TAF_STATE_HOME": str(root / "state"),
            }

            code, stdout, stderr = invoke(environment, "prepare", "inspect", "--repo", str(repo))

            self.assertEqual((code, stderr), (0, ""))
            result = decoded(stdout)
            self.assertNotIn("providers", result)
            self.assertEqual(result["engine"]["availability"], "available")

    def test_inspect_reports_state_usage_without_creating_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            state_home = root / "state"
            environment = {
                "HOME": str(root / "home"),
                "PATH": "",
                "TAF_STATE_HOME": str(state_home),
            }

            code, stdout, stderr = invoke(environment, "prepare", "inspect", "--repo", str(repo))

            self.assertEqual((code, stderr), (0, ""))
            result = decoded(stdout)
            self.assertEqual(
                result["state"],
                {"root_bytes": 0, "entry_count": 0, "orphan_count": 0, "stale_runtime_count": 0},
            )
            self.assertFalse(state_home.exists())

    def test_successful_use_refreshes_binding_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            state_home = root / "state"
            binary = root / "taf-level1"
            write_fake_native_engine(binary)
            environment = {
                "HOME": str(root / "home"),
                "PATH": "",
                "TAF_LEVEL1_BINARY": str(binary),
                "TAF_STATE_HOME": str(state_home),
            }
            code, _stdout, stderr = invoke(environment, "prepare", "build", "--repo", str(repo), "--confirm-state-write")
            self.assertEqual((code, stderr), (0, ""))
            binding = next(state_home.glob("repositories/*/*/binding.json"))
            old = 1_600_000_000
            os.utime(binding, (old, old))

            code, _stdout, stderr = invoke(environment, "prepare", "inspect", "--repo", str(repo))
            self.assertEqual((code, stderr), (0, ""))
            self.assertGreater(binding.stat().st_mtime, old)

            os.utime(binding, (old, old))
            code, _stdout, stderr = invoke(
                environment, "prepare", "query", "--repo", str(repo), "--operation", "repository-map"
            )
            self.assertEqual((code, stderr), (0, ""))
            self.assertGreater(binding.stat().st_mtime, old)

    def test_stale_context_does_not_refresh_binding_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            state_home = root / "state"
            fresh_binary = root / "taf-level1"
            write_fake_native_engine(fresh_binary)
            stale_binary = root / "taf-level1-stale"
            write_fake_native_engine(stale_binary, stale=True)
            environment = {
                "HOME": str(root / "home"),
                "PATH": "",
                "TAF_LEVEL1_BINARY": str(fresh_binary),
                "TAF_STATE_HOME": str(state_home),
            }
            code, _stdout, stderr = invoke(environment, "prepare", "build", "--repo", str(repo), "--confirm-state-write")
            self.assertEqual((code, stderr), (0, ""))
            binding = next(state_home.glob("repositories/*/*/binding.json"))
            old = 1_600_000_000
            os.utime(binding, (old, old))

            environment["TAF_LEVEL1_BINARY"] = str(stale_binary)
            code, stdout, stderr = invoke(environment, "prepare", "inspect", "--repo", str(repo))
            self.assertEqual((code, stderr), (0, ""))
            self.assertEqual(decoded(stdout)["next_safe_action"], "rebuild-index")
            self.assertEqual(binding.stat().st_mtime, old)

    def test_remove_is_a_dry_run_until_state_write_is_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            state_home = root / "state"
            binary = root / "taf-level1"
            write_fake_native_engine(binary)
            environment = {
                "HOME": str(root / "home"),
                "PATH": "",
                "TAF_LEVEL1_BINARY": str(binary),
                "TAF_STATE_HOME": str(state_home),
            }
            code, _stdout, stderr = invoke(environment, "prepare", "build", "--repo", str(repo), "--confirm-state-write")
            self.assertEqual((code, stderr), (0, ""))
            entry = next(state_home.glob("repositories/*/*/binding.json")).parent

            code, stdout, stderr = invoke(environment, "prepare", "remove", "--repo", str(repo))
            self.assertEqual((code, stderr), (0, ""))
            result = decoded(stdout)
            self.assertEqual(result["mode"], "remove")
            self.assertTrue(result["dry_run"])
            self.assertEqual([c["category"] for c in result["candidates"]], ["worktree-entry"])
            self.assertEqual(result["removed"], [])
            self.assertEqual(result["required_authorizations"], ["state-write"])
            self.assertTrue(entry.exists())

            code, stdout, stderr = invoke(environment, "prepare", "remove", "--repo", str(repo), "--confirm-state-write")
            self.assertEqual((code, stderr), (0, ""))
            result = decoded(stdout)
            self.assertFalse(result["dry_run"])
            self.assertEqual([c["category"] for c in result["removed"]], ["worktree-entry"])
            self.assertEqual(result["required_authorizations"], [])
            self.assertFalse(entry.exists())

            code, stdout, stderr = invoke(environment, "prepare", "inspect", "--repo", str(repo))
            self.assertEqual((code, stderr), (0, ""))
            self.assertEqual(decoded(stdout)["next_safe_action"], "build-index")

    def test_dry_run_with_nothing_to_reclaim_needs_no_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = {"HOME": directory, "PATH": "", "TAF_STATE_HOME": str(Path(directory) / "state")}
            code, stdout, stderr = invoke(environment, "prepare", "gc")
            self.assertEqual((code, stderr), (0, ""))
            result = decoded(stdout)
            self.assertEqual(result["candidates"], [])
            self.assertEqual(result["required_authorizations"], [])
            self.assertEqual(result["next_safe_action"], "none")

    def test_gc_dry_run_then_confirmed_removal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            state_home = root / "state"
            environment = {"HOME": str(root / "home"), "PATH": "", "TAF_STATE_HOME": str(state_home)}
            orphan = state_home / "repositories" / ("b" * 64) / ("1" * 64) / "native" / "generations" / "g"
            orphan.mkdir(parents=True)
            (orphan / "index.bin").write_bytes(b"x" * 10)
            (state_home / "runtime" / "0.0.1" / "darwin-arm64").mkdir(parents=True)
            (state_home / "runtime" / "0.0.1" / "darwin-arm64" / "taf-level1").write_bytes(b"old")

            code, stdout, stderr = invoke(environment, "prepare", "gc")
            self.assertEqual((code, stderr), (0, ""))
            result = decoded(stdout)
            self.assertEqual(result["mode"], "gc")
            self.assertTrue(result["dry_run"])
            self.assertEqual(
                sorted(c["category"] for c in result["candidates"]),
                ["empty-parent", "orphan-entry", "stale-runtime"],
            )
            self.assertTrue(orphan.exists())

            code, stdout, stderr = invoke(environment, "prepare", "gc", "--unused-for", "30", "--confirm-state-write")
            self.assertEqual((code, stderr), (0, ""))
            result = decoded(stdout)
            self.assertFalse(result["dry_run"])
            self.assertEqual(len(result["removed"]), 3)
            self.assertFalse(orphan.exists())
            self.assertFalse((state_home / "runtime" / "0.0.1").exists())

            code, stdout, stderr = invoke(environment, "prepare", "inspect", "--repo", str(repo))
            self.assertEqual((code, stderr), (0, ""))
            self.assertEqual(decoded(stdout)["state"]["orphan_count"], 0)

    def test_gc_rejects_negative_unused_for(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = {"HOME": directory, "PATH": "", "TAF_STATE_HOME": str(Path(directory) / "state")}
            code, _stdout, stderr = invoke(environment, "prepare", "gc", "--unused-for", "-1")
            self.assertEqual(code, 2)
            self.assertIn("invalid-unused-for", stderr)


class QueryArgumentTests(unittest.TestCase):
    def test_query_defaults_to_a_four_thousand_character_budget(self) -> None:
        parser = argparse.ArgumentParser()
        register_prepare_command(parser.add_subparsers(dest="command"))
        args = parser.parse_args(["prepare", "query", "--repo", ".", "--operation", "repository-map"])
        self.assertEqual(args.maximum_output_characters, 4000)

    def test_filter_values_are_lower_cased_deduplicated_and_sorted(self) -> None:
        self.assertEqual(
            normalize_filter_values(["Go", "python", "GO"], "--language", FILTER_LANGUAGES),
            ["go", "python"],
        )
        self.assertEqual(
            normalize_filter_values(["Definition", "heading"], "--symbol-kind", FILTER_SYMBOL_KINDS),
            ["definition", "heading"],
        )

    def test_invalid_filter_value_names_the_flag_and_lists_valid_values(self) -> None:
        with self.assertRaises(PrepareCLIError) as caught:
            normalize_filter_values(["cobol"], "--language", FILTER_LANGUAGES)
        message = str(caught.exception)
        self.assertIn("--language", message)
        self.assertIn("'cobol'", message)
        self.assertIn("go, javascript, json, markdown, python, rust, toml, typescript", message)

    def test_query_accepts_mixed_case_filters_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = init_committed_repo(root / "repo")
            binary = root / "taf-level1"
            write_fake_native_engine(binary)
            environment = {"TAF_LEVEL1_BINARY": str(binary), "TAF_STATE_HOME": str(root / "state")}
            code, _stdout, stderr = invoke(environment, "prepare", "build", "--repo", str(repo), "--confirm-state-write")
            self.assertEqual((code, stderr), (0, ""))
            code, stdout, stderr = invoke(
                environment, "prepare", "query", "--repo", str(repo), "--operation", "search-symbols",
                "--query", "Widget", "--language", "Go", "--symbol-kind", "Definition",
                "--source-type", "Source",
            )
            self.assertEqual((code, stderr), (0, ""))
            self.assertEqual(decoded(stdout)["operation"], "search-symbols")
            code, _stdout, stderr = invoke(
                environment, "prepare", "query", "--repo", str(repo), "--operation", "search-symbols",
                "--query", "Widget", "--symbol-kind", "widget",
            )
            self.assertNotEqual(code, 0)
            self.assertIn("valid values", stderr)


class BindingSchemaTests(unittest.TestCase):
    def _built(self, root: Path) -> tuple[Path, Path, dict[str, str]]:
        repo = init_committed_repo(root / "repo")
        write(repo / "notes.txt", "untracked\n")
        binary = root / "taf-level1"
        write_fake_native_engine(binary)
        environment = {"HOME": str(root / "home"), "PATH": "", "TAF_LEVEL1_BINARY": str(binary), "TAF_STATE_HOME": str(root / "state")}
        code, _stdout, stderr = invoke(environment, "prepare", "build", "--repo", str(repo), "--confirm-state-write")
        self.assertEqual((code, stderr), (0, ""))
        binding = next((root / "state").glob("repositories/*/*/binding.json"))
        return repo, binding, environment

    def test_build_writes_schema_2_with_head_and_dirty_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, binding, _environment = self._built(Path(directory))
            value = json.loads(binding.read_text(encoding="utf-8"))
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(value["schema_version"], "2")
            self.assertEqual(value["head_sha"], head)
            self.assertRegex(value["dirty_fingerprint"], r"^sha256:[0-9a-f]{64}$")
            self.assertEqual(value["dirty_paths"], ["notes.txt"])
            self.assertEqual(set(value), {"schema_version", "repository_identity", "worktree_identity", "index_identity", "head_sha", "dirty_fingerprint", "dirty_paths"})

    def test_schema_1_binding_is_accepted_without_delta_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, binding, environment = self._built(Path(directory))
            value = json.loads(binding.read_text(encoding="utf-8"))
            legacy = {key: value[key] for key in ("repository_identity", "worktree_identity", "index_identity")}
            legacy["schema_version"] = "1"
            binding.write_text(json.dumps(legacy), encoding="utf-8")
            code, stdout, stderr = invoke(environment, "prepare", "query", "--repo", str(repo), "--operation", "repository-map")
            self.assertEqual((code, stderr), (0, ""))
            self.assertEqual(decoded(stdout)["status"], "ready")

    def test_too_many_dirty_paths_writes_null(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch("taf_context.refresh.MAXIMUM_BINDING_DIRTY_PATHS", 1):
                _repo, binding, _environment = self._built(Path(directory))
            self.assertIsNone(json.loads(binding.read_text(encoding="utf-8"))["dirty_paths"])

    def test_malformed_schema_2_fields_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo, binding, environment = self._built(Path(directory))
            value = json.loads(binding.read_text(encoding="utf-8"))
            value["head_sha"] = "not-a-commit"
            binding.write_text(json.dumps(value), encoding="utf-8")
            code, stdout, stderr = invoke(environment, "prepare", "query", "--repo", str(repo), "--operation", "repository-map")
            self.assertEqual((code, stdout), (2, ""))
            self.assertIn("context binding is invalid", stderr)


class QueryPathImportTests(unittest.TestCase):
    def test_importing_the_cli_does_not_load_installer_modules(self) -> None:
        # Network, temp-file, and platform modules are only needed by activate
        # and by build's binding write; loading them on every query costs about
        # 20 ms of a 55 ms import. Measured in a fresh interpreter so the test
        # process's own imports cannot mask a regression.
        probe = (
            "import sys, taf_context.cli; "
            "print(sorted(name for name in ('platform', 'tempfile', 'urllib.error', 'urllib.request') if name in sys.modules))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=str(ROOT / "tools" / "taf-context"),
            env={"PATH": os.environ.get("PATH", ""), "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"},
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(completed.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
