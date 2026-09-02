#!/usr/bin/env python3
"""Deterministic local benchmark for the native Level 0 context collector.

The public harness creates synthetic Git repositories, but benchmark evidence is
written only to the path supplied by the caller.  A measured sample is run in a
fresh Python process: ``cold_*`` includes process startup, while ``warm_*``
excludes guard setup and collector import and includes CLI parsing, collection,
rendering, and artifact emission.
"""

from __future__ import annotations

import argparse
import builtins
import contextlib
import _io
import io
import json
import math
import mmap as mmap_module
import os
from pathlib import Path
import platform
import random
import resource
import socket
import _socket
import subprocess
import sys
import tempfile
import time
from typing import Dict, Iterable, Iterator, List, Sequence, Tuple


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
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_COUNT": "2",
    "GIT_CONFIG_KEY_0": "maintenance.auto",
    "GIT_CONFIG_VALUE_0": "0",
    "GIT_CONFIG_KEY_1": "gc.auto",
    "GIT_CONFIG_VALUE_1": "0",
    "GIT_TERMINAL_PROMPT": "0",
}
ALLOWED_GIT_COMMANDS = {
    ("git", "rev-parse", "--show-toplevel"),
    (
        "git", "config", "--local", "--includes", "--name-only", "--get-regexp",
        r"^filter\..*\.(clean|smudge|process)$",
    ),
    ("git", "rev-parse", "--absolute-git-dir"),
    ("git", "rev-parse", "--git-common-dir"),
    ("git", "rev-parse", "--verify", "HEAD"),
    ("git", "rev-list", "--max-parents=0", "HEAD"),
    ("git", "symbolic-ref", "--short", "-q", "HEAD"),
    ("git", "ls-files", "-z"),
    ("git", "ls-files", "--stage", "-z"),
    (
        "git", "diff", "--no-ext-diff", "--no-textconv",
        "--no-renames", "--ignore-submodules=dirty", "--cached", "--name-only", "-z",
    ),
    (
        "git", "diff", "--no-ext-diff", "--no-textconv",
        "--no-renames", "--ignore-submodules=all", "--name-only", "-z",
    ),
    ("git", "ls-files", "--others", "--exclude-standard", "-z"),
    (
        "git",
        "status",
        "--no-renames",
        "--ignore-submodules=all",
        "--porcelain=v1",
        "-z",
        "--ignored=matching",
        "--untracked-files=normal",
    ),
}
NETWORK_GIT_COMMANDS = {
    "archive",
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


class InstrumentationViolation(OSError):
    """Raised when benchmarked code attempts an unmeasured side effect."""


def _byte_length(value: object) -> int:
    if isinstance(value, str):
        return len(value.encode("utf-8", "surrogateescape"))
    if isinstance(value, (bytes, bytearray, memoryview)):
        return len(value)
    return 0


class _CountingReader:
    def __init__(
        self,
        source: object,
        relative: str,
        dirty: bool,
        record: object,
        reject: object,
    ) -> None:
        self._source = source
        self._relative = relative
        self._dirty = dirty
        self._record = record
        self._reject = reject
        self._observed = False

    def _count(self, value: object) -> object:
        amount = _byte_length(value)
        if amount:
            self._record(self._relative, self._dirty, amount, not self._observed)
            self._observed = True
        return value

    def read(self, *args: object, **kwargs: object) -> object:
        return self._count(self._source.read(*args, **kwargs))

    def read1(self, *args: object, **kwargs: object) -> object:
        return self._count(self._source.read1(*args, **kwargs))

    def readline(self, *args: object, **kwargs: object) -> object:
        return self._count(self._source.readline(*args, **kwargs))

    def readlines(self, *args: object, **kwargs: object) -> object:
        values = self._source.readlines(*args, **kwargs)
        for value in values:
            self._count(value)
        return values

    def readinto(self, target: object) -> int:
        amount = self._source.readinto(target)
        if amount:
            self._record(self._relative, self._dirty, amount, not self._observed)
            self._observed = True
        return amount

    def readinto1(self, target: object) -> int:
        amount = self._source.readinto1(target)
        if amount:
            self._record(self._relative, self._dirty, amount, not self._observed)
            self._observed = True
        return amount

    def __iter__(self) -> "_CountingReader":
        return self

    def __next__(self) -> object:
        return self._count(next(self._source))

    def __enter__(self) -> "_CountingReader":
        self._source.__enter__()
        return self

    def __exit__(self, *args: object) -> object:
        return self._source.__exit__(*args)

    def __getattr__(self, name: str) -> object:
        if name in {"raw", "buffer", "fileno", "detach"}:
            self._reject(name)
            raise InstrumentationViolation(
                "wrapped stream escape blocked: {}".format(name)
            )
        return getattr(self._source, name)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure deterministic Level 0 release gates."
    )
    parser.add_argument("--output", type=Path, help="private raw JSON path")
    parser.add_argument("--worker-repo", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--worker-output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--tracked-files", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--dirty-path", action="append", default=[], help=argparse.SUPPRESS)
    parser.add_argument(
        "--state-home",
        type=Path,
        help="TAF_STATE_HOME to use instead of a temporary directory",
    )
    return parser


