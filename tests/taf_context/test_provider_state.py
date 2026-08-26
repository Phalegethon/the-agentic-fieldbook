"""Filesystem contracts for bounded user-local provider and consent state."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from taf_context.consent import AuthorizationLedger
from taf_context.models import ContextAction, canonical_json
from taf_context.provider_models import ProviderDescriptor
from taf_context.provider_state import (
    StateError,
    append_audit,
    read_consent,
    read_project_registration,
    read_user_registry,
    resolve_state_paths,
    write_consent,
)


_MAX_JSON_BYTES = 256 * 1024
_MAX_AUDIT_BYTES = 1024 * 1024


def _descriptor(identity: str = "local.graph") -> dict[str, object]:
    return {
        "schema_version": "1",
        "provider_identity": identity,
        "provider_version": "1.0",
        "provider_schema_version": "1",
        "capabilities": ["repository-map"],
        "locality": "local",
        "discovery_sources": ["user-registry"],
        "availability": "available",
        "registration": "user-registered",
        "status_evidence": "uninspected",
        "freshness": "unknown",
        "path_coverage": None,
        "language_coverage": None,
        "latency_ms": None,
        "confidence": "verified",
        "supported_actions": ["inspect", "query"],
        "required_actions": ["query"],
        "marker_hints": [],
        "reason_codes": [],
        "warnings": [],
    }


def _ledger(action: str = "query") -> AuthorizationLedger:
    return AuthorizationLedger.from_dict(
        {
            "schema_version": "2",
            "records": [
                {
                    "action": action,
                    "repository_identity": "sha256:repo",
                    "provider_identity": "local.graph",
                    "provider_schema_version": "1",
                    "disposition": "allow",
                    "decided_at": "2026-08-26T00:00:00Z",
                    "request_digest": "sha256:" + "a" * 64,
                }
            ],
        }
    )


def _audit(index: int = 0, *, provider_identity: str = "local.graph") -> dict[str, object]:
    return {
        "timestamp": f"2026-08-26T00:00:{index % 60:02d}Z",
        "request_digest": "sha256:" + f"{index:064x}"[-64:],
        "repository_fingerprint": "sha256:repo",
        "provider_identity": provider_identity,
        "provider_schema_version": "1",
        "actions": ["query"],
        "disposition": "allow",
        "operation": "record",
    }


def _paths(root: Path):
    return resolve_state_paths(
        {"TAF_STATE_HOME": os.fspath(root), "HOME": "/not-consulted"},
        "linux",
        Path("/also-not-consulted"),
    )


class StatePathTests(unittest.TestCase):
    def test_resolves_exact_platform_roots_and_filenames_without_global_state(self) -> None:
        cases = (
            ("darwin", {}, Path("/Users/test"), Path("/Users/test/Library/Application Support/TAF/context")),
            ("linux", {"XDG_STATE_HOME": "/state"}, Path("/home/test"), Path("/state/taf/context")),
            ("linux", {}, Path("/home/test"), Path("/home/test/.local/state/taf/context")),
            ("win32", {"LOCALAPPDATA": "C:/Users/test/AppData/Local"}, Path("C:/Users/test"), Path("C:/Users/test/AppData/Local/TAF/context")),
            ("linux", {"TAF_STATE_HOME": "/exact/override", "XDG_STATE_HOME": "/ignored"}, Path("/ignored"), Path("/exact/override")),
        )
        with mock.patch.dict(os.environ, {"TAF_STATE_HOME": "/global-must-not-leak"}, clear=True):
            for platform_name, environment, home, expected in cases:
                with self.subTest(platform_name=platform_name, environment=environment):
                    paths = resolve_state_paths(environment, platform_name, home)
                    self.assertEqual(paths.root, expected)
                    self.assertEqual(paths.providers, expected / "providers.json")
                    self.assertEqual(paths.consent, expected / "consent.json")
                    self.assertEqual(paths.audit, expected / "audit.jsonl")

    def test_windows_without_local_app_data_fails_with_a_stable_code(self) -> None:
        with self.assertRaises(StateError) as caught:
            resolve_state_paths({}, "win32", Path("C:/Users/test"))
        self.assertEqual(caught.exception.code, "state-home-unavailable")

    def test_explicit_empty_override_is_not_redirected_to_a_fallback(self) -> None:
        with self.assertRaises(StateError) as caught:
            resolve_state_paths(
                {"TAF_STATE_HOME": "", "XDG_STATE_HOME": "/must-not-be-used"},
                "linux",
                Path("/must-not-be-used"),
            )
        self.assertEqual(caught.exception.code, "state-home-unavailable")


class SafeReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "state"
        self.root.mkdir()
        self.paths = _paths(self.root)

    def test_missing_registry_and_consent_are_empty_state(self) -> None:
        self.assertEqual(read_user_registry(self.paths), ())
        self.assertEqual(read_consent(self.paths), AuthorizationLedger())

    def test_reads_strict_registry_and_consent_records(self) -> None:
        self.paths.providers.write_text(canonical_json([_descriptor()]), encoding="utf-8")
        self.paths.consent.write_text(canonical_json(_ledger().to_dict()), encoding="utf-8")

        self.assertEqual(read_user_registry(self.paths), (ProviderDescriptor.from_dict(_descriptor()),))
        self.assertEqual(read_consent(self.paths), _ledger())

    def test_rejects_duplicate_keys_in_every_structured_document(self) -> None:
        self.paths.providers.write_text('[{"provider_identity":"a","provider_identity":"b"}]', encoding="utf-8")
        with self.assertRaises(StateError) as registry:
            read_user_registry(self.paths)
        self.assertEqual(registry.exception.code, "provider-registry-invalid")

        self.paths.consent.write_text('{"schema_version":"2","schema_version":"2","records":[]}', encoding="utf-8")
        with self.assertRaises(StateError) as consent:
            read_consent(self.paths)
        self.assertEqual(consent.exception.code, "consent-corrupt")

        repo = Path(self.temporary.name) / "repo"
        target = repo / ".taf/context/registration.json"
        target.parent.mkdir(parents=True)
        target.write_text('{"schema_version":"1","schema_version":"1","repository_identity":"sha256:repo","providers":[]}', encoding="utf-8")
        with self.assertRaises(StateError) as registration:
            read_project_registration(repo, "sha256:repo")
        self.assertEqual(registration.exception.code, "project-registration-invalid")

    def test_rejects_oversize_before_attempting_json_parsing(self) -> None:
        self.paths.providers.write_bytes(b"{" + b" " * _MAX_JSON_BYTES)
        with self.assertRaises(StateError) as caught:
            read_user_registry(self.paths)
        self.assertEqual(caught.exception.code, "control-too-large")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_rejects_symlink_and_nonregular_state_files(self) -> None:
        outside = Path(self.temporary.name) / "outside.json"
        outside.write_text("[]\n", encoding="utf-8")
        self.paths.providers.symlink_to(outside)
        with self.assertRaises(StateError) as linked:
            read_user_registry(self.paths)
        self.assertEqual(linked.exception.code, "unsafe-state-file")

        self.paths.providers.unlink()
        self.paths.providers.mkdir()
        with self.assertRaises(StateError) as directory:
            read_user_registry(self.paths)
        self.assertEqual(directory.exception.code, "unsafe-state-file")

        if hasattr(os, "mkfifo"):
            fifo = self.root / "fifo"
            os.mkfifo(fifo)
            fifo_paths = self.paths.__class__(self.root, self.paths.providers, fifo, self.paths.audit)
            with self.assertRaises(StateError) as pipe:
                read_consent(fifo_paths)
            self.assertEqual(pipe.exception.code, "unsafe-state-file")

    def test_rejects_growth_during_bounded_read(self) -> None:
        self.paths.providers.write_text("[]\n", encoding="utf-8")
        real_read = os.read
        changed = False

        def growing_read(fd: int, count: int) -> bytes:
            nonlocal changed
            data = real_read(fd, count)
            if data and not changed:
                changed = True
                with self.paths.providers.open("ab") as stream:
                    stream.write(b" ")
            return data

        with mock.patch("taf_context.provider_state.os.read", side_effect=growing_read):
            with self.assertRaises(StateError) as caught:
                read_user_registry(self.paths)
        self.assertEqual(caught.exception.code, "unsafe-state-file")


class ProjectRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name) / "repo"
        self.registration = self.repo / ".taf/context/registration.json"
        self.registration.parent.mkdir(parents=True)

    def _write(self, identity: str = "sha256:repo") -> None:
        self.registration.write_text(
            canonical_json(
                {
                    "schema_version": "1",
                    "repository_identity": identity,
                    "providers": [
                        {
                            "provider_identity": "local.graph",
                            "provider_schema_version": "1",
                            "required_capabilities": ["repository-map"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_reads_only_exact_registration_and_ignores_identity_mismatch(self) -> None:
        self._write()
        before = sorted((path.relative_to(self.repo), path.lstat()) for path in self.repo.rglob("*"))

        parsed = read_project_registration(self.repo, "sha256:repo")

        after = sorted((path.relative_to(self.repo), path.lstat()) for path in self.repo.rglob("*"))
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.repository_identity, "sha256:repo")  # type: ignore[union-attr]
        self.assertEqual([(path, item.st_mtime_ns, item.st_size) for path, item in before],
                         [(path, item.st_mtime_ns, item.st_size) for path, item in after])
        self.assertIsNone(read_project_registration(self.repo, "sha256:other"))

    def test_missing_registration_returns_none(self) -> None:
        self.registration.unlink(missing_ok=True)
        self.assertIsNone(read_project_registration(self.repo, "sha256:repo"))

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_descriptor_relative_traversal_rejects_each_symlink_position(self) -> None:
        outside = Path(self.temporary.name) / "outside"
        (outside / "context").mkdir(parents=True)
        target = outside / "context/registration.json"
        target.write_text("{}\n", encoding="utf-8")

        for position in ("dot-taf", "context", "registration"):
            with self.subTest(position=position):
                if self.repo.exists():
                    for path in sorted(self.repo.rglob("*"), reverse=True):
                        if path.is_symlink() or path.is_file():
                            path.unlink()
                        else:
                            path.rmdir()
                self.repo.mkdir(exist_ok=True)
                if position == "dot-taf":
                    (self.repo / ".taf").symlink_to(outside, target_is_directory=True)
                elif position == "context":
                    (self.repo / ".taf").mkdir()
                    (self.repo / ".taf/context").symlink_to(outside / "context", target_is_directory=True)
                else:
                    (self.repo / ".taf/context").mkdir(parents=True)
                    (self.repo / ".taf/context/registration.json").symlink_to(target)
                with self.assertRaises(StateError) as caught:
                    read_project_registration(self.repo, "sha256:repo")
                self.assertEqual(caught.exception.code, "unsafe-project-registration")


class AtomicWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "nested/state"
        self.paths = _paths(self.root)

    def test_writes_owner_only_state_and_fsyncs_file_replace_then_directory(self) -> None:
        events: list[str] = []
        real_fsync = os.fsync
        real_replace = os.replace

        def recording_fsync(fd: int) -> None:
            events.append("directory-fsync" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file-fsync")
            real_fsync(fd)

        def recording_replace(source: object, destination: object, *args: object, **kwargs: object) -> None:
            events.append("replace")
            real_replace(source, destination, *args, **kwargs)

        with mock.patch("taf_context.provider_state.os.fsync", side_effect=recording_fsync), mock.patch(
            "taf_context.provider_state.os.replace", side_effect=recording_replace
        ):
            write_consent(self.paths, _ledger(), _audit())

        self.assertEqual(events, ["file-fsync", "replace", "directory-fsync"] * 2)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(self.paths.consent.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.paths.audit.stat().st_mode), 0o600)
        self.assertEqual(read_consent(self.paths), _ledger())
        self.assertEqual(json.loads(self.paths.audit.read_text(encoding="utf-8")), _audit())

    @unittest.skipUnless(os.name == "posix", "POSIX mode contract")
    def test_narrows_an_existing_state_directory_to_owner_only(self) -> None:
        self.root.mkdir(parents=True, mode=0o755)
        os.chmod(self.root, 0o755)

        append_audit(self.paths, _audit())

        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)

    def test_replace_failure_preserves_ready_state_and_cleans_temporary_file(self) -> None:
        self.root.mkdir(parents=True)
        old = canonical_json(AuthorizationLedger().to_dict()).encode("utf-8")
        self.paths.consent.write_bytes(old)
        real_replace = os.replace

        def fail_consent(source: object, destination: object, *args: object, **kwargs: object) -> None:
            if Path(destination) == self.paths.consent:
                raise OSError("injected replace failure")
            real_replace(source, destination, *args, **kwargs)

        with mock.patch("taf_context.provider_state.os.replace", side_effect=fail_consent):
            with self.assertRaises(StateError) as caught:
                write_consent(self.paths, _ledger(), _audit())

        self.assertEqual(caught.exception.code, "state-write-failed")
        self.assertEqual(self.paths.consent.read_bytes(), old)
        self.assertFalse(self.paths.audit.exists())
        self.assertEqual(sorted(self.root.iterdir()), [self.paths.consent])

    def test_corrupt_consent_is_preserved_instead_of_replaced(self) -> None:
        self.root.mkdir(parents=True)
        corrupt = b'{"schema_version":"2","records":['
        self.paths.consent.write_bytes(corrupt)

        with self.assertRaises(StateError) as caught:
            write_consent(self.paths, _ledger(), _audit())

        self.assertEqual(caught.exception.code, "consent-corrupt")
        self.assertEqual(self.paths.consent.read_bytes(), corrupt)
        self.assertFalse(self.paths.audit.exists())

    def test_preparation_failure_installs_neither_consent_nor_audit(self) -> None:
        oversized_audit = _audit(provider_identity="x" * _MAX_AUDIT_BYTES)
        with self.assertRaises(StateError):
            write_consent(self.paths, _ledger(), oversized_audit)
        self.assertFalse(self.paths.consent.exists())
        self.assertFalse(self.paths.audit.exists())

        oversized_ledger = AuthorizationLedger.from_dict(
            {
                "schema_version": "2",
                "records": [
                    {
                        "action": "query",
                        "repository_identity": "sha256:repo",
                        "provider_identity": "local.graph",
                        "provider_schema_version": "1",
                        "disposition": "allow",
                        "decided_at": "2026-08-26T00:00:00Z",
                        "request_digest": "sha256:" + f"{index:064x}"[-64:],
                    }
                    for index in range(1200)
                ],
            }
        )
        with self.assertRaises(StateError):
            write_consent(self.paths, oversized_ledger, _audit())
        self.assertFalse(self.paths.consent.exists())
        self.assertFalse(self.paths.audit.exists())


class AuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "state"
        self.paths = _paths(self.root)

    def _read_lines(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.paths.audit.read_text(encoding="utf-8").splitlines()]

    def test_rejects_nonmetadata_and_nonregular_audit_records(self) -> None:
        with self.assertRaises(StateError) as extra:
            append_audit(self.paths, {**_audit(), "prompt": "secret"})
        self.assertEqual(extra.exception.code, "audit-record-invalid")
        self.assertFalse(self.paths.audit.exists())

        self.root.mkdir(parents=True)
        self.paths.audit.mkdir()
        with self.assertRaises(StateError) as unsafe:
            append_audit(self.paths, _audit())
        self.assertEqual(unsafe.exception.code, "unsafe-state-file")

    def test_rotation_keeps_newest_1024_complete_records(self) -> None:
        self.root.mkdir(parents=True)
        records = [_audit(index) for index in range(1024)]
        self.paths.audit.write_text("".join(canonical_json(item) for item in records), encoding="utf-8")

        append_audit(self.paths, _audit(1024))

        retained = self._read_lines()
        self.assertEqual(len(retained), 1024)
        self.assertEqual(retained[0]["request_digest"], _audit(1)["request_digest"])
        self.assertEqual(retained[-1]["request_digest"], _audit(1024)["request_digest"])

    def test_rotation_keeps_newest_complete_records_below_one_mibibyte(self) -> None:
        first = _audit(1, provider_identity="a" * 600_000)
        second = _audit(2, provider_identity="b" * 600_000)

        append_audit(self.paths, first)
        append_audit(self.paths, second)

        retained = self._read_lines()
        self.assertEqual(retained, [second])
        self.assertLessEqual(self.paths.audit.stat().st_size, _MAX_AUDIT_BYTES)


if __name__ == "__main__":
    unittest.main()
