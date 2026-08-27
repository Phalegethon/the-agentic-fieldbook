"""Determinism, sizing, mutation, and leakage tests for Level 1 corpus data."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from .level1_corpus import (
    CORPUS_SIZES,
    CorpusClass,
    CorpusManifest,
    MutationManifest,
    apply_mutation,
    generate_level1_corpus,
)

from .repo_factory import commit_all, init_repo, run


FORBIDDEN_BYTES = (
    b"/Users/",
    b"/home/",
    b"C:\\Users",
    b"the-agentic-fieldbook-dev",
    b"28bb0de31aa9f00228762b9e14c614e9ff2841cc",
    b"d06df31cb07aba3274bc980c9fc4550c9725b3bb",
)


def regular_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.parts
    }


class CorpusDeterminismTests(unittest.TestCase):
    def test_small_corpus_is_root_independent_exactly_sized_and_leak_free(self) -> None:
        with tempfile.TemporaryDirectory() as first_temp, tempfile.TemporaryDirectory() as second_temp:
            first = init_repo(Path(first_temp) / "alpha")
            second = init_repo(Path(second_temp) / "omega")

            first_manifest = generate_level1_corpus(first, CorpusClass.SMALL)
            second_manifest = generate_level1_corpus(second, CorpusClass.SMALL)

            self.assertEqual(first_manifest, second_manifest)
            self.assertEqual(first_manifest.to_json_bytes(), second_manifest.to_json_bytes())
            self.assertEqual(regular_files(first), regular_files(second))
            self.assertEqual(len(regular_files(first)), CORPUS_SIZES[CorpusClass.SMALL])
            self.assertEqual(first_manifest.first_party_file_count, 70)
            self.assertEqual(
                sum(first_manifest.language_counts.values()),
                first_manifest.first_party_file_count,
            )
            self.assertEqual(
                sum(first_manifest.exclusion_reason_counts.values()),
                CORPUS_SIZES[CorpusClass.SMALL] - first_manifest.first_party_file_count,
            )
            self.assertEqual(
                run(first, "git", "check-ignore", "generated/ignored/secret_0.env"),
                "generated/ignored/secret_0.env",
            )
            for scenario_root in first_manifest.repository_relative_roots:
                self.assertTrue((first / scenario_root).exists(), scenario_root)
            self.assertEqual(
                len((first / first_manifest.long_document_path).read_text(encoding="utf-8").splitlines()),
                10_000,
            )
            corpus_files = regular_files(first)
            for extension in (".py", ".ts", ".js", ".go", ".rs", ".md"):
                lane = b"\n".join(
                    content
                    for path, content in corpus_files.items()
                    if path.endswith(extension) and path.startswith("scenarios/")
                )
                self.assertIn(b"TAF_ENTRY_POINT", lane, extension)
                self.assertIn(b"TAF_CONFIG_KEY", lane, extension)
                self.assertIn(b"TAF_DYNAMIC_RELATION", lane, extension)

            commit_all(first)
            commit_all(second)
            self.assertEqual(
                run(first, "git", "rev-parse", "HEAD^{tree}"),
                run(second, "git", "rev-parse", "HEAD^{tree}"),
            )

            payload = b"\n".join(regular_files(first).values()) + first_manifest.to_json_bytes()
            for forbidden in FORBIDDEN_BYTES:
                self.assertNotIn(forbidden, payload)

    def test_manifest_parsers_are_strict_and_round_trip_canonical_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            manifest = generate_level1_corpus(root, CorpusClass.SMALL)

        self.assertEqual(CorpusManifest.from_dict(manifest.to_dict()), manifest)
        self.assertEqual(
            CorpusManifest.from_json_bytes(manifest.to_json_bytes()),
            manifest,
        )
        with self.assertRaises(TypeError):
            manifest.language_counts["Python"] = 0
        invalid = manifest.to_dict()
        invalid["executable"] = "echo unsafe"
        with self.assertRaises(ValueError):
            CorpusManifest.from_dict(invalid)

    def test_committed_change_mutation_is_repeatable_and_declares_forbidden_records(self) -> None:
        manifests: list[MutationManifest] = []
        files_after: list[dict[str, bytes]] = []
        for suffix in ("first", "second"):
            with tempfile.TemporaryDirectory(prefix=f"level1-{suffix}-") as temp:
                repo = init_repo(Path(temp) / "repo")
                manifest = generate_level1_corpus(repo, CorpusClass.SMALL)
                commit_all(repo)
                mutation = apply_mutation(repo, manifest, "committed-add-modify-rename-delete")
                manifests.append(mutation)
                files_after.append(regular_files(repo))

        self.assertEqual(manifests[0], manifests[1])
        self.assertEqual(files_after[0], files_after[1])
        self.assertEqual(len(manifests[0].added_paths), 1)
        self.assertEqual(len(manifests[0].modified_paths), 1)
        self.assertEqual(len(manifests[0].renamed_paths), 1)
        self.assertEqual(len(manifests[0].deleted_paths), 1)
        self.assertTrue(manifests[0].expected_record_identities)
        self.assertTrue(manifests[0].forbidden_record_identities)
        self.assertNotEqual(manifests[0].before_tree, manifests[0].after_tree)
        self.assertEqual(
            MutationManifest.from_dict(manifests[0].to_dict()),
            manifests[0],
        )


if __name__ == "__main__":
    unittest.main()