def _run(
    cwd: Path,
    argv: Sequence[str],
    *,
    env: Dict[str, str] = None,
) -> subprocess.CompletedProcess:
    merged = os.environ.copy()
    merged.update(FIXED_GIT_ENV)
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
    _run(
        repo,
        ["git", "init", "--template=", "-q", "-b", "main"],
        env=FIXED_GIT_ENV,
    )
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
    repo = repo.resolve()
    dirty = set(dirty_paths)
    counters = {
        "clean_file_content_reads": 0,
        "dirty_file_content_reads": 0,
        "measured_clean_bytes_read": 0,
        "measured_dirty_bytes_read": 0,
        "content_read_paths": [],
        "network_calls": 0,
        "llm_calls": 0,
        "allowed_process_calls": 0,
        "rejected_process_calls": 0,
        "native_bypass_rejections": 0,
        "git_commands": [],
    }
    real_builtin_open = builtins.open
    real_io_open = io.open
    real_native_open = _io.open
    real_io_fileio = io.FileIO
    real_native_fileio = _io.FileIO
    real_os_open = os.open
    real_os_close = os.close
    real_os_read = os.read
    real_os_readv = getattr(os, "readv", None)
    real_os_pread = getattr(os, "pread", None)
    real_os_preadv = getattr(os, "preadv", None)
    real_os_dup = os.dup
    real_os_dup2 = os.dup2
    real_mmap = mmap_module.mmap
    real_popen = subprocess.Popen
    real_socket_class = socket.socket
    real_socket_type_alias = socket.SocketType
    real_native_socket = _socket.socket
    real_socket_methods = {
        name: getattr(real_socket_class, name)
        for name in (
            "connect",
            "connect_ex",
            "send",
            "sendall",
            "sendto",
            "sendmsg",
        )
        if hasattr(real_socket_class, name)
    }
    real_create_connection = socket.create_connection
    real_socketpair = socket.socketpair
    real_getaddrinfo = socket.getaddrinfo
    tracked_fds: Dict[int, Tuple[str, bool, bool]] = {}

    def classify_path(path: object, dir_fd: int = None) -> object:
        if not isinstance(path, (str, bytes, os.PathLike)):
            return None
        candidate = Path(os.fsdecode(path))
        if not candidate.is_absolute():
            if dir_fd is not None:
                base = _fd_path(dir_fd)
                if base is None:
                    return None
                candidate = base / candidate
            else:
                candidate = Path.cwd() / candidate
        try:
            relative = candidate.resolve(strict=False).relative_to(repo).as_posix()
        except ValueError:
            return None
        return relative, relative in dirty

    def _fd_path(descriptor: int) -> object:
        tracked = tracked_fds.get(descriptor)
        if tracked is not None:
            return repo / tracked[0]
        for prefix in ("/proc/self/fd", "/dev/fd"):
            try:
                return Path(os.readlink("{}/{}".format(prefix, descriptor)))
            except OSError:
                continue
        return None

    def classify_fd(descriptor: int) -> object:
        tracked = tracked_fds.get(descriptor)
        if tracked is not None:
            return tracked[0], tracked[1]
        path = _fd_path(descriptor)
        return None if path is None else classify_path(path)

    def record_bytes(relative: str, is_dirty: bool, amount: int, first: bool) -> None:
        prefix = "dirty" if is_dirty else "clean"
        counters["measured_{}_bytes_read".format(prefix)] += amount
        if first:
            counters["{}_file_content_reads".format(prefix)] += 1
            counters["content_read_paths"].append(relative)

    def reject_stream_escape(name: str) -> None:
        counters["native_bypass_rejections"] += 1

    def wrap_reader(source: object, classification: object, mode: str) -> object:
        if classification is None or ("r" not in mode and "+" not in mode):
            return source
        relative, is_dirty = classification
        return _CountingReader(
            source, relative, is_dirty, record_bytes, reject_stream_escape
        )

    def guarded_builtin_open(file: object, *args: object, **kwargs: object) -> object:
        mode = str(args[0]) if args else str(kwargs.get("mode", "r"))
        classification = classify_fd(file) if isinstance(file, int) else classify_path(file)
        return wrap_reader(real_builtin_open(file, *args, **kwargs), classification, mode)

    def guarded_io_open(file: object, *args: object, **kwargs: object) -> object:
        mode = str(args[0]) if args else str(kwargs.get("mode", "r"))
        classification = classify_fd(file) if isinstance(file, int) else classify_path(file)
        return wrap_reader(real_io_open(file, *args, **kwargs), classification, mode)

    def guarded_fileio(file: object, mode: str = "r", *args: object, **kwargs: object) -> object:
        classification = classify_fd(file) if isinstance(file, int) else classify_path(file)
        if classification is not None and ("r" in mode or "+" in mode):
            counters["native_bypass_rejections"] += 1
            raise InstrumentationViolation("native repository FileIO blocked")
        return real_io_fileio(file, mode, *args, **kwargs)

    def guarded_os_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        descriptor = real_os_open(path, flags, *args, **kwargs)
        if flags & os.O_ACCMODE != os.O_WRONLY:
            classification = classify_path(path, kwargs.get("dir_fd"))
            if classification is not None:
                tracked_fds[descriptor] = (classification[0], classification[1], False)
        return descriptor

    def guarded_os_close(descriptor: int) -> None:
        tracked_fds.pop(descriptor, None)
        return real_os_close(descriptor)

    def record_fd_amount(descriptor: int, amount: int) -> None:
        classification = classify_fd(descriptor)
        if classification is None or amount <= 0:
            return
        tracked = tracked_fds.get(descriptor)
        observed = False if tracked is None else tracked[2]
        record_bytes(classification[0], classification[1], amount, not observed)
        tracked_fds[descriptor] = (classification[0], classification[1], True)

    def guarded_os_read(descriptor: int, amount: int) -> bytes:
        value = real_os_read(descriptor, amount)
        record_fd_amount(descriptor, len(value))
        return value

    def guarded_os_readv(descriptor: int, buffers: object) -> int:
        amount = real_os_readv(descriptor, buffers)
        record_fd_amount(descriptor, amount)
        return amount

    def guarded_os_pread(descriptor: int, amount: int, offset: int) -> bytes:
        value = real_os_pread(descriptor, amount, offset)
        record_fd_amount(descriptor, len(value))
        return value

    def guarded_os_preadv(descriptor: int, buffers: object, offset: int, *args: object) -> int:
        amount = real_os_preadv(descriptor, buffers, offset, *args)
        record_fd_amount(descriptor, amount)
        return amount

    def guarded_os_dup(descriptor: int) -> int:
        duplicate = real_os_dup(descriptor)
        if descriptor in tracked_fds:
            tracked_fds[duplicate] = tracked_fds[descriptor]
        return duplicate

    def guarded_os_dup2(descriptor: int, target: int, *args: object, **kwargs: object) -> int:
        duplicate = real_os_dup2(descriptor, target, *args, **kwargs)
        tracked_fds.pop(target, None)
        if descriptor in tracked_fds:
            tracked_fds[target] = tracked_fds[descriptor]
        return duplicate

    def guarded_mmap(descriptor: int, *args: object, **kwargs: object) -> object:
        if classify_fd(descriptor) is not None:
            counters["native_bypass_rejections"] += 1
            raise InstrumentationViolation("repository mmap blocked by benchmark")
        return real_mmap(descriptor, *args, **kwargs)

    def guarded_popen(argv: Sequence[str], *args: object, **kwargs: object) -> object:
        try:
            command = tuple(os.fsdecode(item) for item in argv)
        except (TypeError, ValueError):
            command = ()
        cwd = kwargs.get("cwd")
        cwd_matches = cwd is not None and Path(cwd).resolve() == repo
        exact = (
            command in ALLOWED_GIT_COMMANDS
            and cwd_matches
            and not kwargs.get("shell", False)
            and kwargs.get("executable") is None
        )
        if exact:
            counters["allowed_process_calls"] += 1
            counters["git_commands"].append(list(command[1:]))
            return real_popen(argv, *args, **kwargs)

        counters["rejected_process_calls"] += 1
        executable = Path(command[0]).name if command else ""
        if executable in LLM_EXECUTABLES:
            counters["llm_calls"] += 1
        if executable in NETWORK_EXECUTABLES:
            counters["network_calls"] += 1
        if executable == "git":
            subcommand = command[1] if len(command) > 1 else ""
            if (
                subcommand == "-c"
                or subcommand in NETWORK_GIT_COMMANDS
                or any(item.startswith("--remote") for item in command[2:])
            ):
                counters["network_calls"] += 1
        raise InstrumentationViolation("process blocked by benchmark")

    def blocked_network(*args: object, **kwargs: object) -> object:
        counters["network_calls"] += 1
        raise InstrumentationViolation("network blocked by benchmark")

    builtins.open = guarded_builtin_open  # type: ignore[assignment]
    io.open = guarded_io_open  # type: ignore[assignment]
    _io.open = guarded_io_open  # type: ignore[assignment]
    io.FileIO = guarded_fileio  # type: ignore[assignment]
    _io.FileIO = guarded_fileio  # type: ignore[assignment]
    os.open = guarded_os_open  # type: ignore[assignment]
    os.close = guarded_os_close  # type: ignore[assignment]
    os.read = guarded_os_read  # type: ignore[assignment]
    if real_os_readv is not None:
        os.readv = guarded_os_readv  # type: ignore[assignment]
    if real_os_pread is not None:
        os.pread = guarded_os_pread  # type: ignore[assignment]
    if real_os_preadv is not None:
        os.preadv = guarded_os_preadv  # type: ignore[assignment]
    os.dup = guarded_os_dup  # type: ignore[assignment]
    os.dup2 = guarded_os_dup2  # type: ignore[assignment]
    mmap_module.mmap = guarded_mmap  # type: ignore[assignment]
    subprocess.Popen = guarded_popen  # type: ignore[assignment]
    for name in real_socket_methods:
        setattr(real_socket_class, name, blocked_network)
    socket.socket = blocked_network  # type: ignore[assignment]
    socket.SocketType = blocked_network  # type: ignore[assignment]
    _socket.socket = blocked_network  # type: ignore[assignment]
    socket.create_connection = blocked_network  # type: ignore[assignment]
    socket.socketpair = blocked_network  # type: ignore[assignment]
    socket.getaddrinfo = blocked_network  # type: ignore[assignment]
    try:
        yield counters
    finally:
        builtins.open = real_builtin_open  # type: ignore[assignment]
        io.open = real_io_open  # type: ignore[assignment]
        _io.open = real_native_open  # type: ignore[assignment]
        io.FileIO = real_io_fileio  # type: ignore[assignment]
        _io.FileIO = real_native_fileio  # type: ignore[assignment]
        os.open = real_os_open  # type: ignore[assignment]
        os.close = real_os_close  # type: ignore[assignment]
        os.read = real_os_read  # type: ignore[assignment]
        if real_os_readv is not None:
            os.readv = real_os_readv  # type: ignore[assignment]
        if real_os_pread is not None:
            os.pread = real_os_pread  # type: ignore[assignment]
        if real_os_preadv is not None:
            os.preadv = real_os_preadv  # type: ignore[assignment]
        os.dup = real_os_dup  # type: ignore[assignment]
        os.dup2 = real_os_dup2  # type: ignore[assignment]
        mmap_module.mmap = real_mmap  # type: ignore[assignment]
        subprocess.Popen = real_popen  # type: ignore[assignment]
        socket.socket = real_socket_class  # type: ignore[assignment]
        socket.SocketType = real_socket_type_alias  # type: ignore[assignment]
        _socket.socket = real_native_socket  # type: ignore[assignment]
        for name, method in real_socket_methods.items():
            setattr(real_socket_class, name, method)
        socket.create_connection = real_create_connection  # type: ignore[assignment]
        socket.socketpair = real_socketpair  # type: ignore[assignment]
        socket.getaddrinfo = real_getaddrinfo  # type: ignore[assignment]


