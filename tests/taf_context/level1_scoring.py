"""Controller-owned Level 1 evidence validation, gates, and scoring."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Optional


_MAX_EVIDENCE_BYTES = 64 * 1024 * 1024
_SHA256_LENGTH = 71
_QUERY_VECTORS = tuple(f"L1-Q-{ordinal:03d}" for ordinal in range(1, 25))
_STATE_VECTORS = ("L1-S-CORRUPT", "L1-S-MOVED", "L1-S-WORKTREE")
_COMMON_FIELDS = {"record_type", "sample_identity", "environment_identity", "candidate_identity", "candidate_digest"}
_RECORD_FIELDS = {
    "run": {"record_type", "schema_version", "run_identity", "environment_identity", "candidate_identity", "candidate_digest", "corpus_identity"},
    "process": _COMMON_FIELDS | {
        "phase", "retained", "ordinal", "elapsed_ns", "peak_rss_bytes",
        "stdout_bytes", "stderr_bytes", "exit_code", "escape_count",
        "runtime_artifacts", "single_self_contained_artifact",
        "cross_platform_targets", "no_system_dependency",
        "lock_license_complete", "startup_simple",
        "security_checks_passed", "security_checks_total",
        "maintenance_checks_passed", "maintenance_checks_total",
    },
    "query": _COMMON_FIELDS | {"vector_identity", "query_class", "retained", "ordinal", "elapsed_ns", "expected_result_identities", "actual_result_identities", "citation_match", "evidence_class_match", "freshness_honest", "forbidden_result_count", "response_characters", "budget_characters", "escape_count", "repository_hash_match", "leak_detected", "result_digest"},
    "update": _COMMON_FIELDS | {"retained", "ordinal", "elapsed_ns", "incremental_digest", "rebuild_digest"},
    "rebuild": _COMMON_FIELDS | {"phase", "retained", "ordinal", "elapsed_ns", "peak_rss_bytes", "index_and_staging_bytes", "relevant_source_bytes"},
    "determinism": _COMMON_FIELDS | {"vector_identity", "permuted", "result_digest"},
    "repository": _COMMON_FIELDS | {"before_hash", "after_hash"},
}


class GateStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class CandidateEvidence:
    schema_version: str
    run_identity: str
    environment_identity: str
    candidate_identity: str
    candidate_digest: str
    corpus_identity: str
    process_samples: tuple[Mapping[str, object], ...]
    query_samples: tuple[Mapping[str, object], ...]
    update_samples: tuple[Mapping[str, object], ...]
    rebuild_samples: tuple[Mapping[str, object], ...]
    determinism_digests: tuple[Mapping[str, object], ...]
    repository_hashes: tuple[Mapping[str, object], ...]

    @classmethod
    def from_jsonl(cls, path: Path) -> "CandidateEvidence":
        if not isinstance(path, Path) or not path.is_file() or path.stat().st_size > _MAX_EVIDENCE_BYTES:
            raise ValueError("invalid evidence file")
        raw_lines = path.read_bytes().splitlines()
        if not raw_lines:
            raise ValueError("empty evidence")
        records = [_load_line(line) for line in raw_lines]
        header = records[0]
        if header.get("record_type") != "run" or set(header) != _RECORD_FIELDS["run"]:
            raise ValueError("invalid run header")
        if header["schema_version"] != "1":
            raise ValueError("invalid schema version")
        _text(header, "run_identity")
        environment = _text(header, "environment_identity")
        candidate = _text(header, "candidate_identity")
        candidate_digest = _digest(header, "candidate_digest")
        corpus_identity = _digest(header, "corpus_identity")

        grouped: dict[str, list[Mapping[str, object]]] = {name: [] for name in _RECORD_FIELDS if name != "run"}
        sample_ids: set[str] = set()
        for record in records[1:]:
            record_type = record.get("record_type")
            if type(record_type) is not str or record_type not in grouped or set(record) != _RECORD_FIELDS[record_type]:
                raise ValueError("unknown or malformed leaf")
            sample_id = _text(record, "sample_identity")
            if sample_id in sample_ids:
                raise ValueError("duplicate sample identity")
            sample_ids.add(sample_id)
            if _text(record, "environment_identity") != environment:
                raise ValueError("mixed environment identity")
            if _text(record, "candidate_identity") != candidate:
                raise ValueError("mixed candidate identity")
            if _digest(record, "candidate_digest") != candidate_digest:
                raise ValueError("mixed candidate digest")
            _validate_leaf(record_type, record)
            grouped[record_type].append(MappingProxyType(dict(record)))
        _validate_schedule(grouped)
        return cls(
            "1",
            str(header["run_identity"]),
            environment,
            candidate,
            candidate_digest,
            corpus_identity,
            tuple(grouped["process"]),
            tuple(grouped["query"]),
            tuple(grouped["update"]),
            tuple(grouped["rebuild"]),
            tuple(grouped["determinism"]),
            tuple(grouped["repository"]),
        )


@dataclass(frozen=True)
class GateReport:
    candidate_identity: str
    support_status: GateStatus
    gate_statuses: Mapping[str, GateStatus]
    failure_reason_codes: tuple[str, ...]
    exact_top_five_recall: float
    fuzzy_top_ten_recall: float
    warm_query_p95_ns: int
    incremental_update_p95_ns: int
    initial_build_ns: int
    peak_rss_bytes: int
    index_and_staging_bytes: int
    relevant_source_bytes: int
    response_budget_overruns: int


@dataclass(frozen=True)
class CandidateScore:
    candidate_identity: str
    eligible: bool
    dimension_raw_values: Mapping[str, float]
    dimension_points: Mapping[str, float]
    total_points: float
    tie_break_values: tuple[object, ...]


@dataclass(frozen=True)
class BakeoffDecision:
    schema_version: str
    run_identity: str
    status: str
    recommended_candidate: Optional[str]
    candidate_reports: tuple[GateReport, ...]
    candidate_scores: tuple[CandidateScore, ...]
    pareto_dominated_candidates: tuple[str, ...]
    reason_codes: tuple[str, ...]
    provisional: bool
    next_authorized_scope: str


def percentile_nearest_rank(values: tuple[int, ...], percentile: int) -> int:
    if not values or type(percentile) is not int or not 1 <= percentile <= 100 or any(type(value) is not int or value < 0 for value in values):
        raise ValueError("invalid percentile input")
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered) / 100))
    return ordered[rank - 1]


def evaluate_gates(evidence: CandidateEvidence) -> GateReport:
    retained_queries = tuple(sample for sample in evidence.query_samples if sample["retained"])
    exact = tuple(sample for sample in retained_queries if sample["query_class"] == "exact")
    fuzzy = tuple(sample for sample in retained_queries if sample["query_class"] == "fuzzy")
    exact_recall = _recall(exact)
    fuzzy_recall = _recall(fuzzy)
    warm_p95 = percentile_nearest_rank(
        tuple(
            int(sample["elapsed_ns"])
            for sample in retained_queries
            if sample["query_class"] != "state"
        ),
        95,
    )
    retained_updates = tuple(sample for sample in evidence.update_samples if sample["retained"])
    update_p95 = percentile_nearest_rank(tuple(int(sample["elapsed_ns"]) for sample in retained_updates), 95)
    cold = next(sample for sample in evidence.rebuild_samples if sample["phase"] == "cold")
    initial_build = int(cold["elapsed_ns"])
    peak_rss = max(int(sample["peak_rss_bytes"]) for sample in evidence.rebuild_samples)
    index_bytes = max(int(sample["index_and_staging_bytes"]) for sample in evidence.rebuild_samples)
    source_bytes = max(int(sample["relevant_source_bytes"]) for sample in evidence.rebuild_samples)
    overruns = sum(int(sample["response_characters"]) > int(sample["budget_characters"]) for sample in retained_queries)
    correctness = exact_recall == 1.0 and fuzzy_recall >= 0.95 and all(
        bool(sample["citation_match"])
        and bool(sample["evidence_class_match"])
        and bool(sample["freshness_honest"])
        and int(sample["forbidden_result_count"]) == 0
        for sample in retained_queries
    )
    update_correctness = all(sample["incremental_digest"] == sample["rebuild_digest"] for sample in retained_updates)
    deterministic = _determinism_passes(evidence.determinism_digests)
    safety = all(
        int(sample["escape_count"]) == 0
        and bool(sample["repository_hash_match"])
        and not bool(sample["leak_detected"])
        for sample in retained_queries
    ) and all(sample["before_hash"] == sample["after_hash"] for sample in evidence.repository_hashes)
    gates = {
        "correctness": GateStatus.PASS if correctness else GateStatus.FAIL,
        "determinism": GateStatus.PASS if deterministic else GateStatus.FAIL,
        "incremental": GateStatus.PASS if update_correctness and update_p95 <= 2_000_000_000 else GateStatus.FAIL,
        "initial-build": GateStatus.PASS if initial_build <= 60_000_000_000 else GateStatus.FAIL,
        "memory": GateStatus.PASS if peak_rss <= 1024 * 1024 * 1024 else GateStatus.FAIL,
        "model-budget": GateStatus.PASS if overruns == 0 else GateStatus.FAIL,
        "query-latency": GateStatus.PASS if warm_p95 <= 250_000_000 else GateStatus.FAIL,
        "safety": GateStatus.PASS if safety else GateStatus.FAIL,
        "storage": GateStatus.PASS if index_bytes * 2 <= source_bytes * 3 else GateStatus.FAIL,
    }
    failures = tuple(sorted(f"gate-{name}" for name, status in gates.items() if status is GateStatus.FAIL))
    return GateReport(
        evidence.candidate_identity,
        GateStatus.PASS,
        MappingProxyType(dict(sorted(gates.items()))),
        failures,
        exact_recall,
        fuzzy_recall,
        warm_p95,
        update_p95,
        initial_build,
        peak_rss,
        index_bytes,
        source_bytes,
        overruns,
    )


def score_candidate(evidence: CandidateEvidence, eligible_population: tuple[CandidateEvidence, ...]) -> CandidateScore:
    reports = {item.candidate_identity: evaluate_gates(item) for item in eligible_population}
    report = reports[evidence.candidate_identity]
    eligible = not report.failure_reason_codes
    normalization_population = tuple(
        item for item in eligible_population
        if not reports[item.candidate_identity].failure_reason_codes
        and reports[item.candidate_identity].support_status is GateStatus.PASS
    ) or eligible_population
    metadata = _product_metadata(evidence)
    packaging_points = (
        (4 if metadata["single_self_contained_artifact"] else 0)
        + (2 if int(metadata["cross_platform_targets"]) >= 2 else 0)
        + (2 if metadata["no_system_dependency"] else 0)
        + (1 if metadata["lock_license_complete"] else 0)
        + (1 if metadata["startup_simple"] else 0)
    )
    raw = {
        "retrieval": (report.exact_top_five_recall + report.fuzzy_top_ten_recall) / 2,
        "incremental": float(report.incremental_update_p95_ns),
        "query-latency": float(report.warm_query_p95_ns),
        "build": float(report.initial_build_ns),
        "memory-storage": float(report.peak_rss_bytes + report.index_and_staging_bytes),
        "packaging": packaging_points / 10,
        "security": int(metadata["security_checks_passed"]) / int(metadata["security_checks_total"]),
        "maintenance": int(metadata["maintenance_checks_passed"]) / int(metadata["maintenance_checks_total"]),
    }
    weights = {"retrieval": 25.0, "incremental": 20.0, "query-latency": 15.0, "build": 10.0, "memory-storage": 10.0, "packaging": 10.0, "security": 5.0, "maintenance": 5.0}
    lower = {"incremental", "query-latency", "build", "memory-storage"}
    fixed = {"packaging", "security", "maintenance"}
    points: dict[str, float] = {}
    for dimension, weight in weights.items():
        if dimension in fixed:
            points[dimension] = weight * raw[dimension] if eligible else 0.0
            continue
        values = []
        for item in normalization_population:
            item_report = reports[item.candidate_identity]
            item_raw = _raw_dimension_from_evidence(item, item_report, dimension)
            values.append(item_raw)
        minimum, maximum = min(values), max(values)
        value = raw[dimension]
        if minimum == maximum:
            normalized = 1.0
        elif dimension in lower:
            normalized = (maximum - value) / (maximum - minimum)
        else:
            normalized = (value - minimum) / (maximum - minimum)
        points[dimension] = weight * normalized if eligible else 0.0
    total = round(sum(points.values()), 6)
    return CandidateScore(
        evidence.candidate_identity,
        eligible,
        MappingProxyType(dict(sorted(raw.items()))),
        MappingProxyType(dict(sorted(points.items()))),
        total,
        (int(metadata["runtime_artifacts"]), report.warm_query_p95_ns, evidence.candidate_identity),
    )


def decide_bakeoff(
    candidate_reports: tuple[GateReport, ...],
    candidate_scores: tuple[CandidateScore, ...] = (),
) -> BakeoffDecision:
    reports = tuple(sorted(candidate_reports, key=lambda item: item.candidate_identity))
    eligible = tuple(report for report in reports if not report.failure_reason_codes and report.support_status is GateStatus.PASS)
    if candidate_scores:
        scores = tuple(sorted(candidate_scores, key=lambda item: item.candidate_identity))
        if tuple(score.candidate_identity for score in scores) != tuple(
            report.candidate_identity for report in reports
        ):
            raise ValueError("candidate score/report identity mismatch")
    else:
        scores = tuple(_score_from_report(report, reports) for report in reports)
    score_by_candidate = {score.candidate_identity: score for score in scores}
    if not eligible:
        status = "NO-GO"
        recommended = None
        reasons = ("no-eligible-candidate",)
    else:
        status = "GO"
        eligible_scores = tuple(
            score_by_candidate[report.candidate_identity] for report in eligible
        )
        best_points = max(score.total_points for score in eligible_scores)
        close_scores = tuple(
            score for score in eligible_scores
            if best_points - score.total_points <= 1.0
        )
        recommended = min(
            close_scores,
            key=lambda score: score.tie_break_values,
        ).candidate_identity
        reasons = ()
    dominated = tuple(sorted(_dominated_candidates(eligible)))
    run_material = "\0".join(report.candidate_identity for report in reports).encode("utf-8")
    return BakeoffDecision(
        "1",
        "sha256:" + hashlib.sha256(run_material).hexdigest(),
        status,
        recommended,
        reports,
        scores,
        dominated,
        reasons,
        False,
        "production-level1-design" if status == "GO" else "replacement-bakeoff-design",
    )


def _validate_leaf(record_type: str, record: Mapping[str, object]) -> None:
    if record_type in {"process", "query", "update", "rebuild"}:
        _integer(record, "ordinal")
        _integer(record, "elapsed_ns")
        _boolean(record, "retained")
    if record_type == "process":
        for field in (
            "peak_rss_bytes", "stdout_bytes", "stderr_bytes", "escape_count",
            "runtime_artifacts", "cross_platform_targets",
            "security_checks_passed", "security_checks_total",
            "maintenance_checks_passed", "maintenance_checks_total",
        ):
            _integer(record, field)
        for field in (
            "single_self_contained_artifact", "no_system_dependency",
            "lock_license_complete", "startup_simple",
        ):
            _boolean(record, field)
        if (
            int(record["security_checks_total"]) < 1
            or int(record["security_checks_passed"]) > int(record["security_checks_total"])
            or int(record["maintenance_checks_total"]) < 1
            or int(record["maintenance_checks_passed"]) > int(record["maintenance_checks_total"])
        ):
            raise ValueError("invalid binary checklist")
        if type(record["exit_code"]) is not int:
            raise ValueError("invalid exit code")
        _text(record, "phase")
    elif record_type == "query":
        if record["query_class"] not in {"exact", "fuzzy", "state"}:
            raise ValueError("invalid query class")
        _vector(record)
        for field in ("forbidden_result_count", "response_characters", "budget_characters", "escape_count"):
            _integer(record, field)
        for field in ("citation_match", "evidence_class_match", "freshness_honest", "repository_hash_match", "leak_detected"):
            _boolean(record, field)
        _digests(record, "expected_result_identities")
        _digests(record, "actual_result_identities")
        _digest(record, "result_digest")
    elif record_type == "update":
        _digest(record, "incremental_digest")
        _digest(record, "rebuild_digest")
    elif record_type == "rebuild":
        if record["phase"] not in {"cold", "warmup", "warm", "after-mutation"}:
            raise ValueError("invalid rebuild phase")
        for field in ("peak_rss_bytes", "index_and_staging_bytes", "relevant_source_bytes"):
            _integer(record, field)
    elif record_type == "determinism":
        _vector(record)
        _boolean(record, "permuted")
        _digest(record, "result_digest")
    elif record_type == "repository":
        _digest(record, "before_hash")
        _digest(record, "after_hash")


def _validate_schedule(grouped: Mapping[str, list[Mapping[str, object]]]) -> None:
    if (
        len(grouped["process"]) != 1
        or grouped["process"][0]["phase"] != "product-metadata"
        or grouped["process"][0]["ordinal"] != 0
        or grouped["process"][0]["retained"] is not True
    ):
        raise ValueError("missing binary product evidence")
    rebuild = {(item["phase"], item["ordinal"], item["retained"]) for item in grouped["rebuild"]}
    expected_rebuild = {("cold", 0, True), ("warmup", 0, False), ("after-mutation", 1, True)} | {("warm", ordinal, True) for ordinal in range(1, 6)}
    if rebuild != expected_rebuild or len(grouped["rebuild"]) != 8:
        raise ValueError("incomplete rebuild schedule")
    updates = {(item["ordinal"], item["retained"]) for item in grouped["update"]}
    if updates != {(0, False)} | {(ordinal, True) for ordinal in range(1, 6)} or len(grouped["update"]) != 6:
        raise ValueError("incomplete update schedule")
    for ordinal in range(1, 25):
        vector = f"L1-Q-{ordinal:03d}"
        queries = [item for item in grouped["query"] if item["vector_identity"] == vector]
        if {(item["ordinal"], item["retained"]) for item in queries} != {(0, False)} | {(sample, True) for sample in range(1, 6)} or len(queries) != 6:
            raise ValueError("incomplete query schedule")
        expected_class = "fuzzy" if vector in {"L1-Q-009", "L1-Q-015"} else "exact"
        if any(item["query_class"] != expected_class for item in queries):
            raise ValueError("invalid query class schedule")
        determinism = [item for item in grouped["determinism"] if item["vector_identity"] == vector]
        if len(determinism) != 4 or sum(bool(item["permuted"]) for item in determinism) != 2:
            raise ValueError("incomplete determinism schedule")
    for vector in _STATE_VECTORS:
        samples = [item for item in grouped["query"] if item["vector_identity"] == vector]
        if (
            len(samples) != 1
            or samples[0]["query_class"] != "state"
            or samples[0]["ordinal"] != 1
            or samples[0]["retained"] is not True
        ):
            raise ValueError("incomplete failure-state schedule")
    if len(grouped["repository"]) != 1:
        raise ValueError("incomplete repository schedule")


def _recall(samples: tuple[Mapping[str, object], ...]) -> float:
    expected = 0
    found = 0
    for sample in samples:
        required = set(sample["expected_result_identities"])
        actual = set(sample["actual_result_identities"])
        expected += len(required)
        found += len(required & actual)
    return 1.0 if expected == 0 else found / expected


def _determinism_passes(samples: tuple[Mapping[str, object], ...]) -> bool:
    for ordinal in range(1, 25):
        values = {sample["result_digest"] for sample in samples if sample["vector_identity"] == f"L1-Q-{ordinal:03d}"}
        if len(values) != 1:
            return False
    return True


def _raw_dimension(report: GateReport, dimension: str) -> float:
    return {
        "retrieval": (report.exact_top_five_recall + report.fuzzy_top_ten_recall) / 2,
        "incremental": float(report.incremental_update_p95_ns),
        "query-latency": float(report.warm_query_p95_ns),
        "build": float(report.initial_build_ns),
        "memory-storage": float(report.peak_rss_bytes + report.index_and_staging_bytes),
        "packaging": 1.0,
        "security": 1.0 if report.gate_statuses["safety"] is GateStatus.PASS else 0.0,
        "maintenance": 1.0,
    }[dimension]


def _product_metadata(evidence: CandidateEvidence) -> Mapping[str, object]:
    if len(evidence.process_samples) != 1:
        raise ValueError("missing binary product evidence")
    return evidence.process_samples[0]


def _raw_dimension_from_evidence(
    evidence: CandidateEvidence,
    report: GateReport,
    dimension: str,
) -> float:
    if dimension not in {"packaging", "security", "maintenance"}:
        return _raw_dimension(report, dimension)
    metadata = _product_metadata(evidence)
    if dimension == "packaging":
        points = (
            (4 if metadata["single_self_contained_artifact"] else 0)
            + (2 if int(metadata["cross_platform_targets"]) >= 2 else 0)
            + (2 if metadata["no_system_dependency"] else 0)
            + (1 if metadata["lock_license_complete"] else 0)
            + (1 if metadata["startup_simple"] else 0)
        )
        return points / 10
    if dimension == "security":
        return int(metadata["security_checks_passed"]) / int(metadata["security_checks_total"])
    return int(metadata["maintenance_checks_passed"]) / int(metadata["maintenance_checks_total"])


def _score_from_report(report: GateReport, population: tuple[GateReport, ...]) -> CandidateScore:
    eligible = not report.failure_reason_codes and report.support_status is GateStatus.PASS
    weights = {"retrieval": 25.0, "incremental": 20.0, "query-latency": 15.0, "build": 10.0, "memory-storage": 10.0, "packaging": 10.0, "security": 5.0, "maintenance": 5.0}
    lower = {"incremental", "query-latency", "build", "memory-storage"}
    raw = {dimension: _raw_dimension(report, dimension) for dimension in weights}
    points: dict[str, float] = {}
    eligible_reports = tuple(item for item in population if not item.failure_reason_codes) or population
    for dimension, weight in weights.items():
        values = [_raw_dimension(item, dimension) for item in eligible_reports]
        minimum, maximum = min(values), max(values)
        value = raw[dimension]
        normalized = 1.0 if minimum == maximum else ((maximum - value) / (maximum - minimum) if dimension in lower else (value - minimum) / (maximum - minimum))
        points[dimension] = weight * normalized if eligible else 0.0
    return CandidateScore(report.candidate_identity, eligible, MappingProxyType(raw), MappingProxyType(points), round(sum(points.values()), 6), (0, report.warm_query_p95_ns, report.candidate_identity))


def _dominated_candidates(reports: tuple[GateReport, ...]) -> set[str]:
    dominated: set[str] = set()
    for candidate in reports:
        for other in reports:
            if candidate is other:
                continue
            lower_or_equal = other.warm_query_p95_ns <= candidate.warm_query_p95_ns and other.incremental_update_p95_ns <= candidate.incremental_update_p95_ns and other.initial_build_ns <= candidate.initial_build_ns and other.peak_rss_bytes <= candidate.peak_rss_bytes
            strictly = other.warm_query_p95_ns < candidate.warm_query_p95_ns or other.incremental_update_p95_ns < candidate.incremental_update_p95_ns or other.initial_build_ns < candidate.initial_build_ns or other.peak_rss_bytes < candidate.peak_rss_bytes
            if lower_or_equal and strictly:
                dominated.add(candidate.candidate_identity)
    return dominated


def _load_line(raw: bytes) -> dict[str, object]:
    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("nonfinite value")))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid JSONL leaf") from error
    if type(value) is not dict:
        raise ValueError("invalid JSONL leaf")
    return value


def _text(value: Mapping[str, object], field: str) -> str:
    item = value.get(field)
    if type(item) is not str or not item or len(item) > 512 or "\n" in item or "\x00" in item:
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


def _digest(value: Mapping[str, object], field: str) -> str:
    item = _text(value, field)
    if len(item) != _SHA256_LENGTH or not item.startswith("sha256:"):
        raise ValueError(f"invalid {field}")
    try:
        int(item[7:], 16)
    except ValueError as error:
        raise ValueError(f"invalid {field}") from error
    return item


def _digests(value: Mapping[str, object], field: str) -> tuple[str, ...]:
    raw = value.get(field)
    if type(raw) is not list:
        raise ValueError(f"invalid {field}")
    result = tuple(_digest({field: item}, field) for item in raw)
    if result != tuple(sorted(set(result))):
        raise ValueError(f"invalid {field}")
    return result


def _vector(value: Mapping[str, object]) -> str:
    item = _text(value, "vector_identity")
    if item not in _QUERY_VECTORS and item not in _STATE_VECTORS:
        raise ValueError("invalid vector identity")
    return item
