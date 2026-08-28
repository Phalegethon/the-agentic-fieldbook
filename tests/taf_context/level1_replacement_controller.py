"""Controller-owned schedules and immutable replacement-v2 evidence writes."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

from .level1_replacement_scoring import ReplacementEvidence


_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_IDENTITY = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_CONTROLLER_FIELDS = {
    "record_type", "sample_identity", "retained", "ordinal", "query_class",
    "expected_result_identities", "environment_identity", "policy_identity",
    "candidate_identity", "candidate_digest", "relevant_source_bytes",
    "budget_characters", "declared_changed_files",
}
_OBSERVED_FIELDS = {
    "product": {
        "artifact_size_bytes", "cross_platform_targets",
        "dependency_license_checks_passed", "dependency_license_checks_total",
        "maintenance_checks_passed", "maintenance_checks_total", "unsafe_code_blocks",
    },
    "build": {
        "elapsed_ns", "peak_rss_bytes", "index_and_staging_bytes",
        "index_digest", "repository_writes",
        "network_attempts", "undeclared_child_processes", "state_escapes",
    },
    "query": {
        "elapsed_ns", "actual_result_identities", "citation_match",
        "evidence_class_match", "freshness_honest", "forbidden_result_count",
        "response_characters", "repository_files_opened",
        "repository_bytes_read", "considered_records", "full_repository_operations",
        "repository_writes", "network_attempts", "undeclared_child_processes",
        "state_escapes",
    },
    "update": {
        "elapsed_ns", "enumerated_repository_files",
        "parsed_repository_files", "incremental_digest", "rebuild_digest",
        "repository_writes", "network_attempts", "undeclared_child_processes",
        "state_escapes",
    },
    "index-determinism": {"first_index_digest", "second_index_digest"},
}


@dataclass(frozen=True)
class ReplacementSample:
    record_type: str
    sample_identity: str
    retained: Optional[bool] = None
    ordinal: Optional[int] = None
    query_class: Optional[str] = None

    def __post_init__(self) -> None:
        if self.record_type not in _OBSERVED_FIELDS:
            raise ValueError("invalid replacement sample type")
        if not _IDENTITY.fullmatch(self.sample_identity):
            raise ValueError("invalid replacement sample identity")
        lifecycle = self.record_type in {"build", "query", "update"}
        if lifecycle is not (type(self.retained) is bool and type(self.ordinal) is int and self.ordinal >= 0):
            raise ValueError("invalid replacement lifecycle sample")
        if self.record_type == "query":
            if self.query_class not in {"exact", "fuzzy"}:
                raise ValueError("invalid replacement query class")
        elif self.query_class is not None:
            raise ValueError("query class on non-query sample")

    def controller_fields(
        self,
        expected: tuple[str, ...],
        *,
        relevant_source_bytes: int,
        model_output_budget_characters: int,
        declared_changed_files: int,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "record_type": self.record_type,
            "sample_identity": self.sample_identity,
        }
        if self.retained is not None:
            result["retained"] = self.retained
        if self.ordinal is not None:
            result["ordinal"] = self.ordinal
        if self.query_class is not None:
            result["query_class"] = self.query_class
            result["expected_result_identities"] = list(expected)
            result["budget_characters"] = model_output_budget_characters
        if self.record_type == "build":
            result["relevant_source_bytes"] = relevant_source_bytes
        if self.record_type == "update":
            result["declared_changed_files"] = declared_changed_files
        return result


def replacement_stage_a_schedule() -> tuple[ReplacementSample, ...]:
    samples = [ReplacementSample("product", "product-metadata")]
    samples.extend(
        ReplacementSample("build", f"build-{ordinal}", True, ordinal)
        for ordinal in range(5)
    )
    for query_class in ("exact", "fuzzy"):
        samples.extend(
            ReplacementSample(
                "query", f"query-{query_class}-{ordinal}", True, ordinal, query_class
            )
            for ordinal in range(20)
        )
    samples.extend(
        ReplacementSample("update", f"update-{ordinal}", True, ordinal)
        for ordinal in range(5)
    )
    samples.append(ReplacementSample("index-determinism", "index-determinism"))
    return _validate_schedule(tuple(samples))


def replacement_stage_b_schedule() -> tuple[ReplacementSample, ...]:
    samples = [ReplacementSample("product", "product-metadata")]
    samples.extend(
        (
            ReplacementSample("build", "rebuild-cold-0", True, 0),
            ReplacementSample("build", "rebuild-warmup-0", False, 0),
            *(ReplacementSample("build", f"rebuild-warm-{ordinal}", True, ordinal) for ordinal in range(1, 6)),
            ReplacementSample("build", "rebuild-after-mutation-1", True, 1),
        )
    )
    for vector in range(1, 25):
        if vector in {3, 4, 5, 6}:
            continue
        query_class = "fuzzy" if vector in {9, 15} else "exact"
        vector_identity = f"L1-Q-{vector:03d}"
        samples.extend(
            ReplacementSample(
                "query",
                f"query-{vector_identity.lower()}-{ordinal}",
                ordinal > 0,
                ordinal,
                query_class,
            )
            for ordinal in range(6)
        )
    samples.extend(
        ReplacementSample("update", f"update-{ordinal}", ordinal > 0, ordinal)
        for ordinal in range(6)
    )
    samples.append(ReplacementSample("index-determinism", "index-determinism"))
    return _validate_schedule(tuple(samples))


def run_replacement_schedule(
    *,
    run_identity: str,
    environment_identity: str,
    policy_identity: str,
    candidate_identity: str,
    candidate_digest: str,
    corpus_identity: str,
    evidence_root: Path,
    schedule: tuple[ReplacementSample, ...],
    expected_result_identities: Mapping[str, tuple[str, ...]],
    relevant_source_bytes: int,
    model_output_budget_characters: int,
    declared_changed_files: Mapping[str, int],
    execute: Callable[[ReplacementSample], Mapping[str, object]],
) -> ReplacementEvidence:
    """Run one frozen schedule and atomically install controller-owned leaves."""
    for value in (run_identity, environment_identity, candidate_identity):
        if type(value) is not str or not _IDENTITY.fullmatch(value):
            raise ValueError("invalid replacement identity")
    for value in (policy_identity, candidate_digest, corpus_identity):
        if type(value) is not str or not _DIGEST.fullmatch(value):
            raise ValueError("invalid replacement digest")
    if not isinstance(evidence_root, Path) or evidence_root.exists():
        raise ValueError("evidence root already exists or is invalid")
    if type(relevant_source_bytes) is not int or relevant_source_bytes < 1:
        raise ValueError("invalid relevant-source size")
    if model_output_budget_characters not in {2000, 4000, 8000, 12000}:
        raise ValueError("invalid model-output budget")
    frozen_schedule = _validate_schedule(schedule)
    query_ids = {sample.sample_identity for sample in frozen_schedule if sample.record_type == "query"}
    if set(expected_result_identities) != query_ids:
        raise ValueError("invalid expected-result map")
    normalized_expected: dict[str, tuple[str, ...]] = {}
    for sample_identity, identities in expected_result_identities.items():
        if type(identities) is not tuple or not identities:
            raise ValueError("invalid expected-result map")
        if identities != tuple(sorted(set(identities))) or any(not _DIGEST.fullmatch(item) for item in identities):
            raise ValueError("invalid expected-result map")
        normalized_expected[sample_identity] = identities
    update_ids = {
        sample.sample_identity for sample in frozen_schedule
        if sample.record_type == "update"
    }
    if set(declared_changed_files) != update_ids or any(
        type(value) is not int or value < 1
        for value in declared_changed_files.values()
    ):
        raise ValueError("invalid declared-changed-files map")
    header = {
        "record_type": "run",
        "schema_version": "1",
        "evidence_version": "replacement-v2",
        "run_identity": run_identity,
        "environment_identity": environment_identity,
        "policy_identity": policy_identity,
        "candidate_identity": candidate_identity,
        "candidate_digest": candidate_digest,
        "corpus_identity": corpus_identity,
    }
    common = {
        "environment_identity": environment_identity,
        "policy_identity": policy_identity,
        "candidate_identity": candidate_identity,
        "candidate_digest": candidate_digest,
    }
    records: list[Mapping[str, object]] = [header]
    active: Optional[ReplacementSample] = None
    try:
        for sample in frozen_schedule:
            active = sample
            observed = execute(sample)
            if type(observed) is not dict:
                raise ValueError("candidate observation is not a dictionary")
            if set(observed) & _CONTROLLER_FIELDS:
                raise ValueError("candidate attempted to set controller-owned fields")
            if set(observed) != _OBSERVED_FIELDS[sample.record_type]:
                raise ValueError("invalid candidate observation fields")
            controller = sample.controller_fields(
                normalized_expected.get(sample.sample_identity, ()),
                relevant_source_bytes=relevant_source_bytes,
                model_output_budget_characters=model_output_budget_characters,
                declared_changed_files=declared_changed_files.get(
                    sample.sample_identity, 0
                ),
            )
            records.append({**common, **controller, **observed})
    except Exception as error:
        evidence_root.mkdir(parents=True, exist_ok=False)
        _install_new_file(
            evidence_root / "partial-evidence.jsonl",
            b"".join(_canonical_json(item) + b"\n" for item in records),
        )
        failure = {
            "schema_version": "1",
            "evidence_version": "replacement-v2",
            "run_identity": run_identity,
            "candidate_identity": candidate_identity,
            "failed_sample_identity": active.sample_identity if active else "schedule-start",
            "exception_type": type(error).__name__,
        }
        _install_new_file(
            evidence_root / "failure.json", _canonical_json(failure) + b"\n"
        )
        raise
    evidence_root.mkdir(parents=True, exist_ok=False)
    output = evidence_root / "evidence.jsonl"
    _install_new_file(
        output, b"".join(_canonical_json(item) + b"\n" for item in records)
    )
    reopened = ReplacementEvidence.from_jsonl(output)
    actual_ids = {
        str(item["sample_identity"])
        for item in (
            (reopened.product_sample,)
            + reopened.build_samples
            + reopened.query_samples
            + reopened.update_samples
            + (reopened.determinism_sample,)
        )
    }
    if actual_ids != {sample.sample_identity for sample in frozen_schedule}:
        raise ValueError("installed replacement schedule mismatch")
    return reopened


def _validate_schedule(
    schedule: tuple[ReplacementSample, ...],
) -> tuple[ReplacementSample, ...]:
    if type(schedule) is not tuple or not schedule or any(
        not isinstance(sample, ReplacementSample) for sample in schedule
    ):
        raise ValueError("invalid replacement schedule")
    identities = tuple(sample.sample_identity for sample in schedule)
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate replacement sample")
    if sum(sample.record_type == "product" for sample in schedule) != 1:
        raise ValueError("invalid replacement product schedule")
    if sum(sample.record_type == "index-determinism" for sample in schedule) != 1:
        raise ValueError("invalid replacement determinism schedule")
    if {sample.query_class for sample in schedule if sample.record_type == "query"} != {"exact", "fuzzy"}:
        raise ValueError("invalid replacement query schedule")
    if not any(sample.record_type == "build" for sample in schedule) or not any(
        sample.record_type == "update" for sample in schedule
    ):
        raise ValueError("invalid replacement lifecycle schedule")
    return schedule


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _install_new_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".replacement-", dir=path.parent, delete=False
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_path, path)
        except FileExistsError as error:
            raise ValueError("evidence file already exists") from error
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