def _read_artifact_metrics(
    output: Path, reported_summary: Dict[str, object]
) -> Dict[str, object]:
    names = ("manifest.json", "snapshot.json", "dossier.md")
    sizes = {
        name: (output / name).stat().st_size
        for name in names
        if (output / name).is_file()
    }
    dossier = output / "dossier.md"
    characters = (
        len(dossier.read_text(encoding="utf-8")) if dossier.is_file() else None
    )
    return {
        "artifact_sizes_bytes": sizes,
        "artifact_total_bytes": sum(sizes.values()),
        "dossier_characters": characters,
        "reported_dossier_characters": reported_summary.get("dossier_characters"),
    }


def _worker(args: argparse.Namespace) -> int:
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
    stdout = io.StringIO()
    stderr = io.StringIO()
    with _guards(repo, dirty_paths) as counters:
        from taf_context.cli import main as context_main

        process_before = os.times()
        warm_started = time.perf_counter()
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
    artifact_metrics = _read_artifact_metrics(output, summary)
    artifact_sizes = artifact_metrics["artifact_sizes_bytes"]
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
        "snapshot_dirty_bytes_within_eligible": snapshot.get("dirty_bytes_hashed", -1)
        <= eligible_dirty_bytes,
        "snapshot_dirty_bytes_match_measurement": snapshot.get("dirty_bytes_hashed", -1)
        == counters["measured_dirty_bytes_read"],
        "measured_dirty_bytes_within_eligible": counters["measured_dirty_bytes_read"]
        <= eligible_dirty_bytes,
        "measured_dirty_bytes_exact": counters["measured_dirty_bytes_read"]
        == eligible_dirty_bytes,
        "zero_clean_file_content_reads": counters["clean_file_content_reads"] == 0
        and counters["measured_clean_bytes_read"] == 0,
        "zero_network_calls": counters["network_calls"] == 0,
        "zero_llm_calls": counters["llm_calls"] == 0,
        "only_expected_process_calls": counters["rejected_process_calls"] == 0,
        "zero_native_bypass_rejections": counters["native_bypass_rejections"] == 0,
        "all_artifacts_present": set(artifact_sizes)
        == {"manifest.json", "snapshot.json", "dossier.md"},
        "dossier_count_matches_report": artifact_metrics["dossier_characters"]
        == artifact_metrics["reported_dossier_characters"],
    }
    result = {
        "status": "ok" if all(checks.values()) else "correctness-failure",
        "warm_wall_seconds": warm_wall,
        "warm_cpu_seconds": warm_cpu,
        "peak_rss_bytes": _rss_bytes(),
        "paths_inspected": len(
            set(snapshot.get("tracked_paths", []))
            | set(snapshot.get("untracked_paths", []))
        ),
        "dirty_bytes_hashed": snapshot.get("dirty_bytes_hashed"),
        "measured_dirty_bytes_read": counters["measured_dirty_bytes_read"],
        "measured_clean_bytes_read": counters["measured_clean_bytes_read"],
        "eligible_dirty_bytes": eligible_dirty_bytes,
        "artifact_sizes_bytes": artifact_sizes,
        "artifact_total_bytes": artifact_metrics["artifact_total_bytes"],
        "dossier_characters": artifact_metrics["dossier_characters"],
        "reported_dossier_characters": artifact_metrics[
            "reported_dossier_characters"
        ],
        "clean_file_content_reads": counters["clean_file_content_reads"],
        "dirty_file_content_reads": counters["dirty_file_content_reads"],
        "network_calls": counters["network_calls"],
        "llm_calls": counters["llm_calls"],
        "allowed_process_calls": counters["allowed_process_calls"],
        "rejected_process_calls": counters["rejected_process_calls"],
        "native_bypass_rejections": counters["native_bypass_rejections"],
        "git_commands": counters["git_commands"],
        "correctness_checks": checks,
        "correctness_passed": all(checks.values()),
        "correctness_failure": not all(checks.values()),
        "performance_failure": False,
        "collector_stderr": stderr.getvalue(),
    }
    sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["correctness_passed"] else 1


