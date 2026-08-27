"""Strict host-local provider binding tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from taf_context.provider_binding import (
    AdapterBinding,
    ProviderBindingError,
    parse_adapter_binding,
    read_adapter_binding,
    validate_binding_for_repository,
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _binding_wire(adapter: Path, provider: Path, state: Path) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "1",
        "adapter_identity": "fixture.command-json",
        "provider_identity": "fixture.graph",
        "adapter_root": str(adapter.resolve()),
        "provider_executable": str(provider.resolve()),
        "provider_executable_digest": "sha256:" + hashlib.sha256(
            provider.read_bytes()
        ).hexdigest(),
        "provider_arguments": ["cli", "--raw"],
        "provider_state_roots": [str(state.resolve())],
        "environment": {"LANG": "C", "LC_ALL": "C"},
        "transport": "cli-json",
    }
    value["binding_digest"] = "sha256:" + hashlib.sha256(
        _canonical(value)
    ).hexdigest()
    return value


class AdapterBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.adapter = root / "adapter"
        self.state = root / "state"
        self.repository = root / "repository"
        self.adapter.mkdir()
        self.state.mkdir()
        self.repository.mkdir()
        self.provider = root / "provider"
        self.provider.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.provider.chmod(0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_exact_local_round_trip_and_repository_validation(self) -> None:
        wire = _binding_wire(self.adapter, self.provider, self.state)
        binding = AdapterBinding.from_dict(wire)

        self.assertEqual(binding.to_local_dict(), wire)
        self.assertEqual(
            parse_adapter_binding(_canonical(wire)), binding
        )
        validate_binding_for_repository(binding, self.repository)

    def test_parser_rejects_duplicate_nonfinite_oversized_and_digest_drift(self) -> None:
        wire = _binding_wire(self.adapter, self.provider, self.state)
        duplicate = _canonical(wire).replace(
            b'{"adapter_identity"',
            b'{"schema_version":"1","adapter_identity"',
            1,
        )
        with self.assertRaises(ProviderBindingError):
            parse_adapter_binding(duplicate)
        with self.assertRaises(ProviderBindingError):
            parse_adapter_binding(b'{"value":NaN}')
        with self.assertRaises(ProviderBindingError):
            parse_adapter_binding(b"{" + b" " * (64 * 1024))
        wire["transport"] = "mcp-stdio"
        with self.assertRaisesRegex(ProviderBindingError, "binding_digest"):
            AdapterBinding.from_dict(wire)

        wire = _binding_wire(self.adapter, self.provider, self.state)
        wire["provider_executable_digest"] = "sha256:" + "0" * 64
        wire["binding_digest"] = "sha256:" + hashlib.sha256(
            _canonical({key: value for key, value in wire.items() if key != "binding_digest"})
        ).hexdigest()
        with self.assertRaisesRegex(
            ProviderBindingError, "provider_executable_digest"
        ):
            AdapterBinding.from_dict(wire)

    def test_paths_environment_and_transport_fail_closed(self) -> None:
        cases = []
        base = _binding_wire(self.adapter, self.provider, self.state)
        for field, value in (
            ("adapter_root", "relative/adapter"),
            ("provider_executable", "relative/provider"),
            ("provider_state_roots", ["relative/state"]),
            ("transport", "http"),
            ("environment", {"API_TOKEN": "value"}),
            ("environment", {"lowercase": "value"}),
        ):
            candidate = dict(base)
            candidate[field] = value
            candidate.pop("binding_digest")
            candidate["binding_digest"] = "sha256:" + hashlib.sha256(
                _canonical(candidate)
            ).hexdigest()
            cases.append((field, candidate))
        for field, candidate in cases:
            with self.subTest(field=field), self.assertRaises(
                ProviderBindingError
            ):
                AdapterBinding.from_dict(candidate)

    def test_symlink_and_overlapping_roots_are_rejected(self) -> None:
        symlink = self.provider.with_name("provider-link")
        symlink.symlink_to(self.provider)
        wire = _binding_wire(self.adapter, symlink, self.state)
        wire["provider_executable"] = str(symlink.absolute())
        wire.pop("binding_digest")
        wire["binding_digest"] = "sha256:" + hashlib.sha256(
            _canonical(wire)
        ).hexdigest()
        with self.assertRaisesRegex(ProviderBindingError, "provider_executable"):
            AdapterBinding.from_dict(wire)

        nested = self.adapter / "state"
        nested.mkdir()
        wire = _binding_wire(self.adapter, self.provider, nested)
        with self.assertRaisesRegex(ProviderBindingError, "provider_state_roots"):
            AdapterBinding.from_dict(wire)

        wire = _binding_wire(self.adapter, self.provider, self.repository)
        binding = AdapterBinding.from_dict(wire)
        with self.assertRaisesRegex(ProviderBindingError, "repository_root"):
            validate_binding_for_repository(binding, self.repository)

    def test_secure_file_reader_rejects_symlink_and_group_readable_binding(self) -> None:
        wire = _binding_wire(self.adapter, self.provider, self.state)
        path = Path(self.temporary.name) / "binding.json"
        path.write_bytes(_canonical(wire))
        path.chmod(0o600)
        self.assertEqual(read_adapter_binding(path).to_local_dict(), wire)

        path.chmod(0o640)
        with self.assertRaisesRegex(ProviderBindingError, "binding_permissions"):
            read_adapter_binding(path)
        path.chmod(0o600)
        link = path.with_name("binding-link.json")
        link.symlink_to(path)
        with self.assertRaisesRegex(ProviderBindingError, "binding_file"):
            read_adapter_binding(link)

    def test_binding_has_no_portable_serializer(self) -> None:
        binding = AdapterBinding.from_dict(
            _binding_wire(self.adapter, self.provider, self.state)
        )
        self.assertFalse(hasattr(binding, "to_dict"))
        self.assertNotIn(str(self.provider), repr(binding.binding_digest))


if __name__ == "__main__":
    unittest.main()
