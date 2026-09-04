"""Opt-in end-to-end precision check of the callers direction against this checkout.

The fixture lists, for twenty functions of this repository, every enclosing
definition that really calls them. Both passes run the same anchors: the first
takes only ``verified`` edges (the default), the second adds the inferred ones
with ``--allow-inferred``. Precision is the share of returned findings that the
fixture confirms, recall the share of the fixture that came back; both are
micro-averaged over the twenty anchors, so a single anchor with many call sites
weighs as much as its call sites.
"""

from __future__ import annotations

from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest

from taf_context.cli import main

ROOT = Path(__file__).parents[2]
FIXTURE = ROOT / "tools" / "taf-context-native" / "testdata" / "dogfood" / "callers.json"
MINIMUM_PRECISION = 0.90
MINIMUM_INFERRED_RECALL = 0.80
# The widest anchor of the fixture has seventeen call sites, so the query has to
# be allowed to return more than the eight findings and 4000 characters that the
# CLI defaults to; both values are the documented maxima.
MAXIMUM_RESULTS = "64"
MAXIMUM_OUTPUT_CHARACTERS = "12000"


@unittest.skipUnless(
    os.environ.get("TAF_DOGFOOD") == "1" and os.environ.get("TAF_LEVEL1_BINARY"),
    "set TAF_DOGFOOD=1 and TAF_LEVEL1_BINARY to run the broker dogfood",
)
class DogfoodCallersTests(unittest.TestCase):
    def _invoke(self, environment: dict[str, str], *argv: str) -> dict[str, object]:
        stdout, stderr = StringIO(), StringIO()
        code = main(list(argv), stdout=stdout, stderr=stderr, environment=environment)
        self.assertEqual((code, stderr.getvalue()), (0, ""))
        return json.loads(stdout.getvalue())

    def _anchor_identity(self, environment: dict[str, str], entry: dict) -> str:
        result = self._invoke(
            environment,
            "prepare",
            "query",
            "--repo",
            str(ROOT),
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
        self.assertEqual(result["status"], "ready", entry["id"])
        anchors = [
            finding
            for finding in result["findings"]
            if finding["path"] == entry["anchor_path"]
            and finding["qualified_name"] == entry["anchor_qualified_name"]
        ]
        self.assertEqual(len(anchors), 1, f"{entry['id']}: {[f['path'] for f in result['findings']]}")
        return anchors[0]["result_identity"]

    def _callers(
        self, environment: dict[str, str], identity: str, *, allow_inferred: bool
    ) -> dict[str, object]:
        argv = [
            "prepare",
            "query",
            "--repo",
            str(ROOT),
            "--operation",
            "related-symbols",
            "--result-id",
            identity,
            "--direction",
            "callers",
            "--maximum-results",
            MAXIMUM_RESULTS,
            "--maximum-output-characters",
            MAXIMUM_OUTPUT_CHARACTERS,
        ]
        if allow_inferred:
            argv.append("--allow-inferred")
        return self._invoke(environment, *argv)

    def _pass(
        self, environment: dict[str, str], entries: list[dict], identities: dict[str, str], *, allow_inferred: bool
    ) -> dict[str, object]:
        matched = returned_total = expected_total = 0
        per_entry = []
        for entry in entries:
            result = self._callers(environment, identities[entry["id"]], allow_inferred=allow_inferred)
            self.assertIn(result["status"], {"ready", "partial"}, entry["id"])
            returned = [(finding["path"], finding["qualified_name"]) for finding in result["findings"]]
            expected = {(caller["path"], caller["qualified_name"]) for caller in entry["expected_callers"]}
            hits = [item for item in returned if item in expected]
            matched += len(hits)
            returned_total += len(returned)
            expected_total += len(expected)
            per_entry.append(
                {
                    "id": entry["id"],
                    "returned": len(returned),
                    "expected": len(expected),
                    "matched": len(hits),
                    "unconfirmed": sorted(item for item in returned if item not in expected),
                    "missed": sorted(expected - set(returned)),
                }
            )
            for finding in result["findings"]:
                self.assertEqual(finding["relation"], "call", entry["id"])
                self.assertIn(finding["edge_evidence"], {"verified", "inferred"}, entry["id"])
                if not allow_inferred:
                    self.assertEqual(finding["edge_evidence"], "verified", entry["id"])
        return {
            "precision": round(matched / returned_total, 4) if returned_total else 0.0,
            "recall": round(matched / expected_total, 4) if expected_total else 0.0,
            "matched": matched,
            "returned": returned_total,
            "expected": expected_total,
            "entries": per_entry,
        }

    def test_callers_of_twenty_functions_are_precise_and_recall_most_call_sites(self) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        entries = fixture["entries"]
        with tempfile.TemporaryDirectory() as directory:
            environment = {
                "HOME": directory,
                "PATH": "",
                "TAF_LEVEL1_BINARY": os.environ["TAF_LEVEL1_BINARY"],
                "TAF_STATE_HOME": str(Path(directory) / "state"),
            }
            built = self._invoke(
                environment,
                "prepare",
                "build",
                "--repo",
                str(ROOT),
                "--confirm-state-write",
            )
            self.assertEqual(built["next_safe_action"], "use-index")
            identities = {entry["id"]: self._anchor_identity(environment, entry) for entry in entries}
            verified = self._pass(environment, entries, identities, allow_inferred=False)
            inferred = self._pass(environment, entries, identities, allow_inferred=True)
        summary = {
            "schema_version": "1",
            "anchors": len(entries),
            "verified_only": verified,
            "allow_inferred": inferred,
        }
        # The controller stores this block as the acceptance evidence for the
        # phase, so it goes to stdout whether the assertions below pass or not.
        print(json.dumps(summary, indent=2, sort_keys=True))
        self.assertGreaterEqual(verified["precision"], MINIMUM_PRECISION, summary["verified_only"])
        self.assertGreaterEqual(inferred["recall"], MINIMUM_INFERRED_RECALL, summary["allow_inferred"])


if __name__ == "__main__":
    unittest.main()