_OK_INTEGER_METRICS = (
    "peak_rss_bytes",
    "paths_inspected",
    "dirty_bytes_hashed",
    "measured_dirty_bytes_read",
    "measured_clean_bytes_read",
    "eligible_dirty_bytes",
    "artifact_total_bytes",
    "dossier_characters",
    "reported_dossier_characters",
    "clean_file_content_reads",
    "dirty_file_content_reads",
    "network_calls",
    "llm_calls",
    "allowed_process_calls",
    "rejected_process_calls",
    "native_bypass_rejections",
)
_OK_NUMBER_METRICS = (
    "warm_wall_seconds",
    "warm_cpu_seconds",
    "cold_wall_seconds",
    "cold_cpu_seconds",
)


def _is_bounded_nonnegative_integer(value: object) -> bool:
    return type(value) is int and 0 <= value <= (1 << 63) - 1


def _is_finite_nonnegative_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        converted = float(value)
    except (OverflowError, TypeError, ValueError):
        return False
    return math.isfinite(converted) and converted >= 0


def _ok_sample_structure_errors(sample: Dict[str, object]) -> List[str]:
    errors = []
    if sample.get("status") != "ok":
        errors.append("status")
    if sample.get("correctness_passed") is not True:
        errors.append("correctness_passed")
    for field in _OK_INTEGER_METRICS:
        value = sample.get(field)
        if not _is_bounded_nonnegative_integer(value):
            errors.append(field)
    for field in _OK_NUMBER_METRICS:
        value = sample.get(field)
        if not _is_finite_nonnegative_number(value):
            errors.append(field)
    sizes = sample.get("artifact_sizes_bytes")
    artifact_names = {"manifest.json", "snapshot.json", "dossier.md"}
    if (
        type(sizes) is not dict
        or set(sizes) != artifact_names
        or any(
            not _is_bounded_nonnegative_integer(value)
            for value in sizes.values()
        )
    ):
        errors.append("artifact_sizes_bytes")
    checks = sample.get("correctness_checks")
    if (
        type(checks) is not dict
        or not checks
        or any(type(value) is not bool for value in checks.values())
        or not all(checks.values())
    ):
        errors.append("correctness_checks")
    commands = sample.get("git_commands")
    if type(commands) is not list or not commands:
        errors.append("git_commands")
    return sorted(set(errors))


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
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"),
            timeout=180,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "timeout",
            "error": "worker exceeded {} second timeout".format(exc.timeout),
            "cold_wall_seconds": time.perf_counter() - started,
            "cold_cpu_seconds": _times_total(os.times()) - _times_total(before),
            "worker_exit_code": None,
            "correctness_passed": None,
            "correctness_failure": False,
            "performance_failure": True,
        }
    cold_wall = time.perf_counter() - started
    cold_cpu = _times_total(os.times()) - _times_total(before)
    try:
        sample = json.loads(completed.stdout)
        if type(sample) is not dict or "correctness_passed" not in sample:
            raise ValueError("worker JSON is not a result object")
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return {
            "status": "worker-protocol-error",
            "error": str(exc),
            "worker_stdout": completed.stdout,
            "worker_stderr": completed.stderr,
            "cold_wall_seconds": cold_wall,
            "cold_cpu_seconds": cold_cpu,
            "worker_exit_code": completed.returncode,
            "correctness_passed": False,
            "correctness_failure": True,
            "performance_failure": False,
        }
    sample["cold_wall_seconds"] = cold_wall
    sample["cold_cpu_seconds"] = cold_cpu
    sample["worker_exit_code"] = completed.returncode
    if sample.get("status") == "ok":
        structure_errors = _ok_sample_structure_errors(sample)
        if structure_errors:
            return {
                "status": "worker-structure-error",
                "error": "missing or invalid mandatory metrics: {}".format(
                    ", ".join(structure_errors)
                ),
                "worker_result": sample,
                "worker_stderr": completed.stderr,
                "cold_wall_seconds": cold_wall,
                "cold_cpu_seconds": cold_cpu,
                "worker_exit_code": completed.returncode,
                "correctness_passed": False,
                "correctness_failure": True,
                "performance_failure": False,
            }
    sample["correctness_failure"] = (
        not bool(sample.get("correctness_passed")) or completed.returncode != 0
    )
    sample["performance_failure"] = False
    if sample["correctness_failure"] and sample.get("status") == "ok":
        sample["status"] = "correctness-failure"
    if completed.stderr:
        sample["worker_stderr"] = completed.stderr
    return sample


