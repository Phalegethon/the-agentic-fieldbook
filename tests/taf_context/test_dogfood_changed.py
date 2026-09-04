"""Opt-in end-to-end check of the change operations over one fixed commit range.

The fixture pins the symbols that the range `9bce09b..f33d96c` of this
repository touched inside three hand-checked subtrees, so the measurement is
reproducible: a clone is checked out at the head of the range and queried with
that range's base. Precision is the share of returned findings the fixture
confirms, recall the share of the fixture that came back; both are
micro-averaged over the fixture paths, so a path with many changed symbols
weighs as much as its symbols.

Only `definition` and `entry-point` findings enter those two numbers. A
`module` finding is counted separately and compared exactly, because a Go
module record covers no more than the package clause and is therefore changed
only when the first line of the file is.

The second test cross-checks `impact-candidates` against the callers fixture:
for every anchor of `callers.json` that this range changed, each hand-checked
call site must come back as a verified candidate with that anchor attributed,
and every call site that does not must be explained by one of the three
reasons the fixture records.
"""

from __future__ import annotations

from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from taf_context.cli import main

ROOT = Path(__file__).parents[2]
FIXTURE = ROOT / "tools" / "taf-context-native" / "testdata" / "dogfood" / "changed.json"
MINIMUM_PRECISION = 0.90
# The acceptance bound only asks for the recall to be reported. The floor
# below is the guard that keeps the fixture honest: without it a symbol the
# engine stops returning would leave the precision at 1.0 and pass.
MINIMUM_RECALL = 0.90
# The largest request the CLI accepts. The fixture is restricted to paths that
# fit one such answer, so a truncated result is a defect rather than a bound.
MAXIMUM_RESULTS = "64"
MAXIMUM_OUTPUT_CHARACTERS = "12000"
MEASURED_KINDS = frozenset({"definition", "entry-point"})
ABSENCE_REASONS = frozenset({"self-changed", "related-symbols-miss", "output-budget"})


