"""Controller-owned replacement-v2 evidence validation and selection.

This module intentionally lives in the public evaluation harness rather than
the runtime package. It cannot index repositories or execute candidates.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Optional


_MAX_EVIDENCE_BYTES = 64 * 1024 * 1024
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_IDENTITY = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_COMMON = {
    "record_type",
    "sample_identity",
    "environment_identity",
    "policy_identity",
    "candidate_identity",
    "candidate_digest",
}
_FIELDS = {
    "run": {
        "record_type", "schema_version", "evidence_version", "run_identity",
        "environment_identity", "policy_identity", "candidate_identity",
        "candidate_digest", "corpus_identity",
    },
    "product": _COMMON | {
        "artifact_size_bytes", "cross_platform_targets",
        "dependency_license_checks_passed", "dependency_license_checks_total",
        "maintenance_checks_passed", "maintenance_checks_total",
        "unsafe_code_blocks",
    },
    "build": _COMMON | {
        "retained", "ordinal", "elapsed_ns", "peak_rss_bytes",
        "index_and_staging_bytes", "relevant_source_bytes", "index_digest",
        "repository_writes", "network_attempts", "undeclared_child_processes",
        "state_escapes",
    },
    "query": _COMMON | {
        "query_class", "retained", "ordinal", "elapsed_ns",
        "expected_result_identities", "actual_result_identities",
        "citation_match", "evidence_class_match", "freshness_honest",
        "forbidden_result_count", "response_characters", "budget_characters",
        "repository_files_opened", "repository_bytes_read", "considered_records",
        "full_repository_operations", "repository_writes", "network_attempts",
        "undeclared_child_processes", "state_escapes",
    },
    "update": _COMMON | {
        "retained", "ordinal", "elapsed_ns", "declared_changed_files",
        "enumerated_repository_files", "parsed_repository_files",
        "incremental_digest", "rebuild_digest", "repository_writes",
        "network_attempts", "undeclared_child_processes", "state_escapes",
    },
    "index-determinism": _COMMON | {
        "first_index_digest", "second_index_digest",
    },
}
_WEIGHTS = MappingProxyType(
    {
        "artifact-size": 5.0,
        "build-latency": 5.0,
        "dependency-license-surface": 5.0,
        "incremental-locality": 15.0,
        "maintenance": 10.0,
        "memory": 10.0,
        "packaging": 10.0,
        "query-latency": 10.0,
        "retrieval-margin": 20.0,
        "storage-ratio": 10.0,
    }
)
_LOWER_IS_BETTER = {
    "artifact-size", "build-latency", "incremental-locality", "memory",
    "query-latency", "storage-ratio",
}
_FIXED_DIMENSIONS = {"dependency-license-surface", "maintenance", "packaging"}


class ReplacementGateStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"


@dataclass(frozen=True)
class ReplacementEvidence:
    schema_version: str
    evidence_version: str
    run_identity: str
    environment_identity: str
    policy_identity: str
    candidate_identity: str
    candidate_digest: str
    corpus_identity: str
    product_sample: Mapping[str, object]
    build_samples: tuple[Mapping[str, object], ...]
    query_samples: tuple[Mapping[str, object], ...]
    update_samples: tuple[Mapping[str, object], ...]
    determinism_sample: Mapping[str, object]

    @classmethod
    def from_jsonl(cls, path: Path) -> "ReplacementEvidence":
        if not isinstance(path, Path) or not path.is_file():
            raise ValueError("invalid evidence file")
        if path.stat().st_size < 1 or path.stat().st_size > _MAX_EVIDENCE_BYTES:
            raise ValueError("invalid evidence size")
        records = tuple(_load_line(line) for line in path.read_bytes().splitlines())
        header = records[0]
        if header.get("record_type") != "run" or set(header) != _FIELDS["run"]:
            raise ValueError("invalid run header")
        if header["schema_version"] != "1" or header["evidence_version"] != "replacement-v2":
            raise ValueError("invalid evidence version")
        run_identity = _identity(header, "run_identity")
        environment = _identity(header, "environment_identity")
        policy = _digest(header, "policy_identity")
        candidate = _identity(header, "candidate_identity")
        candidate_digest = _digest(header, "candidate_digest")
        corpus = _digest(header, "corpus_identity")
        common = {
            "environment_identity": environment,
            "policy_identity": policy,
            "candidate_identity": candidate,
            "candidate_digest": candidate_digest,
        }
        grouped: dict[str, list[Mapping[str, object]]] = {
            name: [] for name in _FIELDS if name != "run"
        }
        sample_ids: set[str] = set()
        for record in records[1:]:
            record_type = record.get("record_type")
            if type(record_type) is not str or record_type not in grouped:
                raise ValueError("unknown evidence record")
            if set(record) != _FIELDS[record_type]:
                raise ValueError("malformed evidence record")
            sample_identity = _identity(record, "sample_identity")
            if sample_identity in sample_ids:
                raise ValueError("duplicate sample identity")
            sample_ids.add(sample_identity)
            for field, expected in common.items():
                if record[field] != expected:
                    raise ValueError(f"mixed {field}")
            _validate_leaf(record_type, record)
            grouped[record_type].append(MappingProxyType(dict(record)))
        if len(grouped["product"]) != 1 or len(grouped["index-determinism"]) != 1:
            raise ValueError("incomplete singleton evidence")
        if not grouped["build"] or not grouped["update"]:
            raise ValueError("incomplete lifecycle evidence")
        query_classes = {sample["query_class"] for sample in grouped["query"]}
        if query_classes != {"exact", "fuzzy"}:
            raise ValueError("incomplete query evidence")
        return cls(
            "1", "replacement-v2", run_identity, environment, policy,
            candidate, candidate_digest, corpus, grouped["product"][0],
            tuple(grouped["build"]), tuple(grouped["query"]),
            tuple(grouped["update"]), grouped["index-determinism"][0],
        )


@dataclass(frozen=True)
class ReplacementGateReport:
    candidate_identity: str
    support_status: ReplacementGateStatus
    gate_statuses: Mapping[str, ReplacementGateStatus]
    failure_reason_codes: tuple[str, ...]
    exact_top_five_recall: float
    fuzzy_top_ten_recall: float
    warm_query_p95_ns: int
    incremental_update_p95_ns: int
    initial_build_ns: int
    peak_rss_bytes: int
    storage_ratio: float
    query_repository_files_opened_p95: int
    query_repository_bytes_read_p95: int
    query_considered_records_p95: int


@dataclass(frozen=True)
class ReplacementCandidateScore:
    candidate_identity: str
    eligible: bool
    dimension_raw_values: Mapping[str, float]
    dimension_points: Mapping[str, float]
    total_points: float
    tie_break_values: tuple[object, ...]


@dataclass(frozen=True)
class ReplacementDecision:
    schema_version: str
    evidence_version: str
    run_identity: str
    status: str
    recommended_candidate: Optional[str]
    candidate_reports: tuple[ReplacementGateReport, ...]
    candidate_scores: tuple[ReplacementCandidateScore, ...]
    reason_codes: tuple[str, ...]
    next_authorized_scope: str


def evaluate_replacement_gates(evidence: ReplacementEvidence) -> ReplacementGateReport:
    if not isinstance(evidence, ReplacementEvidence):
        raise ValueError("invalid replacement evidence")
    queries = tuple(sample for sample in evidence.query_samples if sample["retained"])
    builds = tuple(sample for sample in evidence.build_samples if sample["retained"])
    updates = tuple(sample for sample in evidence.update_samples if sample["retained"])
    if not queries or not builds or not updates:
        raise ValueError("no retained replacement evidence")
    exact = tuple(sample for sample in queries if sample["query_class"] == "exact")
    fuzzy = tuple(sample for sample in queries if sample["query_class"] == "fuzzy")
    exact_recall = _recall(exact)
    fuzzy_recall = _recall(fuzzy)
    warm_p95 = _percentile(tuple(int(item["elapsed_ns"]) for item in queries), 95)
    update_p95 = _percentile(tuple(int(item["elapsed_ns"]) for item in updates), 95)
    files_p95 = _percentile(tuple(int(item["repository_files_opened"]) for item in queries), 95)
    bytes_p95 = _percentile(tuple(int(item["repository_bytes_read"]) for item in queries), 95)
    records_p95 = _percentile(tuple(int(item["considered_records"]) for item in queries), 95)
    initial_build = max(int(item["elapsed_ns"]) for item in builds)
    peak_rss = max(int(item["peak_rss_bytes"]) for item in builds)
    storage_ratio = max(
        int(item["index_and_staging_bytes"]) / int(item["relevant_source_bytes"])
        for item in builds
    )
    gates = {
        "retrieval": exact_recall == 1.0 and fuzzy_recall == 1.0,
        "citations": all(bool(item["citation_match"]) for item in queries),
        "evidence": all(bool(item["evidence_class_match"]) for item in queries),
        "freshness": all(bool(item["freshness_honest"]) for item in queries),
        "forbidden-results": all(int(item["forbidden_result_count"]) == 0 for item in queries),
        "model-budget": all(int(item["response_characters"]) <= int(item["budget_characters"]) for item in queries),
        "query-latency": warm_p95 <= 150_000_000,
        "query-files": files_p95 <= 32,
        "query-bytes": bytes_p95 <= 4 * 1024 * 1024,
        "query-records": records_p95 <= 4096,
        "full-repository-work": all(int(item["full_repository_operations"]) == 0 for item in queries),
        "build": initial_build <= 10_000_000_000,
        "memory": peak_rss <= 512 * 1024 * 1024,
        "storage": storage_ratio <= 1.5,
        "incremental": update_p95 <= 2_000_000_000,
        "update-locality": all(
            int(item["enumerated_repository_files"]) <= int(item["declared_changed_files"])
            and int(item["parsed_repository_files"]) <= int(item["declared_changed_files"])
            for item in updates
        ),
        "incremental-equivalence": all(item["incremental_digest"] == item["rebuild_digest"] for item in updates),
        "index-determinism": evidence.determinism_sample["first_index_digest"] == evidence.determinism_sample["second_index_digest"],
        "repository-read-only": _all_zero(evidence, "repository_writes"),
        "network-offline": _all_zero(evidence, "network_attempts"),
        "child-processes": _all_zero(evidence, "undeclared_child_processes"),
        "state-boundary": _all_zero(evidence, "state_escapes"),
    }
    statuses = MappingProxyType(
        {
            name: ReplacementGateStatus.PASS if passed else ReplacementGateStatus.FAIL
            for name, passed in sorted(gates.items())
        }
    )
    failures = tuple(f"gate-{name}" for name, status in statuses.items() if status is ReplacementGateStatus.FAIL)
    return ReplacementGateReport(
        evidence.candidate_identity, ReplacementGateStatus.PASS, statuses,
        failures, exact_recall, fuzzy_recall, warm_p95, update_p95,
        initial_build, peak_rss, storage_ratio, files_p95, bytes_p95, records_p95,
    )


def score_replacement_candidates(
    evidence: tuple[ReplacementEvidence, ...],
) -> tuple[ReplacementCandidateScore, ...]:
    if not evidence or len({item.candidate_identity for item in evidence}) != len(evidence):
        raise ValueError("invalid candidate population")
    ordered = tuple(sorted(evidence, key=lambda item: item.candidate_identity))
    comparison_identities = {
        (
            item.evidence_version,
            item.run_identity,
            item.environment_identity,
            item.policy_identity,
            item.corpus_identity,
        )
        for item in ordered
    }
    if len(comparison_identities) != 1:
        raise ValueError("mixed comparison population")
    reports = {item.candidate_identity: evaluate_replacement_gates(item) for item in ordered}
    eligible_population = tuple(
        item for item in ordered if not reports[item.candidate_identity].failure_reason_codes
    )
    scores: list[ReplacementCandidateScore] = []
    for item in ordered:
        report = reports[item.candidate_identity]
        eligible = not report.failure_reason_codes
        raw = _raw_dimensions(item, report)
        points: dict[str, float] = {}
        for dimension, weight in _WEIGHTS.items():
            if not eligible:
                points[dimension] = 0.0
                continue
            if dimension in _FIXED_DIMENSIONS:
                points[dimension] = weight * raw[dimension]
                continue
            values = [
                _raw_dimensions(candidate, reports[candidate.candidate_identity])[dimension]
                for candidate in eligible_population
            ]
            minimum, maximum = min(values), max(values)
            if minimum == maximum:
                normalized = 1.0
            elif dimension in _LOWER_IS_BETTER:
                normalized = (maximum - raw[dimension]) / (maximum - minimum)
            else:
                normalized = (raw[dimension] - minimum) / (maximum - minimum)
            points[dimension] = weight * normalized
        bounded_work = (
            report.query_repository_files_opened_p95,
            report.query_repository_bytes_read_p95,
            report.query_considered_records_p95,
        )
        product = item.product_sample
        scores.append(
            ReplacementCandidateScore(
                item.candidate_identity,
                eligible,
                MappingProxyType(dict(sorted(raw.items()))),
                MappingProxyType(dict(sorted(points.items()))),
                round(sum(points.values()), 6),
                (
                    report.storage_ratio,
                    bounded_work,
                    -int(product["cross_platform_targets"]),
                    (
                        int(product["unsafe_code_blocks"]),
                        -int(product["dependency_license_checks_passed"]),
                    ),
                    -int(product["maintenance_checks_passed"]),
                    item.candidate_identity,
                ),
            )
        )
    return tuple(scores)


def decide_replacement_bakeoff(
    reports: tuple[ReplacementGateReport, ...],
    scores: tuple[ReplacementCandidateScore, ...],
) -> ReplacementDecision:
    ordered_reports = tuple(sorted(reports, key=lambda item: item.candidate_identity))
    ordered_scores = tuple(sorted(scores, key=lambda item: item.candidate_identity))
    report_ids = tuple(item.candidate_identity for item in ordered_reports)
    score_ids = tuple(item.candidate_identity for item in ordered_scores)
    if not ordered_reports or report_ids != score_ids or len(set(report_ids)) != len(report_ids):
        raise ValueError("candidate report/score identity mismatch")
    complete = tuple(item for item in ordered_reports if item.support_status is ReplacementGateStatus.PASS)
    eligible_ids = {
        item.candidate_identity for item in complete if not item.failure_reason_codes
    }
    if len(complete) < 2:
        status, recommended, reasons = "NO-GO", None, ("insufficient-complete-finalists",)
    elif not eligible_ids:
        status, recommended, reasons = "NO-GO", None, ("no-eligible-candidate",)
    else:
        eligible_scores = tuple(item for item in ordered_scores if item.candidate_identity in eligible_ids)
        best = max(item.total_points for item in eligible_scores)
        close = tuple(item for item in eligible_scores if best - item.total_points <= 5.0)
        recommended = min(close, key=lambda item: item.tie_break_values).candidate_identity
        status, reasons = "GO", ()
    material = "\0".join(report_ids).encode("utf-8")
    return ReplacementDecision(
        "1", "replacement-v2", "sha256:" + hashlib.sha256(material).hexdigest(),
        status, recommended, ordered_reports, ordered_scores, reasons,
        "production-level1-design" if status == "GO" else "replacement-bakeoff-remediation",
    )


def _validate_leaf(record_type: str, record: Mapping[str, object]) -> None:
    if record_type == "product":
        for field in (
            "artifact_size_bytes", "cross_platform_targets",
            "dependency_license_checks_passed", "dependency_license_checks_total",
            "maintenance_checks_passed", "maintenance_checks_total", "unsafe_code_blocks",
        ):
            _integer(record, field)
        for prefix in ("dependency_license", "maintenance"):
            passed = int(record[f"{prefix}_checks_passed"])
            total = int(record[f"{prefix}_checks_total"])
            if total < 1 or passed > total:
                raise ValueError("invalid product checklist")
        if int(record["cross_platform_targets"]) > 5:
            raise ValueError("invalid platform target count")
        return
    if record_type in {"build", "query", "update"}:
        _boolean(record, "retained")
        _integer(record, "ordinal")
        _integer(record, "elapsed_ns")
        for field in (
            "repository_writes", "network_attempts",
            "undeclared_child_processes", "state_escapes",
        ):
            _integer(record, field)
    if record_type == "build":
        for field in ("peak_rss_bytes", "index_and_staging_bytes", "relevant_source_bytes"):
            _integer(record, field)
        if int(record["relevant_source_bytes"]) < 1:
            raise ValueError("invalid relevant source size")
        _digest(record, "index_digest")
    elif record_type == "query":
        if record["query_class"] not in {"exact", "fuzzy"}:
            raise ValueError("invalid query class")
        for field in (
            "forbidden_result_count", "response_characters", "budget_characters",
            "repository_files_opened", "repository_bytes_read", "considered_records",
            "full_repository_operations",
        ):
            _integer(record, field)
        for field in ("citation_match", "evidence_class_match", "freshness_honest"):
            _boolean(record, field)
        expected = _digests(record, "expected_result_identities")
        actual = _digests(record, "actual_result_identities")
        if len(actual) > (5 if record["query_class"] == "exact" else 10):
            raise ValueError("unbounded query result identities")
        if not expected:
            raise ValueError("missing expected result identity")
    elif record_type == "update":
        for field in ("declared_changed_files", "enumerated_repository_files", "parsed_repository_files"):
            _integer(record, field)
        if int(record["declared_changed_files"]) < 1:
            raise ValueError("invalid changed-file count")
        _digest(record, "incremental_digest")
        _digest(record, "rebuild_digest")
    elif record_type == "index-determinism":
        _digest(record, "first_index_digest")
        _digest(record, "second_index_digest")


def _raw_dimensions(
    evidence: ReplacementEvidence,
    report: ReplacementGateReport,
) -> dict[str, float]:
    product = evidence.product_sample
    dependency_total = int(product["dependency_license_checks_total"])
    maintenance_total = int(product["maintenance_checks_total"])
    return {
        "artifact-size": float(product["artifact_size_bytes"]),
        "build-latency": float(report.initial_build_ns),
        "dependency-license-surface": int(product["dependency_license_checks_passed"]) / dependency_total,
        "incremental-locality": float(report.incremental_update_p95_ns),
        "maintenance": int(product["maintenance_checks_passed"]) / maintenance_total,
        "memory": float(report.peak_rss_bytes),
        "packaging": int(product["cross_platform_targets"]) / 5,
        "query-latency": float(report.warm_query_p95_ns),
        "retrieval-margin": (report.exact_top_five_recall + report.fuzzy_top_ten_recall) / 2,
        "storage-ratio": report.storage_ratio,
    }


def _all_zero(evidence: ReplacementEvidence, field: str) -> bool:
    samples = evidence.build_samples + evidence.query_samples + evidence.update_samples
    return all(int(item[field]) == 0 for item in samples)


def _recall(samples: tuple[Mapping[str, object], ...]) -> float:
    expected_count = 0
    found_count = 0
    for sample in samples:
        expected = set(sample["expected_result_identities"])
        actual = set(sample["actual_result_identities"])
        expected_count += len(expected)
        found_count += len(expected & actual)
    return 1.0 if expected_count == 0 else found_count / expected_count


def _percentile(values: tuple[int, ...], percentile: int) -> int:
    if not values or not 1 <= percentile <= 100:
        raise ValueError("invalid percentile")
    ordered = sorted(values)
    return ordered[max(1, math.ceil(percentile * len(ordered) / 100)) - 1]


def _load_line(raw: bytes) -> dict[str, object]:
    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("nonfinite value")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid JSONL leaf") from error
    if type(value) is not dict:
        raise ValueError("invalid JSONL leaf")
    return value


def _identity(value: Mapping[str, object], field: str) -> str:
    item = value.get(field)
    if type(item) is not str or not _IDENTITY.fullmatch(item):
        raise ValueError(f"invalid {field}")
    return item


def _digest(value: Mapping[str, object], field: str) -> str:
    item = value.get(field)
    if type(item) is not str or not _DIGEST.fullmatch(item):
        raise ValueError(f"invalid {field}")
    return item


def _integer(value: Mapping[str, object], field: str) -> int:
    item = value.get(field)
    if type(item) is not int or item < 0:
        raise ValueError(f"invalid {field}")
    return item


def _boolean(value: Mapping[str, object], field: str) -> bool:
    item = value.get(field)
    if type(item) is not bool:
        raise ValueError(f"invalid {field}")
    return item


def _digests(value: Mapping[str, object], field: str) -> tuple[str, ...]:
    raw = value.get(field)
    if type(raw) is not list:
        raise ValueError(f"invalid {field}")
    result = tuple(_digest({field: item}, field) for item in raw)
    if result != tuple(sorted(set(result))):
        raise ValueError(f"invalid {field}")
    return result