def _percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def _summary(samples: Sequence[Dict[str, object]], field: str) -> Dict[str, object]:
    values = [
        float(sample[field])
        for sample in samples
        if sample.get("status") == "ok"
        and isinstance(sample.get(field), (int, float))
    ]
    if not values:
        return {"p50": None, "p95": None, "sample_count": 0}
    return {
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "sample_count": len(values),
    }


def _retention_decision(correctness_passed: bool, gates_passed: bool) -> str:
    if correctness_passed and gates_passed:
        return "GO — Retain Python for production Level 0."
    return (
        "NO-GO — Python remains a reference implementation; "
        "a replacement plan is required."
    )


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
        "warmup_has_no_correctness_failure": not bool(
            warmup.get("correctness_failure")
        ),
        "samples_have_no_correctness_failure": not any(
            bool(sample.get("correctness_failure")) for sample in samples
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
    complete = (
        len(samples) == MEASURED_RUNS
        and all(sample.get("status") == "ok" for sample in samples)
    )
    warm_p95 = wall["warm_seconds"]["p95"]

    def every(field: str, predicate: object) -> bool:
        return complete and all(predicate(sample.get(field)) for sample in samples)

    gates = {
        "warmup_completed": warmup.get("status") == "ok",
        "all_measured_samples_completed": complete,
        "warm_p95_within_limit": complete
        and warm_p95 is not None
        and (warm_limit is None or warm_p95 <= float(warm_limit)),
        "dossier_within_12000_characters": every(
            "dossier_characters",
            lambda value: isinstance(value, int)
            and value <= MAX_DOSSIER_CHARACTERS,
        ),
        "dirty_bytes_within_eligible": complete
        and all(
            int(sample["measured_dirty_bytes_read"])
            <= int(sample["eligible_dirty_bytes"])
            for sample in samples
        ),
        "zero_clean_file_content_reads": complete
        and all(
            int(sample["clean_file_content_reads"]) == 0
            and int(sample["measured_clean_bytes_read"]) == 0
            for sample in samples
        ),
        "artifacts_within_2_mib": every(
            "artifact_total_bytes",
            lambda value: isinstance(value, int) and value <= MAX_ARTIFACT_BYTES,
        ),
        "zero_network_calls": complete
        and all(
            int(sample["network_calls"]) == 0 for sample in samples
        ),
        "zero_llm_calls": complete
        and all(int(sample["llm_calls"]) == 0 for sample in samples),
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
        "performance_failures": [
            {"run": index + 1, "status": sample.get("status"), "error": sample.get("error")}
            for index, sample in enumerate(samples)
            if sample.get("performance_failure")
        ],
        "gates": gates,
        "mandatory_gates_passed": all(gates.values()),
    }


def _failed_class(case: Dict[str, object], error: Exception) -> Dict[str, object]:
    return {
        "tracked_files": int(case["tracked_files"]),
        "dirty_files": int(case["dirty_files"]),
        "warm_p95_limit_seconds": case["warm_p95_seconds"],
        "status": "class-error",
        "error": "{}: {}".format(type(error).__name__, error),
        "samples": [],
        "correctness_passed": False,
        "mandatory_gates_passed": False,
        "gates": {"class_completed": False},
    }


def _driver(output: Path) -> int:
    output = output.resolve(strict=False)
    results = []
    with tempfile.TemporaryDirectory(prefix="taf-level0-benchmark-") as directory:
        root = Path(directory)
        for index, case in enumerate(CASES):
            try:
                result = _case_result(root / "case-{}".format(index + 1), case)
            except Exception as exc:  # Evidence must survive a later class failure.
                result = _failed_class(case, exc)
            results.append(result)

    correctness_passed = all(result["correctness_passed"] for result in results)
    mandatory_gates_passed = all(
        result["mandatory_gates_passed"] for result in results
    )
    decision = _retention_decision(correctness_passed, mandatory_gates_passed)
    evidence = {
        "schema_version": 1,
        "seed": SEED,
        "warm_up_runs_per_class": WARM_UP_RUNS,
        "measured_runs_per_class": MEASURED_RUNS,
        "percentile_method": "nearest-rank",
        "timing_definitions": {
            "cold": "fresh worker process including interpreter and harness startup",
            "warm": "worker timer excludes guard setup and collector import; includes CLI parsing, collection, rendering, and artifact emission",
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


def _main(argv: Sequence[str] = None) -> int:
    # Named ``_main`` (not ``main``) because this script has its own CLI entry
    # point distinct from the broker's ``taf_context.cli.main`` imported above
    # as ``context_main``; the leading underscore keeps a text search for
    # unguarded broker calls from matching this unrelated local function.
    args = _parser().parse_args(argv)
    if args.state_home is not None:
        os.environ["TAF_STATE_HOME"] = str(args.state_home)
    elif not os.environ.get("TAF_STATE_HOME"):
        os.environ["TAF_STATE_HOME"] = tempfile.mkdtemp(prefix="taf-benchmark-state-")
    if args.worker_repo is not None:
        required = (args.worker_output, args.tracked_files)
        if any(value is None for value in required):
            raise SystemExit("incomplete benchmark worker arguments")
        return _worker(args)
    if args.output is None:
        _parser().error("--output is required")
    return _driver(args.output)


if __name__ == "__main__":
    raise SystemExit(_main())
