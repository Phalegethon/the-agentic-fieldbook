"""Level 1 bakeoff evidence retention and command-line entry point."""

from __future__ import annotations

import argparse
import json
import os
import hashlib
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterable, Mapping, Optional

from taf_context.git_snapshot import collect_snapshot
from taf_context.level1_models import (
    CandidateAvailability,
    CandidateManifest,
    Level1Operation,
    Level1Request,
    Level1Result,
)

if __package__ in {None, ""}:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tests.taf_context.level1_corpus import (
        CorpusClass, CorpusManifest, MutationManifest, apply_mutation,
        generate_level1_corpus,
    )
    from tests.taf_context.level1_process import (
        CandidateProcessError, ProcessEvidence, preflight_candidate, run_candidate,
    )
    from tests.taf_context.level1_scoring import (
        BakeoffDecision, CandidateEvidence, CandidateScore, GateReport, GateStatus,
        decide_bakeoff, evaluate_gates, score_candidate,
    )
    from tests.taf_context.repo_factory import commit_all, init_repo, run as run_git
else:
    from .level1_corpus import (
        CorpusClass, CorpusManifest, MutationManifest, apply_mutation,
        generate_level1_corpus,
    )
    from .level1_process import (
        CandidateProcessError, ProcessEvidence, preflight_candidate, run_candidate,
    )
    from .level1_scoring import (
        BakeoffDecision, CandidateEvidence, CandidateScore, GateReport, GateStatus,
        decide_bakeoff, evaluate_gates, score_candidate,
    )
    from .repo_factory import commit_all, init_repo, run as run_git


_CONTROLLER_OWNED_FIELDS = {
    "record_type",
    "sample_identity",
    "environment_identity",
    "candidate_identity",
    "candidate_digest",
    "phase",
    "vector_identity",
    "retained",
    "ordinal",
    "permuted",
    "query_class",
}


@dataclass(frozen=True)
class BenchmarkSample:
    """One controller-owned schedule slot supplied to a candidate adapter."""

    record_type: str
    sample_identity: str
    phase: Optional[str] = None
    vector_identity: Optional[str] = None
    retained: Optional[bool] = None
    ordinal: Optional[int] = None
    permuted: Optional[bool] = None

    def controller_fields(self) -> dict[str, object]:
        fields: dict[str, object] = {
            "record_type": self.record_type,
            "sample_identity": self.sample_identity,
        }
        for name in ("phase", "vector_identity", "retained", "ordinal", "permuted"):
            value = getattr(self, name)
            if value is not None:
                fields[name] = value
        if self.record_type == "query":
            if self.vector_identity and self.vector_identity.startswith("L1-S-"):
                fields["query_class"] = "state"
            elif self.vector_identity in {"L1-Q-003", "L1-Q-004", "L1-Q-005", "L1-Q-006"}:
                fields["query_class"] = "state"
            elif self.vector_identity in {"L1-Q-009", "L1-Q-015"}:
                fields["query_class"] = "fuzzy"
            else:
                fields["query_class"] = "exact"
        return fields


def level1_sample_schedule() -> tuple[BenchmarkSample, ...]:
    """Return the frozen Level 1 sample schedule in deterministic order."""
    samples: list[BenchmarkSample] = [
        BenchmarkSample("process", "product-metadata", "product-metadata", retained=True, ordinal=0),
        BenchmarkSample("rebuild", "rebuild-cold-0", "cold", retained=True, ordinal=0),
        BenchmarkSample("rebuild", "rebuild-warmup-0", "warmup", retained=False, ordinal=0),
    ]
    samples.extend(
        BenchmarkSample("rebuild", f"rebuild-warm-{ordinal}", "warm", retained=True, ordinal=ordinal)
        for ordinal in range(1, 6)
    )
    for ordinal in range(1, 25):
        vector = f"L1-Q-{ordinal:03d}"
        samples.extend(
            BenchmarkSample(
                "query",
                f"query-{vector}-{sample}",
                vector_identity=vector,
                retained=sample > 0,
                ordinal=sample,
            )
            for sample in range(6)
        )
        samples.extend(
            BenchmarkSample(
                "determinism",
                f"determinism-{vector}-{sample}",
                vector_identity=vector,
                permuted=sample >= 2,
            )
            for sample in range(4)
        )
    samples.extend(
        BenchmarkSample(
            "query",
            f"state-{vector}",
            vector_identity=vector,
            retained=True,
            ordinal=1,
        )
        for vector in ("L1-S-CORRUPT", "L1-S-MOVED", "L1-S-WORKTREE")
    )
    samples.extend(
        BenchmarkSample("update", f"update-{ordinal}", retained=ordinal > 0, ordinal=ordinal)
        for ordinal in range(6)
    )
    samples.append(
        BenchmarkSample("rebuild", "rebuild-after-mutation-1", "after-mutation", retained=True, ordinal=1)
    )
    samples.append(BenchmarkSample("repository", "repository-final"))
    return tuple(samples)


