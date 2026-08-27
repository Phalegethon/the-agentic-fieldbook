"""Hostile process-boundary tests for disposable Level 1 candidates."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional

from taf_context.level1_models import (
    CandidateAvailability,
    CandidateManifest,
    Level1Request,
    Level1ResultStatus,
)
from taf_context.level1_render import render_level1_result
from taf_context.models import Freshness

from .level1_process import (
    CandidateProcessError,
    _request_wire_bytes,
    preflight_candidate,
    run_candidate,
)
from .repo_factory import write
from .test_level1_models import INDEX_IDENTITY, request_wire
from .test_level1_render import coverage, finding


def request() -> Level1Request:
    return Level1Request.from_dict(request_wire())


def valid_result_bytes(request_value: Level1Request) -> bytes:
    rendered = render_level1_result(
        request_value,
        status=Level1ResultStatus.READY,
        provider_version="0.1.0",
        index_identity=INDEX_IDENTITY,
        freshness=Freshness.EXACT,
        parser_versions=(("fake-parser", "1.0.0"),),
        coverage=coverage(),
        ranked_findings=(finding(1),),
        warnings=(),
        next_safe_action="use-cited-evidence",
    )
    return (json.dumps(rendered.result.to_dict(), sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def candidate_script(payload: bytes, *, version_argument: Optional[str] = None) -> str:
    interpreter = Path(sys.executable).resolve()
    return f'''#!{interpreter}
import os
import socket
import subprocess
import sys
import time

required_version_argument = {version_argument!r}
if "--version" in sys.argv:
    if required_version_argument is not None and sys.argv[1:] != [required_version_argument, "--version"]:
        raise SystemExit(64)
    print("0.1.0")
    raise SystemExit(0)

mode = sys.argv[1]
payload = {payload!r}
repo = sys.argv[sys.argv.index("--repo-root") + 1]
sys.stdin.buffer.readline()

if mode == "valid":
    sys.stdout.buffer.write(payload)
elif mode == "duplicate":
    sys.stdout.buffer.write(payload.replace(b'{{', b'{{"schema_version":"1",', 1))
elif mode == "multiple":
    sys.stdout.buffer.write(payload + payload)
elif mode == "oversized-stdout":
    sys.stdout.buffer.write(b"x" * 300000)
elif mode == "oversized-stderr":
    sys.stderr.buffer.write(b"x" * 70000)
    sys.stdout.buffer.write(payload)
elif mode == "invalid-utf8":
    sys.stdout.buffer.write(b"\\xff\\n")
elif mode == "nan":
    sys.stdout.buffer.write(payload.replace(b'"output_characters":', b'"output_characters":NaN,"discarded":'))
elif mode == "wrong-request":
    sys.stdout.buffer.write(payload.replace(b'"request_identity":"request-0001"', b'"request_identity":"wrong-request"'))
elif mode == "timeout":
    time.sleep(5)
elif mode == "partial-nonzero":
    sys.stdout.buffer.write(payload[:80])
    raise SystemExit(9)
elif mode == "spawn":
    subprocess.run(["/usr/bin/true"], check=True)
    sys.stdout.buffer.write(payload)
elif mode == "socket":
    client = socket.socket()
    client.bind(("127.0.0.1", 0))
    sys.stdout.buffer.write(payload)
elif mode == "write-repo":
    with open(os.path.join(repo, "escape.txt"), "w", encoding="utf-8") as handle:
        handle.write("escape")
    sys.stdout.buffer.write(payload)
elif mode == "replace-executable":
    with open(__file__, "a", encoding="utf-8") as handle:
        handle.write("# replaced")
    sys.stdout.buffer.write(payload)
elif mode == "diagnostic-redaction":
    sys.stderr.write("token=TAF_CANARY /Users/example/private\\n")
    sys.stdout.buffer.write(payload)
else:
    raise SystemExit(64)
'''


def manifest(mode: str) -> CandidateManifest:
    return CandidateManifest.from_dict(
        {
            "schema_version": "1",
            "candidate_identity": "taf.native.level1",
            "candidate_version": "0.1.0",
            "language": "Python",
            "protocol_version": "1",
            "availability": "ready",
            "unsupported_reason_codes": [],
            "executable": "candidate.py",
            "arguments": [mode],
            "environment_allowlist": ["LANG", "PATH"],
            "declared_child_processes": [],
            "dependency_lock": "fixture.lock",
            "license_inventory": "licenses.json",
        }
    )


class CandidateProcessBoundaryTests(unittest.TestCase):
    def test_preflight_passes_manifest_arguments_to_candidate_version_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            candidate_root, _repo, _state, _evidence = self.make_fixture(root, "valid")
            executable = candidate_root / "candidate.py"
            write(
                executable,
                candidate_script(
                    valid_result_bytes(request()),
                    version_argument="valid",
                ),
            )
            executable.chmod(0o755)

            preflight = preflight_candidate(manifest("valid"), candidate_root, os.environ)

        self.assertIs(preflight.availability, CandidateAvailability.READY, preflight.reason_codes)

    def test_preflight_allows_only_a_declared_uv_managed_python_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            candidate_root = root / "candidate"
            uv_root = root / "uv-python"
            interpreter = uv_root / "cpython-3.14.7-test" / "bin" / "python3.14"
            interpreter.parent.mkdir(parents=True)
            write(interpreter, candidate_script(valid_result_bytes(request())))
            interpreter.chmod(0o755)
            executable = candidate_root / ".venv" / "bin" / "python"
            executable.parent.mkdir(parents=True)
            executable.symlink_to(interpreter)
            write(candidate_root / "fixture.lock", "fixture==1.0.0\n")
            write(candidate_root / "licenses.json", '{"fixture":"MIT"}\n')
            environment = dict(os.environ)
            environment["UV_PYTHON_INSTALL_DIR"] = str(uv_root)
            manifest_wire = manifest("valid").to_dict()
            manifest_wire["executable"] = ".venv/bin/python"
            uv_manifest = CandidateManifest.from_dict(manifest_wire)

            preflight = preflight_candidate(uv_manifest, candidate_root, environment)

        self.assertIs(preflight.availability, CandidateAvailability.READY, preflight.reason_codes)

    def test_permuted_wire_is_semantically_equal_but_byte_distinct(self) -> None:
        canonical = _request_wire_bytes(request(), False)
        permuted = _request_wire_bytes(request(), True)
        self.assertNotEqual(permuted, canonical)
        self.assertEqual(json.loads(permuted), json.loads(canonical))

    def make_fixture(self, root: Path, mode: str) -> tuple[Path, Path, Path, Path]:
        candidate_root = root / "candidate"
        repo_root = root / "repo"
        state_root = root / "state"
        evidence_root = root / "evidence"
        for path in (candidate_root, repo_root, state_root, evidence_root):
            path.mkdir(parents=True)
        write(repo_root / "source.py", "class Fixture:\n    pass\n")
        executable = candidate_root / "candidate.py"
        write(executable, candidate_script(valid_result_bytes(request())))
        executable.chmod(0o755)
        write(candidate_root / "fixture.lock", "fixture==1.0.0\n")
        write(candidate_root / "licenses.json", '{"fixture":"MIT"}\n')
        return candidate_root, repo_root, state_root, evidence_root

    def test_preflight_hashes_regular_inputs_and_rejects_symlinked_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            candidate_root, _repo, _state, _evidence = self.make_fixture(root, "valid")
            ready = preflight_candidate(manifest("valid"), candidate_root, os.environ)
            self.assertIs(ready.availability, CandidateAvailability.READY)
            self.assertTrue(ready.candidate_digest.startswith("sha256:"))
            self.assertTrue(ready.executable_digest.startswith("sha256:"))
            self.assertTrue(ready.isolation.offline_enforced)
            self.assertTrue(ready.isolation.child_process_audited)
            self.assertTrue(ready.isolation.rss_measured)

            (candidate_root / "candidate.py").unlink()
            (candidate_root / "candidate.py").symlink_to("/usr/bin/python3")
            rejected = preflight_candidate(manifest("valid"), candidate_root, os.environ)
            self.assertIs(rejected.availability, CandidateAvailability.UNSUPPORTED)
            self.assertIn("unsafe-executable", rejected.reason_codes)

    def test_valid_candidate_is_bounded_nonmutating_and_retains_redacted_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            candidate_root, repo_root, state_root, evidence_root = self.make_fixture(root, "diagnostic-redaction")
            before = (repo_root / "source.py").read_bytes()

            result, evidence = run_candidate(
                manifest("diagnostic-redaction"),
                request(),
                repo_root,
                state_root,
                2.0,
                evidence_root,
                candidate_root=candidate_root,
            )

            self.assertEqual(result.request_identity, request().request_identity)
            self.assertEqual((repo_root / "source.py").read_bytes(), before)
            self.assertEqual(evidence.exit_code, 0)
            self.assertGreater(evidence.elapsed_ns, 0)
            self.assertGreater(evidence.peak_rss_bytes, 0)
            diagnostics = (evidence_root / "diagnostics.txt").read_text(encoding="utf-8")
            self.assertNotIn("TAF_CANARY", diagnostics)
            self.assertNotIn("/Users/", diagnostics)
            self.assertTrue((evidence_root / "complete.json").is_file())

    def test_ready_build_returns_the_new_controller_bound_index_identity(self) -> None:
        build_wire = request_wire("build")
        build_request = Level1Request.from_dict(build_wire)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            candidate_root, repo_root, state_root, evidence_root = self.make_fixture(root, "valid")
            executable = candidate_root / "candidate.py"
            write(executable, candidate_script(valid_result_bytes(build_request)))
            executable.chmod(0o755)

            result, _evidence = run_candidate(
                manifest("valid"),
                build_request,
                repo_root,
                state_root,
                2.0,
                evidence_root,
                candidate_root=candidate_root,
            )

        self.assertEqual(result.index_identity, INDEX_IDENTITY)

    def test_hostile_candidates_fail_closed_and_never_overwrite_complete_evidence(self) -> None:
        hostile_modes = (
            "duplicate",
            "multiple",
            "oversized-stdout",
            "oversized-stderr",
            "invalid-utf8",
            "nan",
            "wrong-request",
            "timeout",
            "partial-nonzero",
            "spawn",
            "socket",
            "write-repo",
            "replace-executable",
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            candidate_root, repo_root, state_root, evidence_root = self.make_fixture(root, "valid")
            run_candidate(
                manifest("valid"), request(), repo_root, state_root, 2.0, evidence_root,
                candidate_root=candidate_root,
            )
            retained = (evidence_root / "complete.json").read_bytes()
            repository_before = (repo_root / "source.py").read_bytes()

            for mode in hostile_modes:
                with self.subTest(mode=mode):
                    with self.assertRaises(CandidateProcessError):
                        run_candidate(
                            manifest(mode), request(), repo_root, state_root, 0.2, evidence_root,
                            candidate_root=candidate_root,
                        )
                    self.assertEqual((evidence_root / "complete.json").read_bytes(), retained)
                    self.assertEqual((repo_root / "source.py").read_bytes(), repository_before)
                    self.assertFalse((repo_root / "escape.txt").exists())


if __name__ == "__main__":
    unittest.main()
