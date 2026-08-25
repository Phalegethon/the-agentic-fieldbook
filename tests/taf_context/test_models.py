"""Behavioral tests for the portable context manifest schema."""

from __future__ import annotations

import copy
import unittest

from taf_context.models import (
    BackgroundState,
    ContextManifest,
    Freshness,
    ManifestError,
    canonical_json,
)


VALID = {
    "schema_version": "1",
    "repository_identity": "sha256:repo",
    "canonical_root_fingerprint": "sha256:root",
    "git_common_dir_fingerprint": "sha256:common",
    "worktree_identity": "sha256:worktree",
    "head_sha": "a" * 40,
    "dirty_fingerprint": "sha256:clean",
    "provider_name": "taf-context",
    "provider_version": "0.1.0",
    "provider_index_id": "level0:fixture",
    "provider_schema_version": "1",
    "index_levels": ["level0"],
    "capabilities": ["repository-map", "status"],
    "created_at": "2026-08-25T00:00:00Z",
    "updated_at": "2026-08-25T00:00:00Z",
    "include_rules_hash": "sha256:default",
    "exclude_rules_hash": "sha256:default",
    "language_coverage": {"Python": 1.0},
    "path_coverage": 1.0,
    "tracked_file_count": 1,
    "indexed_file_count": 1,
    "skipped_file_count": 0,
    "parse_failure_count": 0,
    "generated_or_vendored_count": 0,
    "storage_bytes": 0,
    "background_state": "ready",
    "warnings": [],
}


class ContextManifestTests(unittest.TestCase):
    def test_exact_round_trip_serialization_freezes_collections(self) -> None:
        manifest = ContextManifest.from_dict(VALID)

        self.assertEqual(manifest.to_dict(), VALID)
        self.assertEqual(manifest.index_levels, ("level0",))
        self.assertEqual(manifest.capabilities, ("repository-map", "status"))
        self.assertEqual(manifest.language_coverage, (("Python", 1.0),))
        self.assertIs(manifest.background_state, BackgroundState.READY)

    def test_rejects_invalid_enum_value_without_echoing_source_content(self) -> None:
        invalid = copy.deepcopy(VALID)
        invalid["background_state"] = "private-state-value"

        with self.assertRaisesRegex(ManifestError, "background_state") as caught:
            ContextManifest.from_dict(invalid)

        self.assertNotIn("private-state-value", str(caught.exception))

    def test_rejects_missing_required_field(self) -> None:
        invalid = copy.deepcopy(VALID)
        del invalid["provider_index_id"]

        with self.assertRaisesRegex(ManifestError, "provider_index_id"):
            ContextManifest.from_dict(invalid)

    def test_rejects_unknown_schema_version(self) -> None:
        invalid = copy.deepcopy(VALID)
        invalid["schema_version"] = "999"

        with self.assertRaisesRegex(ManifestError, "schema_version"):
            ContextManifest.from_dict(invalid)

    def test_head_object_id_is_exactly_sha1_or_sha256_hex(self) -> None:
        for head in ("a" * 39, "a" * 41, "a" * 63, "a" * 65, "g" * 40):
            with self.subTest(head=head):
                invalid = copy.deepcopy(VALID)
                invalid["head_sha"] = head
                with self.assertRaisesRegex(ManifestError, "head_sha"):
                    ContextManifest.from_dict(invalid)

        sha256 = copy.deepcopy(VALID)
        sha256["head_sha"] = "b" * 64
        self.assertEqual(ContextManifest.from_dict(sha256).head_sha, "b" * 64)

    def test_rejects_non_repository_relative_capability_and_warning(self) -> None:
        for field in ("capabilities", "warnings"):
            with self.subTest(field=field):
                invalid = copy.deepcopy(VALID)
                invalid[field] = ["/private/source/content"]

                with self.assertRaisesRegex(ManifestError, field) as caught:
                    ContextManifest.from_dict(invalid)

                self.assertNotIn("/private/source/content", str(caught.exception))

    def test_rejects_coverage_outside_unit_interval(self) -> None:
        invalid = copy.deepcopy(VALID)
        invalid["path_coverage"] = 1.01

        with self.assertRaisesRegex(ManifestError, "path_coverage"):
            ContextManifest.from_dict(invalid)

    def test_rejects_negative_counts(self) -> None:
        invalid = copy.deepcopy(VALID)
        invalid["storage_bytes"] = -1

        with self.assertRaisesRegex(ManifestError, "storage_bytes"):
            ContextManifest.from_dict(invalid)

    def test_rejects_unknown_fields(self) -> None:
        invalid = copy.deepcopy(VALID)
        invalid["not_in_schema"] = True

        with self.assertRaisesRegex(ManifestError, "not_in_schema"):
            ContextManifest.from_dict(invalid)

    def test_canonical_json_has_deterministic_key_order_and_utf8(self) -> None:
        value = {"z": "İ", "a": [2, 1]}

        self.assertEqual(canonical_json(value), '{"a":[2,1],"z":"İ"}\n')

    def test_canonical_json_rejects_non_finite_floats(self) -> None:
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    canonical_json({"coverage": value})


class EnumWireValueTests(unittest.TestCase):
    def test_freshness_wire_values_are_stable(self) -> None:
        self.assertEqual(Freshness.EXACT.value, "exact")
        self.assertEqual(Freshness.UNUSABLE.value, "unusable")


if __name__ == "__main__":
    unittest.main()
