"""Language-neutral recovery conformance vector runner."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from taf_context.recovery import classify_recovery_state
from taf_context.recovery_models import RecoveryClaim


FIXTURES = Path(__file__).parent / "conformance" / "work-recovery"
EXPECTED_IDS = {
    "integrated-stale-worktree",
    "same-head-worktree",
    "dirty-mixed-state",
    "divergence",
    "stale-note-conflict",
    "stale-test-result",
    "untracked-metadata",
    "budget-truncation",
}


class RecoveryConformanceTests(unittest.TestCase):
    def test_all_portable_vectors_match_exact_expected_records(self) -> None:
        paths = sorted(FIXTURES.glob("*.json"))
        self.assertEqual({path.stem for path in paths}, EXPECTED_IDS)

        for path in paths:
            with self.subTest(vector=path.stem):
                vector = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(vector["schema_version"], "1")
                self.assertEqual(vector["fixture_id"], path.stem)
                if vector["kind"] == "state":
                    state, reasons = classify_recovery_state(**vector["input"])
                    actual = {"state": state.value, "reason_codes": list(reasons)}
                elif vector["kind"] == "claim":
                    actual = RecoveryClaim.from_dict(vector["input"]).to_dict()
                else:
                    self.fail("unknown conformance vector kind")
                self.assertEqual(actual, vector["expected"])

    def test_vectors_contain_no_machine_local_paths(self) -> None:
        for path in FIXTURES.glob("*.json"):
            content = path.read_text(encoding="utf-8")
            self.assertNotIn("/Users/", content)
            self.assertNotIn("/home/", content)
            self.assertNotIn("C:\\", content)


if __name__ == "__main__":
    unittest.main()
