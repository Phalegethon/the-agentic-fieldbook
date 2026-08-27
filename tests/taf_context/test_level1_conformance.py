"""Schema and citation checks for the predeclared Level 1 query vectors."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from taf_context.level1_models import Level1Request, Level1ResultStatus

from .level1_corpus import CorpusClass, generate_level1_corpus


VECTOR_ROOT = Path(__file__).parent / "conformance" / "level1-query"
EXACT_VECTOR_FIELDS = {
    "schema_version",
    "vector_identity",
    "request",
    "expected_result_identities",
    "acceptable_rank_maximum",
    "expected_evidence_class",
    "expected_citations",
    "allowed_omissions",
    "forbidden_result_identities",
    "required_status",
}


class Level1ConformanceVectorTests(unittest.TestCase):
    def test_exactly_24_canonical_vectors_cover_each_read_operation_six_times(self) -> None:
        paths = sorted(VECTOR_ROOT.glob("*.json"))
        self.assertEqual(len(paths), 24)
        operation_counts: dict[str, int] = {}
        for ordinal, path in enumerate(paths, start=1):
            value = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(set(value), EXACT_VECTOR_FIELDS)
            self.assertEqual(value["schema_version"], "1")
            self.assertEqual(value["vector_identity"], f"L1-Q-{ordinal:03d}")
            request = Level1Request.from_dict(value["request"])
            operation_counts[request.operation.value] = operation_counts.get(request.operation.value, 0) + 1
            self.assertIn(value["required_status"], {item.value for item in Level1ResultStatus})
            self.assertIn(value["expected_evidence_class"], {"verified", "inferred", "uncertain"})
            self.assertGreaterEqual(value["acceptable_rank_maximum"], 1)
            self.assertGreaterEqual(value["allowed_omissions"], 0)
            for citation in value["expected_citations"]:
                self.assertEqual(set(citation), {"path", "start_line", "end_line"})
        self.assertEqual(
            operation_counts,
            {
                "repository-map": 6,
                "search-symbols": 6,
                "search-docs": 6,
                "source-snippets": 6,
            },
        )

    def test_expected_citations_and_identities_exist_in_the_generated_small_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            manifest = generate_level1_corpus(root, CorpusClass.SMALL)
            known = set(manifest.expected_record_identities)

            for path in sorted(VECTOR_ROOT.glob("*.json")):
                value = json.loads(path.read_text(encoding="utf-8"))
                for identity in value["expected_result_identities"]:
                    self.assertIn(identity, known, path.name)
                for citation in value["expected_citations"]:
                    source = root / citation["path"]
                    self.assertTrue(source.is_file(), (path.name, citation))
                    line_count = len(source.read_text(encoding="utf-8").splitlines())
                    self.assertGreaterEqual(citation["start_line"], 1)
                    self.assertLessEqual(citation["end_line"], line_count)

    def test_vectors_are_leak_free_and_canonically_serialized(self) -> None:
        for path in sorted(VECTOR_ROOT.glob("*.json")):
            raw = path.read_bytes()
            value = json.loads(raw)
            expected = (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")
            self.assertEqual(raw, expected, path.name)
            for forbidden in (b"/Users/", b"/home/", b"C:\\Users", b"the-agentic-fieldbook-dev"):
                self.assertNotIn(forbidden, raw)


if __name__ == "__main__":
    unittest.main()
