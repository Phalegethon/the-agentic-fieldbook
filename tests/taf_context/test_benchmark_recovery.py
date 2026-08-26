"""Validation tests for retained work-recovery benchmark evidence."""

from __future__ import annotations

import copy
import unittest

from tests.taf_context.benchmark_recovery import (
    BenchmarkValidationError,
    _correct,
    summarize_samples,
    validate_benchmark_result,
)


SAMPLE = {
    "fixture_id": "small-clean",
    "budget_characters": 2000,
    "sample_index": 1,
    "warm_elapsed_ms": 2.5,
    "peak_rss_kib": 1024,
    "characters_used": 900,
    "omitted_item_count": 0,
    "correct": True,
    "counters": {
        "mutation": 0,
        "network": 0,
        "provider": 0,
        "validation": 0,
        "output_write": 0,
        "other_worktree_read": 0,
        "second_collection": 0,
    },
}


def result() -> dict[str, object]:
    samples = [copy.deepcopy(SAMPLE), {**copy.deepcopy(SAMPLE), "sample_index": 2, "warm_elapsed_ms": 3.5}]
    return {
        "schema_version": "1",
        "seed": 20260826,
        "budgets": [2000, 4000, 8000, 12000],
        "samples_per_fixture_budget": 2,
        "fixture_ids": ["small-clean"],
        "cold_samples": [
            {
                "fixture_id": "small-clean",
                "budget_characters": 2000,
                "elapsed_ms": 5.0,
                "characters_used": 900,
                "correct": True,
            }
        ],
        "samples": samples,
        "aggregates": summarize_samples(samples),
        "selected_budget_characters": 2000,
        "language_decision": "retain-python",
    }


class BenchmarkValidationTests(unittest.TestCase):
    def test_omission_pressure_gate_requires_a_real_omission(self) -> None:
        class Coverage:
            budget_characters = 2000
            omitted_item_count = 0

        class Dossier:
            coverage = Coverage()

        class Result:
            model_text = (
                "## Scope\n## Current Workstream\n## Evidence Claims\n"
                "## Coverage and Omissions\n## Next-Action Boundary\n"
            )
            characters_used = len(model_text)
            dossier = Dossier()

        self.assertFalse(_correct(Result(), 2000, require_omissions=True))

    def test_valid_result_uses_leaf_derived_aggregates(self) -> None:
        value = result()

        validate_benchmark_result(value, expected_samples=2)

        aggregate = value["aggregates"][0]
        self.assertEqual(aggregate["sample_count"], 2)
        self.assertEqual(aggregate["mean_warm_elapsed_ms"], 3.0)
        self.assertEqual(aggregate["maximum_characters_used"], 900)

    def test_rejects_boolean_float_or_negative_integer_counts(self) -> None:
        for field, value in (
            ("peak_rss_kib", True),
            ("characters_used", 1.5),
            ("omitted_item_count", -1),
        ):
            with self.subTest(field=field):
                invalid = result()
                invalid["samples"][0][field] = value
                with self.assertRaisesRegex(BenchmarkValidationError, field):
                    validate_benchmark_result(invalid, expected_samples=2)

    def test_rejects_nonfinite_timings_and_noncanonical_fixture_ids(self) -> None:
        invalid_time = result()
        invalid_time["samples"][0]["warm_elapsed_ms"] = float("nan")
        with self.assertRaisesRegex(BenchmarkValidationError, "warm_elapsed_ms"):
            validate_benchmark_result(invalid_time, expected_samples=2)

        invalid_id = result()
        invalid_id["samples"][0]["fixture_id"] = "Small Clean"
        with self.assertRaisesRegex(BenchmarkValidationError, "fixture_id"):
            validate_benchmark_result(invalid_id, expected_samples=2)

    def test_rejects_nonzero_forbidden_action_counters(self) -> None:
        for counter in SAMPLE["counters"]:
            with self.subTest(counter=counter):
                invalid = result()
                invalid["samples"][0]["counters"][counter] = 1
                with self.assertRaisesRegex(BenchmarkValidationError, counter):
                    validate_benchmark_result(invalid, expected_samples=2)

    def test_rejects_missing_samples_failures_and_tampered_aggregates(self) -> None:
        missing = result()
        missing["samples"].pop()
        with self.assertRaisesRegex(BenchmarkValidationError, "sample count"):
            validate_benchmark_result(missing, expected_samples=2)

        failed = result()
        failed["samples"][0]["correct"] = False
        with self.assertRaisesRegex(BenchmarkValidationError, "correct"):
            validate_benchmark_result(failed, expected_samples=2)

        tampered = result()
        tampered["aggregates"][0]["mean_warm_elapsed_ms"] = 99.0
        with self.assertRaisesRegex(BenchmarkValidationError, "aggregates"):
            validate_benchmark_result(tampered, expected_samples=2)


if __name__ == "__main__":
    unittest.main()
