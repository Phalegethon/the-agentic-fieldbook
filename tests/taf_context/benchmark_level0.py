#!/usr/bin/env python3
"""Deterministic local benchmark for the native Level 0 context collector.

The public harness creates synthetic Git repositories, but benchmark evidence is
written only to the path supplied by the caller.  A measured sample is run in a
fresh Python process: ``cold_*`` includes process startup, while ``warm_*`` is
the collector interval after imports and argument parsing are complete.
"""

from __future__ import annotations

import argparse
import builtins
import contextlib
import io
import json
import math
import os
from pathlib import Path
import platform
import random
import resource
import socket
import subprocess
import sys
import tempfile
import time
from typing import Callable, Dict, Iterable, Iterator, List, Sequence, Tuple


SEED = 20260825
WARM_UP_RUNS = 1
MEASURED_RUNS = 5
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_DOSSIER_CHARACTERS = 12000
CASES = (
    {"tracked_files": 1000, "dirty_files": 0, "warm_p95_seconds": None},
    {"tracked_files": 10000, "dirty_files": 7, "warm_p95_seconds": 2.0},
    {"tracked_files": 25000, "dirty_files": 25, "warm_p95_seconds": 5.0},
)
FIXED_GIT_ENV = {
    "GIT_AUTHOR_DATE": "2026-08-25T00:00:00Z",
    "GIT_COMMITTER_DATE": "2026-08-25T00:00:00Z",
    "GIT_OPTIONAL_LOCKS": "0",
}
NETWORK_GIT_COMMANDS = {
    "clone",
    "fetch",
    "pull",
    "push",
    "ls-remote",
    "remote-ext",
    "send-pack",
    "upload-archive",
    "upload-pack",
}
NETWORK_EXECUTABLES = {
    "curl",
    "ftp",
    "nc",
    "scp",
    "sftp",
    "ssh",
    "telnet",
    "wget",
}
LLM_EXECUTABLES = {
    "anthropic",
    "claude",
    "codex",
    "gemini",
    "ollama",
    "openai",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure deterministic Level 0 release gates."
    )
    parser.add_argument("--output", type=Path, help="private raw JSON path")
    parser.add_argument("--worker-repo", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--worker-output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--tracked-files", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--dirty-path", action="append", default=[], help=argparse.SUPPRESS)
    return parser


def _run(
    cwd: Path,
    argv: Sequence[str],
    *,
    env: Dict[str, str] = None,
) -> subprocess.CompletedProcess:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        list(argv),
        cwd=str(cwd),
        env=merged,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
        timeout=180,
    )