def run_benchmark_schedule(
    *,
    run_identity: str,
    environment_identity: str,
    candidate_identity: str,
    candidate_digest: str,
    corpus_identity: str,
    evidence_root: Path,
    execute: Callable[[BenchmarkSample], Mapping[str, object]],
) -> tuple[CandidateEvidence, GateReport]:
    """Execute the frozen schedule while retaining controller-owned identities."""
    header = {
        "record_type": "run",
        "schema_version": "1",
        "run_identity": run_identity,
        "environment_identity": environment_identity,
        "candidate_identity": candidate_identity,
        "candidate_digest": candidate_digest,
        "corpus_identity": corpus_identity,
    }
    common = {
        "environment_identity": environment_identity,
        "candidate_identity": candidate_identity,
        "candidate_digest": candidate_digest,
    }
    records: list[Mapping[str, object]] = [header]
    active_sample: Optional[BenchmarkSample] = None
    try:
        for sample in level1_sample_schedule():
            active_sample = sample
            observed = execute(sample)
            if type(observed) is not dict or set(observed) & _CONTROLLER_OWNED_FIELDS:
                raise ValueError("candidate adapter attempted to set controller-owned fields")
            records.append({**common, **sample.controller_fields(), **observed})
    except Exception as error:
        evidence_root.mkdir(parents=True, exist_ok=True)
        partial = b"".join(_canonical_json(record) + b"\n" for record in records)
        _install_new_file(evidence_root / "partial-evidence.jsonl", partial)
        failure = {
            "schema_version": "1",
            "run_identity": run_identity,
            "candidate_identity": candidate_identity,
            "failed_sample_identity": (
                active_sample.sample_identity if active_sample is not None else "schedule-start"
            ),
            "exception_type": type(error).__name__,
        }
        _install_new_file(
            evidence_root / "failure.json",
            _canonical_json(failure) + b"\n",
        )
        raise
    return retain_candidate_evidence(records, evidence_root)


