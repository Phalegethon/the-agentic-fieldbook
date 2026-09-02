"""Opt-in end-to-end recall check through the broker against this checkout."""

from __future__ import annotations

from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest

from taf_context.cli import main

ROOT = Path(__file__).parents[2]
FIXTURE = ROOT / "tools" / "taf-context-native" / "testdata" / "dogfood" / "recall.json"
MINIMUM_HITS = 39


def _last_segment(qualified: str) -> str:
    return qualified.rsplit(".", 1)[-1]


@unittest.skipUnless(
    os.environ.get("TAF_DOGFOOD") == "1" and os.environ.get("TAF_LEVEL1_BINARY"),
    "set TAF_DOGFOOD=1 and TAF_LEVEL1_BINARY to run the broker dogfood",
)
class DogfoodRecallTests(unittest.TestCase):
    def _invoke(self, environment: dict[str, str], *argv: str) -> dict[str, object]:
        stdout, stderr = StringIO(), StringIO()
        code = main(list(argv), stdout=stdout, stderr=stderr, environment=environment)
        self.assertEqual((code, stderr.getvalue()), (0, ""))
        return json.loads(stdout.getvalue())

    def test_default_queries_find_real_symbols_and_headings(self) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
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
            hits, misses = 0, []
            for entry in fixture["entries"]:
                argv = [
                    "prepare",
                    "query",
                    "--repo",
                    str(ROOT),
                    "--operation",
                    entry["operation"],
                    "--query",
                    entry["query"],
                ]
                for language in entry.get("languages", []):
                    argv += ["--language", language]
                for kind in entry.get("symbol_kinds", []):
                    argv += ["--symbol-kind", kind]
                result = self._invoke(environment, *argv)
                self.assertEqual(result["status"], "ready", entry["id"])
                if "query-frontier-exhausted" in result["warnings"]:
                    self.assertTrue(result["truncated"], entry["id"])
                found = any(
                    finding["path"] == entry["expected_path"]
                    and _last_segment(finding["qualified_name"]).lower()
                    == entry["expected_symbol"].lower()
                    for finding in result["findings"]
                )
                if found:
                    hits += 1
                else:
                    misses.append(entry["id"])
        self.assertGreaterEqual(hits, MINIMUM_HITS, f"misses: {misses}")