def _write_fixture_file(path: Path, index: int, dirty: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = "dirty\n" if dirty else "clean\n"
    path.write_text("entry={:08d}\n{}".format(index, suffix), encoding="utf-8")


def _fixture(root: Path, tracked_files: int, dirty_files: int) -> Tuple[Path, List[str]]:
    root.mkdir(parents=True)
    repo = root / "repo"
    repo.mkdir()
    _run(repo, ["git", "init", "-q", "-b", "main"])
    _run(repo, ["git", "config", "user.name", "Level 0 Benchmark Fixture"])
    _run(repo, ["git", "config", "user.email", "fixture@example.invalid"])
    paths = []
    for index in range(tracked_files):
        relative = "src/{:03d}/file_{:05d}.py".format(index // 1000, index)
        paths.append(relative)
        _write_fixture_file(repo / relative, index)
    _run(repo, ["git", "add", "--all"], env=FIXED_GIT_ENV)
    _run(repo, ["git", "commit", "-q", "-m", "fixture"], env=FIXED_GIT_ENV)

    chooser = random.Random(SEED + tracked_files + dirty_files)
    dirty_paths = sorted(chooser.sample(paths, dirty_files))
    for relative in dirty_paths:
        index = int(Path(relative).stem.split("_")[1])
        _write_fixture_file(repo / relative, index, dirty=True)
    return repo, dirty_paths


def _times_total(value: os.times_result) -> float:
    return value.user + value.system + value.children_user + value.children_system


def _rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return int(value)
    return int(value) * 1024


@contextlib.contextmanager
def _guards(repo: Path, dirty_paths: Iterable[str]) -> Iterator[Dict[str, object]]:
    dirty = set(dirty_paths)
    counters = {
        "clean_file_content_reads": 0,
        "dirty_file_content_reads": 0,
        "content_read_paths": [],
        "network_calls": 0,
        "llm_calls": 0,
        "unexpected_process_calls": 0,
        "git_commands": [],
    }
    real_open = Path.open
    real_builtin_open = builtins.open
    real_run = subprocess.run
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create_connection = socket.create_connection

    def record_read(path: Path, mode: str) -> None:
        if "r" not in mode and "+" not in mode:
            return
        try:
            relative = path.absolute().relative_to(repo).as_posix()
        except ValueError:
            return
        counters["content_read_paths"].append(relative)
        key = (
            "dirty_file_content_reads"
            if relative in dirty
            else "clean_file_content_reads"
        )
        counters[key] += 1

    def guarded_open(path: Path, *args: object, **kwargs: object) -> object:
        mode = str(args[0]) if args else str(kwargs.get("mode", "r"))
        record_read(path, mode)
        return real_open(path, *args, **kwargs)

    def guarded_builtin_open(file: object, *args: object, **kwargs: object) -> object:
        mode = str(args[0]) if args else str(kwargs.get("mode", "r"))
        if isinstance(file, (str, bytes, os.PathLike)):
            record_read(Path(file), mode)
        return real_builtin_open(file, *args, **kwargs)

    def guarded_run(argv: Sequence[str], *args: object, **kwargs: object) -> object:
        command = [os.fspath(item) for item in argv]
        executable = Path(command[0]).name if command else ""
        if executable == "git":
            subcommand = command[1] if len(command) > 1 else ""
            counters["git_commands"].append(subcommand)
            if subcommand in NETWORK_GIT_COMMANDS:
                counters["network_calls"] += 1
                raise OSError("network Git command blocked by benchmark")
        elif executable in NETWORK_EXECUTABLES:
            counters["network_calls"] += 1
            raise OSError("network command blocked by benchmark")
        elif executable in LLM_EXECUTABLES:
            counters["llm_calls"] += 1
            raise OSError("LLM command blocked by benchmark")
        else:
            counters["unexpected_process_calls"] += 1
        return real_run(argv, *args, **kwargs)

    def blocked_connect(*args: object, **kwargs: object) -> object:
        counters["network_calls"] += 1
        raise OSError("network blocked by benchmark")

    Path.open = guarded_open  # type: ignore[assignment]
    builtins.open = guarded_builtin_open  # type: ignore[assignment]
    subprocess.run = guarded_run  # type: ignore[assignment]
    socket.socket.connect = blocked_connect  # type: ignore[assignment]
    socket.socket.connect_ex = blocked_connect  # type: ignore[assignment]
    socket.create_connection = blocked_connect  # type: ignore[assignment]
    try:
        yield counters
    finally:
        Path.open = real_open  # type: ignore[assignment]
        builtins.open = real_builtin_open  # type: ignore[assignment]
        subprocess.run = real_run  # type: ignore[assignment]
        socket.socket.connect = real_connect  # type: ignore[assignment]
        socket.socket.connect_ex = real_connect_ex  # type: ignore[assignment]
        socket.create_connection = real_create_connection  # type: ignore[assignment]


def _worker(args: argparse.Namespace) -> int:
    from taf_context.cli import main as context_main

    repo = args.worker_repo.resolve()
    output = args.worker_output.resolve(strict=False)
    dirty_paths = tuple(sorted(args.dirty_path))
    try:
        output.relative_to(repo)
    except ValueError:
        pass
    else:
        raise SystemExit("worker output must be outside fixture repository")

    eligible_dirty_bytes = sum((repo / path).stat().st_size for path in dirty_paths)
    process_before = os.times()
    warm_started = time.perf_counter()
    stdout = io.StringIO()
    stderr = io.StringIO()
    with _guards(repo, dirty_paths) as counters:
        exit_code = context_main(
            ["snapshot", "--repo", str(repo), "--output-dir", str(output)],
            stdout=stdout,
            stderr=stderr,
        )
    warm_wall = time.perf_counter() - warm_started
    warm_cpu = _times_total(os.times()) - _times_total(process_before)

    summary = json.loads(stdout.getvalue()) if exit_code == 0 else {}
    snapshot_path = output / "snapshot.json"
    snapshot = (
        json.loads(snapshot_path.read_text(encoding="utf-8"))
        if snapshot_path.is_file()
        else {}
    )
    artifact_sizes = {
        name: (output / name).stat().st_size
        for name in ("manifest.json", "snapshot.json", "dossier.md")
        if (output / name).is_file()
    }
    actual_dirty = sorted(
        set(snapshot.get("staged_paths", []))
        | set(snapshot.get("unstaged_paths", []))
        | set(snapshot.get("untracked_paths", []))
    )
    checks = {
        "collector_exit_zero": exit_code == 0,
        "tracked_count_exact": len(snapshot.get("tracked_paths", []))
        == args.tracked_files,
        "dirty_paths_exact": actual_dirty == list(dirty_paths),
        "dirty_bytes_within_eligible": snapshot.get("dirty_bytes_hashed", -1)
        <= eligible_dirty_bytes,
        "dirty_bytes_exact": snapshot.get("dirty_bytes_hashed", -1)
        == eligible_dirty_bytes,
        "zero_clean_file_content_reads": counters["clean_file_content_reads"] == 0,
        "zero_network_calls": counters["network_calls"] == 0,
        "zero_llm_calls": counters["llm_calls"] == 0,
        "only_expected_process_calls": counters["unexpected_process_calls"] == 0,
        "all_artifacts_present": set(artifact_sizes)
        == {"manifest.json", "snapshot.json", "dossier.md"},
    }
    result = {
        "warm_wall_seconds": warm_wall,
        "warm_cpu_seconds": warm_cpu,
        "peak_rss_bytes": _rss_bytes(),
        "paths_inspected": len(
            set(snapshot.get("tracked_paths", []))
            | set(snapshot.get("untracked_paths", []))
        ),
        "dirty_bytes_hashed": snapshot.get("dirty_bytes_hashed"),
        "eligible_dirty_bytes": eligible_dirty_bytes,
        "artifact_sizes_bytes": artifact_sizes,
        "artifact_total_bytes": sum(artifact_sizes.values()),
        "dossier_characters": summary.get("dossier_characters"),
        "clean_file_content_reads": counters["clean_file_content_reads"],
        "dirty_file_content_reads": counters["dirty_file_content_reads"],
        "network_calls": counters["network_calls"],
        "llm_calls": counters["llm_calls"],
        "git_commands": counters["git_commands"],
        "correctness_checks": checks,
        "correctness_passed": all(checks.values()),
        "collector_stderr": stderr.getvalue(),
    }
    sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["correctness_passed"] else 1


def _measured_worker(
    repo: Path,
    output: Path,
    tracked_files: int,
    dirty_paths: Sequence[str],
) -> Dict[str, object]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker-repo",
        str(repo),
        "--worker-output",
        str(output),
        "--tracked-files",
        str(tracked_files),
    ]
    for path in dirty_paths:
        command.extend(("--dirty-path", path))
    before = os.times()
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"),
        timeout=180,
    )
    cold_wall = time.perf_counter() - started
    cold_cpu = _times_total(os.times()) - _times_total(before)
    if not completed.stdout:
        raise RuntimeError(
            "benchmark worker emitted no JSON (exit {}): {}".format(
                completed.returncode, completed.stderr.strip()
            )
        )
    sample = json.loads(completed.stdout)
    sample["cold_wall_seconds"] = cold_wall
    sample["cold_cpu_seconds"] = cold_cpu
    sample["worker_exit_code"] = completed.returncode
    if completed.stderr:
        sample["worker_stderr"] = completed.stderr
    return sample