class ProcessScheduleExecutor:
    """Translate controller schedule slots into isolated candidate processes."""

    def __init__(
        self,
        manifest: CandidateManifest,
        candidate_root: Path,
        repo: Path,
        state_root: Path,
        corpus: CorpusManifest,
        process_evidence_root: Path,
    ) -> None:
        self.manifest = manifest
        self.candidate_root = candidate_root
        self.repo = repo
        self.state_root = state_root
        self.corpus = corpus
        self.process_evidence_root = process_evidence_root
        self.snapshot = collect_snapshot(repo)
        if self.snapshot.head_sha is None:
            raise ValueError("benchmark corpus has no committed head")
        self.initial_repository_hash = _repository_content_hash(repo)
        self.current_index_identity: Optional[str] = None
        self.base_index_identity: Optional[str] = None
        self.mutation: Optional[MutationManifest] = None
        self.clean_rebuild_observation: Optional[dict[str, object]] = None
        self.clean_semantic_digest: Optional[str] = None
        self._vectors = _load_vectors()
        self._run_counter = 0
        preflight = preflight_candidate(manifest, candidate_root, os.environ)
        if preflight.availability is not CandidateAvailability.READY:
            reason = preflight.reason_codes[0] if preflight.reason_codes else "candidate-unsupported"
            raise CandidateProcessError(reason)
        self.preflight = preflight

    def __call__(self, sample: BenchmarkSample) -> dict[str, object]:
        if sample.record_type == "process":
            return self._product_metadata()
        if sample.record_type == "rebuild":
            if sample.phase == "after-mutation":
                self._prepare_mutation()
                assert self.clean_rebuild_observation is not None
                return dict(self.clean_rebuild_observation)
            return self._build(sample)
        if sample.record_type == "query":
            if sample.vector_identity and sample.vector_identity.startswith("L1-S-"):
                return self._state_query(sample)
            return self._query(sample)
        if sample.record_type == "determinism":
            return self._determinism(sample)
        if sample.record_type == "update":
            return self._update(sample)
        if sample.record_type == "repository":
            return self._repository_record()
        raise ValueError("unknown benchmark sample")

    def _product_metadata(self) -> dict[str, object]:
        regular_files = tuple(
            path for path in self.candidate_root.rglob("*")
            if path.is_file() and ".git" not in path.parts
        )
        language = self.manifest.language.lower()
        compiled = language in {"go", "rust"}
        runtime_artifacts = 1 if compiled else max(1, len(regular_files))
        security_passes = sum(
            (
                self.preflight.isolation.offline_enforced,
                self.preflight.isolation.child_process_audited,
                self.preflight.isolation.rss_measured,
                not self.manifest.declared_child_processes,
                not self.preflight.reason_codes,
            )
        )
        maintenance_passes = sum(
            (
                self.manifest.protocol_version == "1",
                self.preflight.dependency_lock_digest is not None,
                self.preflight.license_inventory_digest is not None,
                not self.manifest.declared_child_processes,
                len(self.manifest.arguments) <= 2,
            )
        )
        return {
            "elapsed_ns": 0,
            "peak_rss_bytes": 0,
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "exit_code": 0,
            "escape_count": 0,
            "runtime_artifacts": runtime_artifacts,
            "single_self_contained_artifact": compiled,
            "cross_platform_targets": 1,
            "no_system_dependency": compiled,
            "lock_license_complete": (
                self.preflight.dependency_lock_digest is not None
                and self.preflight.license_inventory_digest is not None
            ),
            "startup_simple": len(self.manifest.arguments) <= 1,
            "security_checks_passed": security_passes,
            "security_checks_total": 5,
            "maintenance_checks_passed": maintenance_passes,
            "maintenance_checks_total": 5,
        }

    def _build(self, sample: BenchmarkSample) -> dict[str, object]:
        request = _control_request(
            Level1Operation.BUILD,
            self.snapshot,
            self.manifest,
            None,
            sample.sample_identity,
        )
        result, process = self._run(request, self.state_root, sample.sample_identity)
        if result.index_identity is None:
            raise CandidateProcessError("build-index-identity-missing")
        self.current_index_identity = result.index_identity
        self.base_index_identity = result.index_identity
        return {
            "elapsed_ns": process.elapsed_ns,
            "peak_rss_bytes": process.peak_rss_bytes,
            "index_and_staging_bytes": _regular_file_bytes(self.state_root),
            "relevant_source_bytes": self.corpus.relevant_source_bytes,
        }

    def _query(self, sample: BenchmarkSample) -> dict[str, object]:
        if sample.vector_identity is None or self.current_index_identity is None:
            raise CandidateProcessError("query-before-build")
        vector = self._vectors[sample.vector_identity]
        request = _vector_request(
            vector,
            self.snapshot,
            self.manifest,
            self.current_index_identity,
            sample.sample_identity,
        )
        result, process = self._run(request, self.state_root, sample.sample_identity)
        return _query_observation(vector, request, result, process)

    def _determinism(self, sample: BenchmarkSample) -> dict[str, object]:
        if sample.vector_identity is None or self.current_index_identity is None:
            raise CandidateProcessError("determinism-before-build")
        vector = self._vectors[sample.vector_identity]
        request = _vector_request(
            vector,
            self.snapshot,
            self.manifest,
            self.current_index_identity,
            sample.sample_identity,
        )
        result, _process = self._run(
            request,
            self.state_root,
            sample.sample_identity,
            permute_wire=bool(sample.permuted),
        )
        return {"result_digest": _semantic_result_digest(result)}

    def _prepare_mutation(self) -> None:
        if self.mutation is not None:
            return
        if self.base_index_identity is None:
            raise CandidateProcessError("mutation-before-build")
        pristine = self.state_root.parent / "pristine-state"
        _copy_tree(self.state_root, pristine)
        mutation_identity = (
            "dirty-refactor-100"
            if self.corpus.first_party_file_count >= 100
            else "committed-add-modify-rename-delete"
        )
        self.mutation = apply_mutation(self.repo, self.corpus, mutation_identity)
        self.snapshot = collect_snapshot(self.repo)
        clean_state = self.state_root.parent / "clean-rebuild-state"
        clean_request = _control_request(
            Level1Operation.BUILD,
            self.snapshot,
            self.manifest,
            None,
            "rebuild-after-mutation-1",
        )
        clean_result, clean_process = self._run(
            clean_request,
            clean_state,
            "setup-clean-after-mutation",
        )
        if clean_result.index_identity is None:
            raise CandidateProcessError("clean-build-index-identity-missing")
        self.clean_semantic_digest = self._semantic_suite_digest(
            clean_state,
            clean_result.index_identity,
            "clean",
        )
        self.clean_rebuild_observation = {
            "elapsed_ns": clean_process.elapsed_ns,
            "peak_rss_bytes": clean_process.peak_rss_bytes,
            "index_and_staging_bytes": _regular_file_bytes(clean_state),
            "relevant_source_bytes": self.corpus.relevant_source_bytes,
        }

    def _update(self, sample: BenchmarkSample) -> dict[str, object]:
        self._prepare_mutation()
        assert self.base_index_identity is not None
        assert self.clean_semantic_digest is not None
        update_state = self.state_root.parent / f"update-state-{sample.ordinal}"
        _copy_tree(self.state_root.parent / "pristine-state", update_state)
        request = _control_request(
            Level1Operation.UPDATE,
            self.snapshot,
            self.manifest,
            self.base_index_identity,
            sample.sample_identity,
        )
        result, process = self._run(request, update_state, sample.sample_identity)
        if result.index_identity is None:
            raise CandidateProcessError("update-index-identity-missing")
        incremental = self._semantic_suite_digest(
            update_state,
            result.index_identity,
            f"update-{sample.ordinal}",
        )
        return {
            "elapsed_ns": process.elapsed_ns,
            "incremental_digest": incremental,
            "rebuild_digest": self.clean_semantic_digest,
        }

    def _semantic_suite_digest(self, state: Path, index_identity: str, label: str) -> str:
        digests: list[dict[str, str]] = []
        for vector_identity, vector in sorted(self._vectors.items()):
            request = _vector_request(
                vector,
                self.snapshot,
                self.manifest,
                index_identity,
                f"semantic-{label}-{vector_identity.lower()}",
            )
            result, _process = self._run(
                request,
                state,
                f"semantic-{label}-{vector_identity.lower()}",
            )
            digests.append(
                {"vector_identity": vector_identity, "result_digest": _semantic_result_digest(result)}
            )
        return "sha256:" + hashlib.sha256(_canonical_json(digests)).hexdigest()

    def _state_query(self, sample: BenchmarkSample) -> dict[str, object]:
        if self.current_index_identity is None or sample.vector_identity is None:
            raise CandidateProcessError("state-query-before-build")
        state = self.state_root.parent / sample.vector_identity.lower()
        _copy_tree(self.state_root, state)
        snapshot = self.snapshot
        if sample.vector_identity == "L1-S-CORRUPT":
            candidates = tuple(
                path for path in sorted(state.rglob("*"))
                if path.is_file() and path.name != "candidate.sb"
            )
            target = candidates[0] if candidates else state / "corrupt-index.marker"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"TAF_CORRUPT_INDEX\n")
        request = _control_request(
            Level1Operation.STATUS,
            snapshot,
            self.manifest,
            self.current_index_identity,
            sample.sample_identity,
        )
        wire = request.to_dict()
        if sample.vector_identity == "L1-S-MOVED":
            wire["repository_identity"] = "sha256:" + "a" * 64
        elif sample.vector_identity == "L1-S-WORKTREE":
            wire["worktree_identity"] = "sha256:" + "b" * 64
        request = Level1Request.from_dict(wire)
        try:
            result, process = self._run(request, state, sample.sample_identity)
        except CandidateProcessError as error:
            unsafe = error.reason_code in {
                "repository-mutated",
                "executable-mutated",
                "unsafe-result-preview",
            }
            digest = "sha256:" + hashlib.sha256(error.reason_code.encode("ascii")).hexdigest()
            return _state_observation(0, 1 if unsafe else 0, not unsafe, digest, 0, False)
        honest = result.status.value != "ready" or result.freshness.value != "exact"
        return _state_observation(
            process.elapsed_ns,
            sum(process.escape_counters.values()),
            True,
            _semantic_result_digest(result),
            result.output_characters,
            _leak_detected(result),
            freshness_honest=honest,
        )

    def _repository_record(self) -> dict[str, object]:
        if self.mutation is not None:
            run_git(self.repo, "git", "reset", "--hard", "HEAD")
            for path in self.mutation.added_paths:
                candidate = self.repo / path
                if candidate.is_file() or candidate.is_symlink():
                    candidate.unlink()
        after = _repository_content_hash(self.repo)
        return {"before_hash": self.initial_repository_hash, "after_hash": after}

    def _run(
        self,
        request: Level1Request,
        state: Path,
        label: str,
        *,
        permute_wire: bool = False,
    ) -> tuple[Level1Result, ProcessEvidence]:
        self._run_counter += 1
        evidence = self.process_evidence_root / f"{self._run_counter:04d}-{label}"
        return run_candidate(
            self.manifest,
            request,
            self.repo,
            state,
            120.0,
            evidence,
            candidate_root=self.candidate_root,
            permute_wire=permute_wire,
        )


