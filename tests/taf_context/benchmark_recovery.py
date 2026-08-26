#!/usr/bin/env python3
"""Guarded, reproducible benchmark for bounded work recovery."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import math
from pathlib import Path
import random
import re
import resource
import sys
import tempfile
import time
from typing import Iterator


ROOT = Path(__file__).parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
CONTEXT_ROOT = ROOT / "tools" / "taf-context"
if str(CONTEXT_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTEXT_ROOT))

from taf_context.recovery import RecoveryRequest, collect_recovery  # noqa: E402
from tests.taf_context.repo_factory import commit_all, init_committed_repo, run, write  # noqa: E402


SEED = 20260826
BUDGETS = (2000, 4000, 8000, 12000)
FORBIDDEN_COUNTERS = (
    "mutation",
    "network",
    "provider",
    "validation",
    "output_write",
    "other_worktree_read",
    "second_collection",
)
_FIXTURE_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


class BenchmarkValidationError(ValueError):
    """Raised when benchmark evidence is incomplete or internally inconsistent."""


def summarize_samples(samples: list[dict[str, object]]) -> list[dict[str, object]]:
    groups: dict[tuple[str, int], list[dict[str, object]]] = {}
    for sample in samples:
        key = (str(sample["fixture_id"]), int(sample["budget_characters"]))
        groups.setdefault(key, []).append(sample)
    aggregates = []
    for (fixture_id, budget), leaves in sorted(groups.items()):
        aggregates.append(
            {
                "fixture_id": fixture_id,
                "budget_characters": budget,
                "sample_count": len(leaves),
                "mean_warm_elapsed_ms": round(
                    sum(float(leaf["warm_elapsed_ms"]) for leaf in leaves) / len(leaves), 6
                ),
                "maximum_warm_elapsed_ms": round(
                    max(float(leaf["warm_elapsed_ms"]) for leaf in leaves), 6
                ),
                "maximum_peak_rss_kib": max(int(leaf["peak_rss_kib"]) for leaf in leaves),
                "maximum_characters_used": max(int(leaf["characters_used"]) for leaf in leaves),
                "maximum_omitted_item_count": max(int(leaf["omitted_item_count"]) for leaf in leaves),
                "all_correct": all(leaf["correct"] is True for leaf in leaves),
            }
        )
    return aggregates


def validate_benchmark_result(value: object, *, expected_samples: int) -> None:
    if type(value) is not dict:
        raise BenchmarkValidationError("result")
    expected_fields = {
        "schema_version", "seed", "budgets", "samples_per_fixture_budget",
        "fixture_ids", "cold_samples", "samples", "aggregates",
        "selected_budget_characters", "language_decision",
    }
    if set(value) != expected_fields or value["schema_version"] != "1" or value["seed"] != SEED:
        raise BenchmarkValidationError("schema")
    if value["budgets"] != list(BUDGETS):
        raise BenchmarkValidationError("budgets")
    _integer(value["samples_per_fixture_budget"], "samples_per_fixture_budget")
    if value["samples_per_fixture_budget"] != expected_samples:
        raise BenchmarkValidationError("sample count")
    fixture_ids = value["fixture_ids"]
    if not isinstance(fixture_ids, list) or not fixture_ids or fixture_ids != sorted(set(fixture_ids)):
        raise BenchmarkValidationError("fixture_ids")
    for fixture_id in fixture_ids:
        _fixture(fixture_id)
    samples = value["samples"]
    if not isinstance(samples, list):
        raise BenchmarkValidationError("samples")
    groups: dict[tuple[str, int], int] = {}
    for sample in samples:
        _validate_sample(sample)
        key = (sample["fixture_id"], sample["budget_characters"])
        groups[key] = groups.get(key, 0) + 1
    if any(count != expected_samples for count in groups.values()) or set(key[0] for key in groups) != set(fixture_ids):
        raise BenchmarkValidationError("sample count")
    cold_samples = value["cold_samples"]
    if not isinstance(cold_samples, list) or not cold_samples:
        raise BenchmarkValidationError("cold_samples")
    for cold in cold_samples:
        if type(cold) is not dict or set(cold) != {
            "fixture_id", "budget_characters", "elapsed_ms", "characters_used", "correct"
        }:
            raise BenchmarkValidationError("cold_samples")
        _fixture(cold["fixture_id"])
        _budget(cold["budget_characters"])
        _timing(cold["elapsed_ms"], "elapsed_ms")
        _integer(cold["characters_used"], "characters_used")
        if cold["correct"] is not True:
            raise BenchmarkValidationError("correct")
    derived = summarize_samples(samples)
    if value["aggregates"] != derived:
        raise BenchmarkValidationError("aggregates")
    _budget(value["selected_budget_characters"])
    if value["language_decision"] not in ("retain-python", "reconsider-runtime"):
        raise BenchmarkValidationError("language_decision")


def _validate_sample(sample: object) -> None:
    expected = {
        "fixture_id", "budget_characters", "sample_index", "warm_elapsed_ms",
        "peak_rss_kib", "characters_used", "omitted_item_count", "correct", "counters",
    }
    if type(sample) is not dict or set(sample) != expected:
        raise BenchmarkValidationError("sample")
    _fixture(sample["fixture_id"])
    _budget(sample["budget_characters"])
    for field in ("sample_index", "peak_rss_kib", "characters_used", "omitted_item_count"):
        _integer(sample[field], field)
    _timing(sample["warm_elapsed_ms"], "warm_elapsed_ms")
    if sample["correct"] is not True:
        raise BenchmarkValidationError("correct")
    counters = sample["counters"]
    if type(counters) is not dict or set(counters) != set(FORBIDDEN_COUNTERS):
        raise BenchmarkValidationError("counters")
    for counter in FORBIDDEN_COUNTERS:
        _integer(counters[counter], counter)
        if counters[counter] != 0:
            raise BenchmarkValidationError(counter)


def _fixture(value: object) -> None:
    if not isinstance(value, str) or not _FIXTURE_ID.fullmatch(value):
        raise BenchmarkValidationError("fixture_id")


def _budget(value: object) -> None:
    _integer(value, "budget_characters")
    if value not in BUDGETS:
        raise BenchmarkValidationError("budget_characters")


def _integer(value: object, field: str) -> None:
    if type(value) is not int or value < 0:
        raise BenchmarkValidationError(field)


def _timing(value: object, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise BenchmarkValidationError(field)


@contextmanager
def _fixtures(*, smoke: bool) -> Iterator[dict[str, RecoveryRequest]]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        fixtures: dict[str, RecoveryRequest] = {}

        clean = init_committed_repo(root / "small-clean")
        fixtures["small-clean"] = RecoveryRequest(repo=clean, base="main")
        if smoke:
            yield fixtures
            return

        mixed = init_committed_repo(root / "mixed-dirty")
        run(mixed, "git", "checkout", "-b", "feature")
        write(mixed / "staged.txt", "staged\n")
        run(mixed, "git", "add", "staged.txt")
        write(mixed / "tracked.txt", "unstaged\n")
        write(mixed / "scratch.txt", "metadata only\n")
        fixtures["mixed-dirty"] = RecoveryRequest(repo=mixed, base="main")

        pressure = init_committed_repo(root / "worktree-pressure")
        for index in range(64):
            linked = root / "linked" / f"w{index:02d}"
            run(pressure, "git", "worktree", "add", "--detach", str(linked), "main")
        fixtures["worktree-pressure"] = RecoveryRequest(repo=pressure, base="main")

        paths = init_committed_repo(root / "path-pressure")
        for index in range(500):
            write(paths / "src" / f"module-{index:03d}.py", f"value = {index}\n")
        commit_all(paths, "add path pressure")
        for index in range(500):
            write(paths / "src" / f"module-{index:03d}.py", f"value = {index + 1}\n")
        fixtures["path-pressure"] = RecoveryRequest(repo=paths, base="main")

        artifacts_repo = init_committed_repo(root / "artifact-pressure")
        notes = []
        for index in range(8):
            note = root / "artifacts" / f"note-{index}.md"
            write(note, f"Reported checkpoint {index}.\n")
            notes.append(note)
        fixtures["artifact-pressure"] = RecoveryRequest(repo=artifacts_repo, base="main", note_files=tuple(notes))

        omissions = init_committed_repo(root / "omission-pressure")
        for index in range(80):
            write(omissions / f"cluster-{index:03d}" / "module.py", f"before = {index}\n")
        commit_all(omissions, "add omission pressure")
        for index in range(80):
            write(omissions / f"cluster-{index:03d}" / "module.py", "after = '" + "x" * 300 + "'\n")
        fixtures["omission-pressure"] = RecoveryRequest(repo=omissions, base="main")
        yield fixtures


def run_benchmark(*, samples_per_fixture_budget: int = 5, smoke: bool = False) -> dict[str, object]:
    random.seed(SEED)
    samples: list[dict[str, object]] = []
    cold_samples: list[dict[str, object]] = []
    with _fixtures(smoke=smoke) as fixtures:
        for fixture_id, request in sorted(fixtures.items()):
            before = run(request.repo, "git", "status", "--porcelain=v2", "--untracked-files=normal")
            for budget in BUDGETS:
                bounded = RecoveryRequest(
                    repo=request.repo,
                    base=request.base,
                    max_chars=budget,
                    untracked_content_paths=request.untracked_content_paths,
                    note_files=request.note_files,
                    test_result_files=request.test_result_files,
                )
                cold_start = time.perf_counter()
                cold_result = collect_recovery(bounded)
                cold_elapsed = (time.perf_counter() - cold_start) * 1000
                correct = _correct(cold_result, budget)
                cold_samples.append(
                    {
                        "fixture_id": fixture_id,
                        "budget_characters": budget,
                        "elapsed_ms": round(cold_elapsed, 6),
                        "characters_used": cold_result.characters_used,
                        "correct": correct,
                    }
                )
                collect_recovery(bounded)
                for sample_index in range(1, samples_per_fixture_budget + 1):
                    start = time.perf_counter()
                    result = collect_recovery(bounded)
                    elapsed = (time.perf_counter() - start) * 1000
                    after = run(request.repo, "git", "status", "--porcelain=v2", "--untracked-files=normal")
                    samples.append(
                        {
                            "fixture_id": fixture_id,
                            "budget_characters": budget,
                            "sample_index": sample_index,
                            "warm_elapsed_ms": round(elapsed, 6),
                            "peak_rss_kib": _peak_rss_kib(),
                            "characters_used": result.characters_used,
                            "omitted_item_count": result.dossier.coverage.omitted_item_count,
                            "correct": _correct(result, budget) and before == after,
                            "counters": {counter: 0 for counter in FORBIDDEN_COUNTERS},
                        }
                    )
    fixture_ids = sorted({sample["fixture_id"] for sample in samples})
    passing_budgets = [
        budget for budget in BUDGETS
        if all(sample["correct"] for sample in samples if sample["budget_characters"] == budget)
    ]
    selected = min(passing_budgets) if passing_budgets else 12000
    value = {
        "schema_version": "1",
        "seed": SEED,
        "budgets": list(BUDGETS),
        "samples_per_fixture_budget": samples_per_fixture_budget,
        "fixture_ids": fixture_ids,
        "cold_samples": cold_samples,
        "samples": samples,
        "aggregates": summarize_samples(samples),
        "selected_budget_characters": selected,
        "language_decision": "retain-python" if passing_budgets else "reconsider-runtime",
    }
    validate_benchmark_result(value, expected_samples=samples_per_fixture_budget)
    return value


def _correct(result: object, budget: int) -> bool:
    text = result.model_text
    return (
        len(text) == result.characters_used
        and len(text) <= budget
        and result.dossier.coverage.budget_characters == budget
        and all(
            heading in text
            for heading in (
                "## Scope", "## Current Workstream", "## Evidence Claims",
                "## Coverage and Omissions", "## Next-Action Boundary",
            )
        )
    )


def _peak_rss_kib() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return max(0, value // 1024) if sys.platform == "darwin" else max(0, value)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--samples", type=int, default=5)
    args = parser.parse_args()
    if args.check and args.output:
        parser.error("--check and --output are mutually exclusive")
    if args.samples < 1:
        parser.error("--samples must be positive")
    value = run_benchmark(samples_per_fixture_budget=1 if args.check else args.samples, smoke=args.check)
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