def _percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def _summary(samples: Sequence[Dict[str, object]], field: str) -> Dict[str, float]:
    values = [float(sample[field]) for sample in samples]
    return {
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
    }


def _version(argv: Sequence[str]) -> str:
    try:
        return _run(Path.cwd(), argv).stdout.strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return "unavailable"


def _machine() -> Dict[str, object]:
    return {
        "hostname": platform.node(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "platform": platform.platform(),
        "python": sys.version.replace("\n", " "),
        "python_executable": sys.executable,
        "git": _version(["git", "--version"]),
        "os": _version(["sw_vers"]) if sys.platform == "darwin" else platform.platform(),
        "cpu_model": _version(["sysctl", "-n", "hw.model"])
        if sys.platform == "darwin"
        else platform.processor(),
        "memory_bytes": int(_version(["sysctl", "-n", "hw.memsize"]))
        if sys.platform == "darwin"
        else None,
    }


def _case_result(root: Path, case: Dict[str, object]) -> Dict[str, object]:
    tracked_files = int(case["tracked_files"])
    dirty_files = int(case["dirty_files"])
    repo, dirty_paths = _fixture(root, tracked_files, dirty_files)
    warmup_output = root / "warmup-artifacts"
    warmup = _measured_worker(repo, warmup_output, tracked_files, dirty_paths)
    samples = []
    for index in range(MEASURED_RUNS):
        samples.append(
            _measured_worker(
                repo,
                root / "sample-{:02d}-artifacts".format(index + 1),
                tracked_files,
                dirty_paths,
            )
        )

    status = _run(repo, ["git", "status", "--porcelain=v1", "-z"]).stdout
    declared_status_paths = sorted(
        record[3:] for record in status.split("\0") if record
    )
    corpus_checks = {
        "warmup_correct": bool(warmup["correctness_passed"]),
        "all_samples_correct": all(
            bool(sample["correctness_passed"]) for sample in samples
        ),
        "only_declared_paths_dirty_after_runs": declared_status_paths == dirty_paths,
    }
    wall = {
        "cold_seconds": _summary(samples, "cold_wall_seconds"),
        "warm_seconds": _summary(samples, "warm_wall_seconds"),
    }
    cpu = {
        "cold_seconds": _summary(samples, "cold_cpu_seconds"),
        "warm_seconds": _summary(samples, "warm_cpu_seconds"),
    }
    warm_limit = case["warm_p95_seconds"]
    gates = {
        "warm_p95_within_limit": True
        if warm_limit is None
        else wall["warm_seconds"]["p95"] <= float(warm_limit),
        "dossier_within_12000_characters": max(
            int(sample["dossier_characters"]) for sample in samples
        )
        <= MAX_DOSSIER_CHARACTERS,
        "dirty_bytes_within_eligible": all(
            int(sample["dirty_bytes_hashed"])
            <= int(sample["eligible_dirty_bytes"])
            for sample in samples
        ),
        "zero_clean_file_content_reads": all(
            int(sample["clean_file_content_reads"]) == 0 for sample in samples
        ),
        "artifacts_within_2_mib": max(
            int(sample["artifact_total_bytes"]) for sample in samples
        )
        <= MAX_ARTIFACT_BYTES,
        "zero_network_calls": all(
            int(sample["network_calls"]) == 0 for sample in samples
        ),
        "zero_llm_calls": all(int(sample["llm_calls"]) == 0 for sample in samples),
    }
    return {
        "tracked_files": tracked_files,
        "dirty_files": dirty_files,
        "dirty_paths": dirty_paths,
        "warm_p95_limit_seconds": warm_limit,
        "warmup": warmup,
        "samples": samples,
        "wall_time": wall,
        "cpu_time": cpu,
        "peak_rss_bytes": _summary(samples, "peak_rss_bytes"),
        "corpus_checks": corpus_checks,
        "correctness_passed": all(corpus_checks.values()),
        "gates": gates,
        "mandatory_gates_passed": all(gates.values()),
    }


def _driver(output: Path) -> int:
    output = output.resolve(strict=False)
    results = []
    with tempfile.TemporaryDirectory(prefix="taf-level0-benchmark-") as directory:
        root = Path(directory)
        for index, case in enumerate(CASES):
            results.append(_case_result(root / "case-{}".format(index + 1), case))

    correctness_passed = all(result["correctness_passed"] for result in results)
    mandatory_gates_passed = all(
        result["mandatory_gates_passed"] for result in results
    )
    decision = (
        "GO — Retain Python for production Level 0."
        if mandatory_gates_passed
        else "NO-GO — Python remains a reference implementation; a replacement plan is required."
    )
    evidence = {
        "schema_version": 1,
        "seed": SEED,
        "warm_up_runs_per_class": WARM_UP_RUNS,
        "measured_runs_per_class": MEASURED_RUNS,
        "percentile_method": "nearest-rank",
        "timing_definitions": {
            "cold": "fresh worker process including interpreter and harness startup",
            "warm": "collector interval after worker imports and argument parsing",
        },
        "machine": _machine(),
        "classes": results,
        "correctness_passed": correctness_passed,
        "mandatory_gates_passed": mandatory_gates_passed,
        "python_retention_decision": decision,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(".{}.tmp".format(output.name))
    temporary.write_text(
        json.dumps(evidence, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(str(temporary), str(output))
    print(json.dumps({
        "correctness_passed": correctness_passed,
        "mandatory_gates_passed": mandatory_gates_passed,
        "output": str(output),
        "python_retention_decision": decision,
    }, ensure_ascii=False, sort_keys=True))
    return 0 if correctness_passed else 1


def main(argv: Sequence[str] = None) -> int:
    args = _parser().parse_args(argv)
    if args.worker_repo is not None:
        required = (args.worker_output, args.tracked_files)
        if any(value is None for value in required):
            raise SystemExit("incomplete benchmark worker arguments")
        return _worker(args)
    if args.output is None:
        _parser().error("--output is required")
    return _driver(args.output)


if __name__ == "__main__":
    raise SystemExit(main())