def retain_candidate_evidence(
    leaf_records: Iterable[Mapping[str, object]],
    evidence_root: Path,
) -> tuple[CandidateEvidence, GateReport]:
    """Install one immutable JSONL run, reopen it, then derive its report."""
    if not isinstance(evidence_root, Path):
        raise ValueError("invalid evidence root")
    evidence_root.mkdir(parents=True, exist_ok=True)
    evidence_path = evidence_root / "evidence.jsonl"
    descriptor = os.open(
        evidence_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            for record in leaf_records:
                payload = json.dumps(
                    dict(record),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                ).encode("ascii")
                handle.write(payload + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        directory = os.open(evidence_root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        raise

    evidence = CandidateEvidence.from_jsonl(evidence_path)
    report = evaluate_gates(evidence)
    _install_new_file(
        evidence_root / "gate-report.json",
        _canonical_json(_report_dict(report)) + b"\n",
    )
    return evidence, report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one isolated Level 1 candidate bakeoff")
    parser.add_argument("--candidate-manifest", type=Path, required=True)
    parser.add_argument("--corpus-class", choices=("small", "medium", "large"), required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--reference-machine", required=True)
    return parser


def main(
    argv: Optional[list[str]] = None,
    *,
    execute_factory: Optional[Callable[..., Callable[[BenchmarkSample], Mapping[str, object]]]] = None,
) -> int:
    arguments = build_parser().parse_args(argv)
    if not arguments.candidate_manifest.is_file():
        raise SystemExit("candidate manifest does not exist")
    if arguments.evidence_root.exists() and any(arguments.evidence_root.iterdir()):
        raise SystemExit("evidence root is not empty")
    if not arguments.reference_machine or "\n" in arguments.reference_machine:
        raise SystemExit("invalid reference machine")
    manifest_wire = _load_json_object(arguments.candidate_manifest)
    manifest = CandidateManifest.from_dict(manifest_wire)
    candidate_digest = "sha256:" + hashlib.sha256(_canonical_json(manifest.to_dict())).hexdigest()
    if manifest.availability is CandidateAvailability.UNSUPPORTED:
        _retain_unsupported_candidate(
            manifest,
            candidate_digest,
            arguments.reference_machine,
            arguments.evidence_root,
        )
        return 0
    if execute_factory is None:
        preflight = preflight_candidate(
            manifest,
            arguments.candidate_manifest.parent.resolve(),
            os.environ,
        )
        if preflight.availability is not CandidateAvailability.READY:
            _retain_unsupported_candidate(
                manifest,
                candidate_digest,
                arguments.reference_machine,
                arguments.evidence_root,
                reason_codes=preflight.reason_codes,
            )
            return 0
    factory = execute_factory or _process_execute_factory

    with tempfile.TemporaryDirectory(prefix="taf-level1-benchmark-") as temporary:
        temporary_root = Path(temporary)
        repo = init_repo(temporary_root / "repo")
        corpus = generate_level1_corpus(repo, CorpusClass(arguments.corpus_class))
        commit_all(repo, "level1-corpus")
        corpus_identity = "sha256:" + hashlib.sha256(corpus.to_json_bytes()).hexdigest()
        state_root = temporary_root / "state"
        process_evidence_root = temporary_root / "process-evidence"
        execute = factory(
            manifest,
            arguments.candidate_manifest.parent.resolve(),
            repo,
            state_root,
            corpus,
            process_evidence_root,
        )
        evidence, report = run_benchmark_schedule(
            run_identity="run-" + uuid.uuid4().hex,
            environment_identity=arguments.reference_machine,
            candidate_identity=manifest.candidate_identity,
            candidate_digest=candidate_digest,
            corpus_identity=corpus_identity,
            evidence_root=arguments.evidence_root,
            execute=execute,
        )

    score = score_candidate(evidence, (evidence,))
    decision = decide_bakeoff((report,), (score,))
    _install_new_file(
        arguments.evidence_root / "decision.json",
        _canonical_json(_decision_dict(decision)) + b"\n",
    )
    return 0


def _report_dict(report: GateReport) -> dict[str, object]:
    return {
        "candidate_identity": report.candidate_identity,
        "support_status": report.support_status.value,
        "gate_statuses": {key: value.value for key, value in report.gate_statuses.items()},
        "failure_reason_codes": list(report.failure_reason_codes),
        "exact_top_five_recall": report.exact_top_five_recall,
        "fuzzy_top_ten_recall": report.fuzzy_top_ten_recall,
        "warm_query_p95_ns": report.warm_query_p95_ns,
        "incremental_update_p95_ns": report.incremental_update_p95_ns,
        "initial_build_ns": report.initial_build_ns,
        "peak_rss_bytes": report.peak_rss_bytes,
        "index_and_staging_bytes": report.index_and_staging_bytes,
        "relevant_source_bytes": report.relevant_source_bytes,
        "response_budget_overruns": report.response_budget_overruns,
    }


def _score_dict(score: CandidateScore) -> dict[str, object]:
    return {
        "candidate_identity": score.candidate_identity,
        "eligible": score.eligible,
        "dimension_raw_values": dict(score.dimension_raw_values),
        "dimension_points": dict(score.dimension_points),
        "total_points": score.total_points,
        "tie_break_values": list(score.tie_break_values),
    }


def _decision_dict(decision: BakeoffDecision) -> dict[str, object]:
    return {
        "schema_version": decision.schema_version,
        "run_identity": decision.run_identity,
        "status": decision.status,
        "recommended_candidate": decision.recommended_candidate,
        "candidate_reports": [_report_dict(report) for report in decision.candidate_reports],
        "candidate_scores": [_score_dict(score) for score in decision.candidate_scores],
        "pareto_dominated_candidates": list(decision.pareto_dominated_candidates),
        "reason_codes": list(decision.reason_codes),
        "provisional": decision.provisional,
        "next_authorized_scope": decision.next_authorized_scope,
    }


def _load_json_object(path: Path) -> dict[str, object]:
    if path.stat().st_size > 256 * 1024:
        raise ValueError("candidate manifest is oversized")

    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate candidate manifest key")
            result[key] = value
        return result

    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=pairs,
        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("nonfinite candidate manifest value")),
    )
    if type(value) is not dict:
        raise ValueError("candidate manifest must be an object")
    return value


def _retain_unsupported_candidate(
    manifest: CandidateManifest,
    candidate_digest: str,
    environment_identity: str,
    evidence_root: Path,
    *,
    reason_codes: Optional[tuple[str, ...]] = None,
) -> None:
    reasons = reason_codes if reason_codes is not None else manifest.unsupported_reason_codes
    evidence_root.mkdir(parents=True, exist_ok=True)
    support_record = {
        "record_type": "unsupported",
        "schema_version": "1",
        "run_identity": "run-" + uuid.uuid4().hex,
        "environment_identity": environment_identity,
        "candidate_identity": manifest.candidate_identity,
        "candidate_digest": candidate_digest,
        "reason_codes": list(reasons),
    }
    gate_names = (
        "correctness", "determinism", "incremental", "initial-build",
        "memory", "model-budget", "query-latency", "safety", "storage",
    )
    report = GateReport(
        manifest.candidate_identity,
        GateStatus.UNSUPPORTED,
        MappingProxyType({name: GateStatus.UNSUPPORTED for name in gate_names}),
        reasons,
        0.0,
        0.0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
    )
    dimensions = (
        "retrieval", "incremental", "query-latency", "build",
        "memory-storage", "packaging", "security", "maintenance",
    )
    score = CandidateScore(
        manifest.candidate_identity,
        False,
        MappingProxyType({name: 0.0 for name in dimensions}),
        MappingProxyType({name: 0.0 for name in dimensions}),
        0.0,
        (0, 0, manifest.candidate_identity),
    )
    decision = decide_bakeoff((report,), (score,))
    _install_new_file(
        evidence_root / "unsupported.jsonl",
        _canonical_json(support_record) + b"\n",
    )
    _install_new_file(
        evidence_root / "gate-report.json",
        _canonical_json(_report_dict(report)) + b"\n",
    )
    _install_new_file(
        evidence_root / "decision.json",
        _canonical_json(_decision_dict(decision)) + b"\n",
    )


def _process_execute_factory(
    manifest: CandidateManifest,
    candidate_root: Path,
    repo: Path,
    state_root: Path,
    corpus: CorpusManifest,
    process_evidence_root: Path,
) -> Callable[[BenchmarkSample], Mapping[str, object]]:
    return ProcessScheduleExecutor(
        manifest,
        candidate_root,
        repo,
        state_root,
        corpus,
        process_evidence_root,
    )


def _load_vectors() -> dict[str, dict[str, object]]:
    root = Path(__file__).parent / "conformance" / "level1-query"
    vectors: dict[str, dict[str, object]] = {}
    for path in sorted(root.glob("*.json")):
        value = _load_json_object(path)
        identity = value.get("vector_identity")
        if type(identity) is not str or identity in vectors:
            raise ValueError("invalid conformance vector identity")
        Level1Request.from_dict(value["request"])  # type: ignore[arg-type]
        vectors[identity] = value
    if tuple(sorted(vectors)) != tuple(f"L1-Q-{ordinal:03d}" for ordinal in range(1, 25)):
        raise ValueError("incomplete conformance vectors")
    return vectors


def _control_request(
    operation: Level1Operation,
    snapshot: object,
    manifest: CandidateManifest,
    index_identity: Optional[str],
    request_identity: str,
) -> Level1Request:
    head_sha = getattr(snapshot, "head_sha")
    if type(head_sha) is not str:
        raise ValueError("missing committed head")
    return Level1Request.from_dict(
        {
            "schema_version": "1",
            "request_identity": request_identity.lower(),
            "consumer_identity": "taf-bakeoff",
            "operation": operation.value,
            "repository_identity": getattr(snapshot, "repository_identity"),
            "worktree_identity": getattr(snapshot, "worktree_identity"),
            "committed_head": head_sha,
            "dirty_overlay_fingerprint": getattr(snapshot, "dirty_fingerprint"),
            "provider_identity": manifest.candidate_identity,
            "index_identity": index_identity,
            "required_capability": operation.value,
            "minimum_freshness": "exact",
            "query": None,
            "result_identities": [],
            "filters": {
                "path_prefixes": [],
                "languages": [],
                "symbol_kinds": [],
                "source_types": [],
            },
            "maximum_results": 10,
            "maximum_model_output_characters": 4000,
            "allow_inferred": False,
        }
    )


def _vector_request(
    vector: Mapping[str, object],
    snapshot: object,
    manifest: CandidateManifest,
    index_identity: str,
    request_identity: str,
) -> Level1Request:
    raw = vector.get("request")
    if type(raw) is not dict:
        raise ValueError("invalid vector request")
    wire = dict(raw)
    actual = {
        "repository_identity": getattr(snapshot, "repository_identity"),
        "worktree_identity": getattr(snapshot, "worktree_identity"),
        "committed_head": getattr(snapshot, "head_sha"),
        "dirty_overlay_fingerprint": getattr(snapshot, "dirty_fingerprint"),
        "index_identity": index_identity,
    }
    declared = dict(wire)
    wire.update(actual)
    vector_identity = vector.get("vector_identity")
    if vector_identity == "L1-Q-003":
        wire["committed_head"] = declared["committed_head"]
    elif vector_identity == "L1-Q-004":
        wire["worktree_identity"] = declared["worktree_identity"]
    elif vector_identity == "L1-Q-005":
        wire["index_identity"] = declared["index_identity"]
    elif vector_identity == "L1-Q-006":
        wire["repository_identity"] = "sha256:" + "6" * 64
    wire.update(
        {
            "request_identity": request_identity.lower(),
            "provider_identity": manifest.candidate_identity,
        }
    )
    return Level1Request.from_dict(wire)


def _query_observation(
    vector: Mapping[str, object],
    request: Level1Request,
    result: Level1Result,
    process: ProcessEvidence,
) -> dict[str, object]:
    required_status = vector["required_status"]
    safe_refusal = required_status in {"stale", "error"}
    expected = (
        tuple(vector["expected_result_identities"])  # type: ignore[arg-type]
        if not safe_refusal
        else ()
    )
    forbidden = set(vector["forbidden_result_identities"])  # type: ignore[arg-type]
    actual = tuple(sorted(item.result_identity for item in result.findings))
    expected_citations = {
        (item["path"], item["start_line"], item["end_line"])
        for item in vector["expected_citations"]  # type: ignore[union-attr]
    }
    actual_citations = {
        (item.path, item.start_line, item.end_line)
        for item in result.findings
        if item.result_identity in set(expected)
    }
    evidence_classes = {
        item.evidence_class.value
        for item in result.findings
        if item.result_identity in set(expected)
    }
    expected_class = vector["expected_evidence_class"]
    return {
        "elapsed_ns": process.elapsed_ns,
        "expected_result_identities": list(expected),
        "actual_result_identities": list(actual),
        "citation_match": safe_refusal or expected_citations <= actual_citations,
        "evidence_class_match": safe_refusal or (bool(evidence_classes) and evidence_classes == {expected_class}),
        "freshness_honest": (
            result.status.value == required_status
            and (expected_class != "verified" or result.freshness.value == "exact")
        ),
        "forbidden_result_count": sum(identity in forbidden for identity in actual),
        "response_characters": result.output_characters,
        "budget_characters": request.maximum_model_output_characters,
        "escape_count": sum(process.escape_counters.values()),
        "repository_hash_match": True,
        "leak_detected": _leak_detected(result),
        "result_digest": _semantic_result_digest(result),
    }


def _state_observation(
    elapsed_ns: int,
    escape_count: int,
    repository_hash_match: bool,
    result_digest: str,
    response_characters: int,
    leak_detected: bool,
    *,
    freshness_honest: bool = True,
) -> dict[str, object]:
    return {
        "elapsed_ns": elapsed_ns,
        "expected_result_identities": [],
        "actual_result_identities": [],
        "citation_match": True,
        "evidence_class_match": True,
        "freshness_honest": freshness_honest,
        "forbidden_result_count": 0,
        "response_characters": response_characters,
        "budget_characters": 4000,
        "escape_count": escape_count,
        "repository_hash_match": repository_hash_match,
        "leak_detected": leak_detected,
        "result_digest": result_digest,
    }


def _semantic_result_digest(result: Level1Result) -> str:
    wire = result.to_dict()
    semantic = {
        key: value
        for key, value in wire.items()
        if key not in {"request_identity", "index_identity"}
    }
    return "sha256:" + hashlib.sha256(_canonical_json(semantic)).hexdigest()


def _leak_detected(result: Level1Result) -> bool:
    raw = _canonical_json(result.to_dict()).decode("ascii")
    return bool(
        re.search(r"(?:/Users|/home|/private|/tmp|/var)/|[A-Za-z]:\\", raw)
        or re.search(r"(?i)(?:token|password|secret|api[_-]?key)[=:]", raw)
    )


def _regular_file_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def _copy_tree(source: Path, target: Path) -> None:
    if target.exists():
        raise FileExistsError(target)
    shutil.copytree(source, target)


def _repository_content_hash(repo: Path) -> str:
    tree = run_git(repo, "git", "rev-parse", "HEAD^{tree}")
    return "sha256:" + hashlib.sha256(tree.encode("ascii")).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def _install_new_file(path: Path, content: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


if __name__ == "__main__":
    main()