@unittest.skipUnless(
    os.environ.get("TAF_DOGFOOD") == "1" and os.environ.get("TAF_LEVEL1_BINARY"),
    "set TAF_DOGFOOD=1 and TAF_LEVEL1_BINARY to run the broker dogfood",
)
class DogfoodChangedTests(unittest.TestCase):
    fixture: dict
    environment: dict[str, str]
    repository: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls._directory = tempfile.TemporaryDirectory()
        directory = Path(cls._directory.name)
        cls.repository = directory / "repo"
        cls.environment = {
            "HOME": str(directory),
            "PATH": os.environ.get("PATH", ""),
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
            "TAF_LEVEL1_BINARY": os.environ["TAF_LEVEL1_BINARY"],
            "TAF_STATE_HOME": str(directory / "state"),
        }
        head = cls.fixture["range"]["head"]
        try:
            subprocess.run(
                ["git", "clone", "-q", "--no-hardlinks", str(ROOT), str(cls.repository)],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(cls.repository), "checkout", "-q", "--detach", head],
                check=True, capture_output=True,
            )
        except subprocess.CalledProcessError as error:
            cls._directory.cleanup()
            # A checkout that cannot reach the range says nothing about the
            # operations, so it skips instead of failing.
            raise unittest.SkipTest(
                f"the fixed range head {head} is unreachable from this checkout: "
                f"{error.stderr.decode('utf-8', 'replace').strip()}"
            )
        cls._cache: dict[tuple[str, str], dict[str, object]] = {}
        built = cls._invoke(
            "prepare", "build", "--repo", str(cls.repository), "--confirm-state-write"
        )
        if built["next_safe_action"] != "use-index":
            cls._directory.cleanup()
            raise unittest.SkipTest("the clone of the fixed range did not become ready")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._directory.cleanup()

    @classmethod
    def _invoke(cls, *argv: str) -> dict[str, object]:
        stdout, stderr = StringIO(), StringIO()
        code = main(list(argv), stdout=stdout, stderr=stderr, environment=cls.environment)
        assert (code, stderr.getvalue()) == (0, ""), (code, stderr.getvalue())
        return json.loads(stdout.getvalue())

    @classmethod
    def _query(cls, operation: str, path_prefix: str, *extra: str) -> dict[str, object]:
        # Every stage of both tests asks for the same few answers, so one
        # engine call per (operation, path) pair is enough.
        key = (operation, path_prefix)
        if key not in cls._cache:
            cls._cache[key] = cls._invoke(
                "prepare",
                "query",
                "--repo",
                str(cls.repository),
                "--operation",
                operation,
                "--base",
                cls.fixture["range"]["base"],
                "--path-prefix",
                path_prefix,
                "--maximum-results",
                MAXIMUM_RESULTS,
                "--maximum-output-characters",
                MAXIMUM_OUTPUT_CHARACTERS,
                *extra,
            )
        return cls._cache[key]

    def _assert_base_is_the_fixed_range(self, result: dict[str, object]) -> None:
        base = result["base"]
        self.assertEqual(base["requested"], self.fixture["range"]["base"])
        self.assertEqual(base["source"], "explicit")
        self.assertIsNone(base["warning"])
        self.assertTrue(base["sha"].startswith(self.fixture["range"]["base"]), base["sha"])

    def test_changed_symbols_match_the_curated_touched_symbols(self) -> None:
        matched = returned_total = expected_total = 0
        per_path = []
        for entry in self.fixture["paths"]:
            path = entry["path"]
            result = self._query("changed-symbols", path)
            self.assertEqual(result["status"], "ready", path)
            self._assert_base_is_the_fixed_range(result)
            # A path the index does not carry is reported once for the whole
            # change set, and this range does contain such a path.
            self.assertIn("changed-path-not-indexed", result["warnings"])
            self.assertFalse(result["truncated"], path)
            self.assertEqual(
                sorted({finding["path"] for finding in result["findings"]}), [path]
            )
            spans = {
                (finding["qualified_name"], finding["record_kind"]): (
                    finding["start_line"],
                    finding["end_line"],
                )
                for finding in result["findings"]
            }
            expected_spans = {
                (symbol["qualified_name"], symbol["record_kind"]): (
                    symbol["start_line"],
                    symbol["end_line"],
                )
                for symbol in entry["changed_symbols"]
            }
            modules = {key for key in spans if key[1] == "module"}
            expected_modules = {key for key in expected_spans if key[1] == "module"}
            self.assertEqual(modules, expected_modules, path)
            returned = {key for key in spans if key[1] in MEASURED_KINDS}
            expected = {key for key in expected_spans if key[1] in MEASURED_KINDS}
            hits = returned & expected
            for key in sorted(hits):
                # The fixture carries the span the language's own parser
                # reports, so a matched symbol must also match line for line.
                self.assertEqual(spans[key], expected_spans[key], (path, key))
            matched += len(hits)
            returned_total += len(returned)
            expected_total += len(expected)
            per_path.append(
                {
                    "path": path,
                    "returned": len(returned),
                    "expected": len(expected),
                    "matched": len(hits),
                    "modules": sorted(name for name, _kind in modules),
                    "unconfirmed": sorted(name for name, _kind in returned - expected),
                    "missed": sorted(name for name, _kind in expected - returned),
                }
            )
        summary = {
            "schema_version": "1",
            "range": self.fixture["range"],
            "paths": len(self.fixture["paths"]),
            "precision": round(matched / returned_total, 4) if returned_total else 0.0,
            "recall": round(matched / expected_total, 4) if expected_total else 0.0,
            "matched": matched,
            "returned": returned_total,
            "expected": expected_total,
            "per_path": per_path,
        }
        # The controller stores this block as the acceptance evidence for the
        # phase, so it goes to stdout whether the assertion below passes or not.
        print(json.dumps(summary, indent=2, sort_keys=True))
        self.assertGreaterEqual(summary["precision"], MINIMUM_PRECISION, summary)
        self.assertGreaterEqual(summary["recall"], MINIMUM_RECALL, summary)

    def test_impact_candidates_confirm_the_callers_fixture_for_changed_anchors(self) -> None:
        entries = self.fixture["impact_cross_check"]
        self.assertTrue(entries)
        per_anchor = []
        for entry in entries:
            anchor = (entry["anchor_path"], entry["anchor_qualified_name"])
            impact = self._query("impact-candidates", entry["path_prefix"])
            self.assertEqual(impact["status"], "ready", entry["id"])
            self._assert_base_is_the_fixed_range(impact)
            candidates = {
                (finding["path"], finding["qualified_name"]): finding
                for finding in impact["findings"]
            }
            returned = set(candidates)
            for expected in entry["expected_candidates"]:
                key = (expected["path"], expected["qualified_name"])
                # Compared against the key set rather than the candidates, so a
                # failure names what came back instead of printing every field.
                self.assertIn(key, returned, entry["id"])
                self.assertTrue(
                    any(
                        attribution["path"] == anchor[0]
                        and attribution["qualified_name"] == anchor[1]
                        and attribution["edge_evidence"] == "verified"
                        for attribution in candidates[key]["anchors"]
                    ),
                    (entry["id"], key, candidates[key]["anchors"]),
                )
            for absent in entry["absent_callers"]:
                key = (absent["path"], absent["qualified_name"])
                self.assertIn(absent["reason"], ABSENCE_REASONS, (entry["id"], key))
                if absent["reason"] == "self-changed":
                    # The composition never offers a changed symbol as its own
                    # candidate, so this call site is in the changed set.
                    self.assertIn(
                        self._changed_identity(key),
                        {item["result_identity"] for item in impact["changed"]},
                        (entry["id"], key),
                    )
                elif absent["reason"] == "related-symbols-miss":
                    # The relationship query itself does not reach this call
                    # site, so the composition cannot either.
                    self.assertNotIn(key, self._related_callers(entry), (entry["id"], key))
                else:
                    self.assertTrue(impact["truncated"], (entry["id"], key))
            per_anchor.append(
                {
                    "id": entry["id"],
                    "anchor_path": anchor[0],
                    "anchor_qualified_name": anchor[1],
                    "changed_count": impact["changed_count"],
                    "returned_count": impact["returned_count"],
                    "omitted_count": impact["omitted_count"],
                    "truncated": impact["truncated"],
                    "attributed": len(entry["expected_candidates"]),
                    "absent": sorted(
                        f"{item['reason']}:{item['qualified_name']}"
                        for item in entry["absent_callers"]
                    ),
                }
            )
        summary = {
            "schema_version": "1",
            "range": self.fixture["range"],
            "anchors": len(entries),
            "attributed": sum(item["attributed"] for item in per_anchor),
            "per_anchor": per_anchor,
        }
        print(json.dumps(summary, indent=2, sort_keys=True))

    def _changed_identity(self, key: tuple[str, str]) -> str:
        result = self._query("changed-symbols", key[0])
        identities = [
            finding["result_identity"]
            for finding in result["findings"]
            if finding["qualified_name"] == key[1]
        ]
        self.assertEqual(len(identities), 1, key)
        return identities[0]

    def _related_callers(self, entry: dict[str, object]) -> set[tuple[str, str]]:
        found = self._invoke(
            "prepare",
            "query",
            "--repo",
            str(self.repository),
            "--operation",
            "search-symbols",
            "--query",
            entry["anchor_query"],
            "--symbol-kind",
            "definition",
            "--maximum-results",
            MAXIMUM_RESULTS,
            "--maximum-output-characters",
            MAXIMUM_OUTPUT_CHARACTERS,
        )
        anchors = [
            finding["result_identity"]
            for finding in found["findings"]
            if finding["path"] == entry["anchor_path"]
            and finding["qualified_name"] == entry["anchor_qualified_name"]
        ]
        self.assertEqual(len(anchors), 1, entry["id"])
        related = self._invoke(
            "prepare",
            "query",
            "--repo",
            str(self.repository),
            "--operation",
            "related-symbols",
            "--result-id",
            anchors[0],
            "--direction",
            "callers",
            "--maximum-results",
            MAXIMUM_RESULTS,
            "--maximum-output-characters",
            MAXIMUM_OUTPUT_CHARACTERS,
        )
        return {(finding["path"], finding["qualified_name"]) for finding in related["findings"]}


if __name__ == "__main__":
    unittest.main()
