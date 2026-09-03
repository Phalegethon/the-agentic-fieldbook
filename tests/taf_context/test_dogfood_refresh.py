"""Opt-in end-to-end refresh check through the broker against a copy of this checkout."""

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


@unittest.skipUnless(
    os.environ.get("TAF_DOGFOOD") == "1" and os.environ.get("TAF_LEVEL1_BINARY"),
    "set TAF_DOGFOOD=1 and TAF_LEVEL1_BINARY to run the broker dogfood",
)
class DogfoodRefreshTests(unittest.TestCase):
    def _invoke(self, environment: dict[str, str], *argv: str) -> dict[str, object]:
        stdout, stderr = StringIO(), StringIO()
        code = main(list(argv), stdout=stdout, stderr=stderr, environment=environment)
        self.assertEqual((code, stderr.getvalue()), (0, ""))
        return json.loads(stdout.getvalue())

    def test_edit_then_query_finds_the_new_symbol_without_a_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory) / "repo"
            subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(ROOT), str(work)], check=True, capture_output=True)
            environment = {"HOME": directory, "PATH": os.environ.get("PATH", ""), "TAF_LEVEL1_BINARY": os.environ["TAF_LEVEL1_BINARY"], "TAF_STATE_HOME": str(Path(directory) / "state")}
            built = self._invoke(environment, "prepare", "build", "--repo", str(work), "--confirm-state-write")
            self.assertEqual(built["next_safe_action"], "use-index")
            target = work / "tools" / "taf-context" / "taf_context" / "state_paths.py"
            target.write_text(target.read_text(encoding="utf-8") + "\n\ndef refresh_dogfood_marker():\n    return 1\n", encoding="utf-8")
            result = self._invoke(environment, "prepare", "query", "--repo", str(work), "--operation", "search-symbols", "--query", "refresh_dogfood_marker")
            self.assertEqual(result["status"], "ready")
            self.assertTrue(result["refresh"]["performed"])
            self.assertEqual(result["refresh"]["changed_path_count"], 1)
            self.assertTrue(any(f["qualified_name"].endswith("refresh_dogfood_marker") for f in result["findings"]))
            again = self._invoke(environment, "prepare", "query", "--repo", str(work), "--operation", "search-symbols", "--query", "refresh_dogfood_marker")
            self.assertFalse(again["refresh"]["performed"])
