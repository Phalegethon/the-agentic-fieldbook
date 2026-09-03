"""Collect bounded, local-only repository metadata for Level 0 context."""

from __future__ import annotations

import collections
import errno
import hashlib
import os
import re
import stat
import subprocess
import threading
from pathlib import Path, PurePosixPath
from typing import Callable

from .models import BackgroundState, ContextManifest, RepositorySnapshot


_GIT_TIMEOUT_SECONDS = 20
_HASH_CHUNK_BYTES = 1024 * 1024
_OBJECT_ID = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")
_INDEX_MODES = {b"100644", b"100755", b"120000", b"160000"}

_GENERATED_OR_VENDORED_SEGMENTS = {
    "vendor",
    "vendors",
    "third_party",
    "node_modules",
    "dist",
    "build",
    "generated",
    "coverage",
    "target",
    "out",
    ".cache",
    "__pycache__",
    ".venv",
    "venv",
    "pods",
}

_BINARY_SUFFIXES = {
    ".7z", ".bin", ".bz2", ".class", ".dat", ".db", ".dll", ".dylib",
    ".exe", ".gif", ".gz", ".ico", ".jar", ".jpeg", ".jpg", ".mov",
    ".mp3", ".mp4", ".otf", ".pdf", ".png", ".pyc", ".so",
    ".sqlite", ".tar", ".tgz", ".ttf", ".war", ".webp", ".woff",
    ".woff2", ".xz", ".zip",
}
_CREDENTIAL_NAMES = {
    ".env", "credentials", "credentials.json", "id_dsa", "id_ecdsa",
    "id_ed25519", "id_rsa", "secrets.json",
}
_CREDENTIAL_SUFFIXES = {".jks", ".key", ".keystore", ".p12", ".pem", ".pfx"}

_LANGUAGES = {
    ".bash": "Shell",
    ".c": "C",
    ".cc": "C++",
    ".cfg": "Configuration",
    ".cpp": "C++",
    ".cs": "C#",
    ".css": "CSS",
    ".cxx": "C++",
    ".fish": "Shell",
    ".go": "Go",
    ".gradle": "Gradle",
    ".h": "C",
    ".hpp": "C++",
    ".html": "HTML",
    ".ini": "Configuration",
    ".java": "Java",
    ".js": "JavaScript",
    ".json": "JSON",
    ".jsx": "JavaScript",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".md": "Markdown",
    ".markdown": "Markdown",
    ".php": "PHP",
    ".ps1": "PowerShell",
    ".py": "Python",
    ".pyi": "Python",
    ".rb": "Ruby",
    ".rs": "Rust",
    ".rst": "reStructuredText",
    ".scala": "Scala",
    ".scss": "SCSS",
    ".sh": "Shell",
    ".sql": "SQL",
    ".svelte": "Svelte",
    ".swift": "Swift",
    ".toml": "TOML",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".txt": "Text",
    ".vue": "Vue",
    ".xml": "XML",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".zsh": "Shell",
}

_INCLUDE_RULES = (
    "git-tracked-files",
    "git-untracked-files-exclude-standard",
)
_EXCLUDE_RULES = ("git-ignored-entries",)


class SnapshotError(RuntimeError):
    """Raised when bounded repository metadata cannot be collected safely."""


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "7",
            "GIT_CONFIG_KEY_0": "core.fsmonitor",
            "GIT_CONFIG_VALUE_0": "false",
            "GIT_CONFIG_KEY_1": "diff.external",
            "GIT_CONFIG_VALUE_1": "",
            "GIT_CONFIG_KEY_2": "core.hooksPath",
            "GIT_CONFIG_VALUE_2": os.devnull,
            "GIT_CONFIG_KEY_3": "protocol.allow",
            "GIT_CONFIG_VALUE_3": "never",
            "GIT_CONFIG_KEY_4": "credential.helper",
            "GIT_CONFIG_VALUE_4": "",
            "GIT_CONFIG_KEY_5": "diff.renames",
            "GIT_CONFIG_VALUE_5": "false",
            "GIT_CONFIG_KEY_6": "status.renames",
            "GIT_CONFIG_VALUE_6": "false",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    environment.pop("GIT_EXTERNAL_DIFF", None)
    return environment


def _git(repo: Path, *args: str, allow_failure: bool = False) -> bytes | None:
    command = ["git", *args]
    if args and args[0] == "diff":
        command[2:2] = ["--no-ext-diff", "--no-textconv"]
    try:
        result = subprocess.run(
            command,
            cwd=repo,
            env=_git_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SnapshotError("local Git command failed") from exc
    if result.returncode != 0:
        if allow_failure:
            return None
        raise SnapshotError("local Git command failed")
    return result.stdout


def _text(raw: bytes | None, field: str, *, empty_ok: bool = False) -> str:
    if raw is None:
        raise SnapshotError(f"malformed Git output: {field}")
    if not raw.endswith(b"\n"):
        raise SnapshotError(f"malformed Git output: {field}")
    try:
        value = raw[:-1].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SnapshotError(f"malformed Git output: {field}") from exc
    if not value and not empty_ok:
        raise SnapshotError(f"malformed Git output: {field}")
    if "\x00" in value or "\n" in value:
        raise SnapshotError(f"malformed Git output: {field}")
    return value


def _path_text(raw: bytes, field: str) -> str:
    try:
        value = raw.decode("utf-8", "surrogateescape")
    except UnicodeDecodeError as exc:  # pragma: no cover - surrogateescape is total
        raise SnapshotError(f"malformed Git output: {field}") from exc
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise SnapshotError(f"malformed Git output: {field}")
    return value


def _z_paths(raw: bytes | None, field: str) -> tuple[str, ...]:
    if raw is None or (raw and not raw.endswith(b"\x00")):
        raise SnapshotError(f"malformed Git output: {field}")
    if not raw:
        return ()
    return tuple(
        sorted({_path_text(item, field) for item in raw[:-1].split(b"\x00")})
    )


def _status_paths(raw: bytes | None) -> tuple[dict[str, str], int]:
    if raw is None or (raw and not raw.endswith(b"\x00")):
        raise SnapshotError("malformed Git output: status")
    fields = [] if not raw else raw[:-1].split(b"\x00")
    statuses: dict[str, str] = {}
    ignored = 0
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if len(record) < 4 or record[2:3] != b" ":
            raise SnapshotError("malformed Git output: status")
        try:
            flags = record[:2].decode("ascii")
        except UnicodeDecodeError as exc:
            raise SnapshotError("malformed Git output: status") from exc
        path = _path_text(record[3:], "status")
        if flags == "!!":
            ignored += 1
        else:
            statuses[path] = flags
        if flags[0] in "RC" or flags[1] in "RC":
            if index >= len(fields):
                raise SnapshotError("malformed Git output: status")
            _path_text(fields[index], "status")
            index += 1
    return statuses, ignored


def _index_gitlinks(raw: bytes | None) -> dict[str, tuple[str, ...]]:
    if raw is None or (raw and not raw.endswith(b"\x00")):
        raise SnapshotError("malformed Git output: index entries")
    gitlinks: dict[str, list[str]] = {}
    seen_entries: set[tuple[str, bytes]] = set()
    fields = [] if not raw else raw[:-1].split(b"\x00")
    for record in fields:
        header, separator, path_raw = record.partition(b"\t")
        parts = header.split(b" ")
        if not separator or len(parts) != 3:
            raise SnapshotError("malformed Git output: index entries")
        mode, object_id, stage = parts
        if mode not in _INDEX_MODES or stage not in {b"0", b"1", b"2", b"3"}:
            raise SnapshotError("malformed Git output: index entries")
        try:
            value = object_id.decode("ascii")
        except UnicodeDecodeError as exc:
            raise SnapshotError("malformed Git output: index entries") from exc
        if not _OBJECT_ID.fullmatch(value):
            raise SnapshotError("malformed Git output: index entries")
        path = _path_text(path_raw, "index entries")
        entry_key = (path, stage)
        if entry_key in seen_entries:
            raise SnapshotError("malformed Git output: index entries")
        seen_entries.add(entry_key)
        if mode == b"160000":
            gitlinks.setdefault(path, []).append(
                f"160000:{value.lower()}:{stage.decode('ascii')}"
            )
    return {
        path: tuple(sorted(entries, key=lambda entry: entry.rsplit(":", 1)[1]))
        for path, entries in sorted(gitlinks.items())
    }


def _fingerprint(values: tuple[str, ...] | list[str]) -> str:
    digest = hashlib.sha256()
    for value in values:
        encoded = value.encode("utf-8", "surrogateescape")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"sha256:{digest.hexdigest()}"


def _object_ids(raw: bytes | None, field: str) -> tuple[str, ...]:
    if raw is None:
        raise SnapshotError(f"malformed Git output: {field}")
    try:
        values = raw.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise SnapshotError(f"malformed Git output: {field}") from exc
    if not values or any(not _OBJECT_ID.fullmatch(value) for value in values):
        raise SnapshotError(f"malformed Git output: {field}")
    return tuple(sorted(value.lower() for value in values))


def _resolve_common_dir(git_dir: Path, canonical_root: Path, value: str) -> Path:
    common = Path(value)
    if common.is_absolute():
        return common.resolve()
    if value == ".git" and git_dir.name == ".git":
        return git_dir
    resolved_from_git_dir = (git_dir / common).resolve()
    if resolved_from_git_dir.exists():
        return resolved_from_git_dir
    return (canonical_root / common).resolve()


def _excluded_dirty_category(relative: str) -> str | None:
    path = PurePosixPath(relative)
    lowered_parts = tuple(part.lower() for part in path.parts)
    name = path.name.lower()
    suffix = path.suffix.lower()
    if set(lowered_parts) & _GENERATED_OR_VENDORED_SEGMENTS:
        return "generated-or-vendored"
    if (
        name in _CREDENTIAL_NAMES
        or name.startswith(".env.")
        or suffix in _CREDENTIAL_SUFFIXES
    ):
        return "credential"
    if suffix in _BINARY_SUFFIXES:
        return "binary"
    return None


def _metadata_descriptor(prefix: str, metadata: os.stat_result) -> str:
    return f"{prefix}:{metadata.st_size}:{metadata.st_mtime_ns}"


def _open_regular_beneath(root: Path, relative: str) -> tuple[int, list[int]]:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
    if nofollow == 0:
        raise OSError(errno.ENOTSUP, "no-follow opens unavailable")
    descriptors: list[int] = []
    try:
        current = os.open(root, directory_flags)
        descriptors.append(current)
        parts = PurePosixPath(relative).parts
        for component in parts[:-1]:
            current = os.open(component, directory_flags, dir_fd=current)
            descriptors.append(current)
        leaf = os.open(parts[-1], os.O_RDONLY | nofollow, dir_fd=current)
        return leaf, descriptors
    except BaseException:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise


def _same_file_state(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_size,
        left.st_mtime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_size,
        right.st_mtime_ns,
    )


def _content_descriptor(
    root: Path,
    relative: str,
    max_dirty_file_bytes: int,
) -> tuple[str, int, bool, bool, str | None]:
    path = root / relative
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return "deleted", 0, False, True, None
    except OSError as exc:
        raise SnapshotError("dirty path metadata unavailable") from exc

    mode = metadata.st_mode
    if stat.S_ISLNK(mode):
        try:
            target = os.readlink(path)
        except OSError as exc:
            raise SnapshotError("dirty symlink metadata unavailable") from exc
        return f"symlink:{target}", 0, False, True, None
    if not stat.S_ISREG(mode):
        return f"unsupported:{mode:o}", 0, False, True, None
    excluded = _excluded_dirty_category(relative)
    if excluded == "binary" and metadata.st_size > max_dirty_file_bytes:
        excluded = None
    if metadata.st_size > max_dirty_file_bytes:
        return (
            f"oversized:{metadata.st_size}:{metadata.st_mtime_ns}",
            0,
            False,
            False,
            "dirty-fingerprint-incomplete",
        )
    if excluded is not None:
        return (
            _metadata_descriptor(f"excluded:{excluded}", metadata),
            0,
            excluded == "binary",
            False,
            f"dirty-{excluded}-content-excluded",
        )
    digest = hashlib.sha256()
    bytes_hashed = 0
    descriptor = -1
    parents: list[int] = []
    try:
        descriptor, parents = _open_regular_beneath(root, relative)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not _same_file_state(metadata, before):
            return (
                _metadata_descriptor("unsafe", before),
                0,
                False,
                False,
                "dirty-path-unsafe",
            )
        remaining = max_dirty_file_bytes
        while remaining:
            chunk = os.read(descriptor, min(_HASH_CHUNK_BYTES, remaining))
            if not chunk:
                break
            digest.update(chunk)
            bytes_hashed += len(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.ENOENT, errno.ENOTSUP}:
            return _metadata_descriptor("unsafe", metadata), 0, False, False, "dirty-path-unsafe"
        raise SnapshotError("dirty path content unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        for parent in reversed(parents):
            os.close(parent)
    if not _same_file_state(before, after):
        return (
            _metadata_descriptor("changed", after),
            bytes_hashed,
            False,
            False,
            "dirty-file-changed-during-read",
        )
    return (
        f"regular:{after.st_size}:sha256:{digest.hexdigest()}",
        bytes_hashed,
        False,
        True,
        None,
    )


def _language_counts(paths: tuple[str, ...]) -> tuple[tuple[str, int], ...]:
    counts: dict[str, int] = {}
    for path in paths:
        language = _LANGUAGES.get(PurePosixPath(path).suffix.lower(), "Other")
        counts[language] = counts.get(language, 0) + 1
    return tuple(sorted(counts.items()))


def _is_candidate_artifact(path: str) -> bool:
    normalized = path.lower()
    if "/" not in path and path in {"AGENTS.md", "CLAUDE.md"}:
        return True
    if "/" not in path and (normalized == "readme" or normalized.startswith("readme.")):
        return True
    if normalized.startswith("docs/superpowers/specs/"):
        return True
    if normalized.startswith("docs/superpowers/plans/"):
        return True
    return "test-result" in normalized or "benchmark-result" in normalized


def _run_concurrently(
    jobs: list[Callable[[], object]], workers: int = 4
) -> list[tuple[bool, object]]:
    """Run independent callables on a bounded number of threads.

    Each outcome is ``(True, value)`` or ``(False, exception)`` at the job's
    own index, so the caller consumes results in the original order and the
    first failing command still raises first. ``threading`` is used instead
    of ``concurrent.futures`` because the latter imports ``logging``, which
    costs about 7 ms on the query path.

    If the platform refuses to start a thread (``RuntimeError``, e.g. thread
    exhaustion), the jobs that were never picked up by a started worker are
    run sequentially in the current thread instead of letting the error
    escape — the sequential fallback can only ever raise ``SnapshotError``,
    the same as the normal concurrent path.
    """
    outcomes: list[tuple[bool, object]] = [(False, None)] * len(jobs)
    pending = collections.deque(range(len(jobs)))

    def worker() -> None:
        while True:
            try:
                index = pending.popleft()
            except IndexError:
                return
            try:
                outcomes[index] = (True, jobs[index]())
            except BaseException as exc:  # re-raised by the consumer in order
                outcomes[index] = (False, exc)

    threads = [
        threading.Thread(target=worker, name=f"taf-git-{ordinal}", daemon=True)
        for ordinal in range(max(1, min(workers, len(jobs))))
    ]
    started: list[threading.Thread] = []
    try:
        for thread in threads:
            thread.start()
            started.append(thread)
    except RuntimeError:
        # Some threads could not be started; drain whatever jobs are left in
        # the pending queue on the current thread rather than losing them.
        worker()
    for thread in started:
        thread.join()
    return outcomes


def _outcome(outcomes: list[tuple[bool, object]], index: int) -> bytes | None:
    ok, value = outcomes[index]
    if not ok:
        if value is None:
            raise SnapshotError("local Git command failed")
        raise value  # type: ignore[misc]
    return value  # type: ignore[return-value]


def _rev_parse_identity(repo: Path) -> tuple[str, str, bytes | None]:
    """Return the Git directory, the common directory value, and raw HEAD.

    One ``rev-parse`` answers all three; ``--git-common-dir`` prints a path
    relative to the current directory, so this runs from the canonical root
    exactly like the separate calls it replaces. The combined form can fail
    for reasons unrelated to HEAD (a rejected combination of arguments), so a
    failure does not by itself mean the repository is unborn: on the
    fallback path the two directory queries are repeated without
    ``--verify``, and HEAD is decided separately by the exact command the
    pre-branch sequential implementation used, so a combined-call failure
    can never be silently reported as an unborn repository.
    """
    combined = _git(
        repo, "rev-parse", "--absolute-git-dir", "--git-common-dir", "--verify", "HEAD",
        allow_failure=True,
    )
    expected_lines = 3
    if combined is None:
        combined = _git(repo, "rev-parse", "--absolute-git-dir", "--git-common-dir")
        expected_lines = 2
    if combined is None or not combined.endswith(b"\n"):
        raise SnapshotError("malformed Git output: repository identity")
    lines = combined[:-1].split(b"\n")
    if len(lines) != expected_lines:
        raise SnapshotError("malformed Git output: repository identity")
    git_dir = _text(lines[0] + b"\n", "Git directory")
    common_value = _text(lines[1] + b"\n", "Git common directory")
    if expected_lines == 3:
        raw_head = lines[2] + b"\n"
    else:
        raw_head = _git(repo, "rev-parse", "--verify", "HEAD", allow_failure=True)
    return git_dir, common_value, raw_head


def collect_snapshot(
    repo: Path, max_dirty_file_bytes: int = 8 * 1024 * 1024
) -> RepositorySnapshot:
    """Return deterministic Git metadata without fetching or reading clean files."""
    if (
        isinstance(max_dirty_file_bytes, bool)
        or not isinstance(max_dirty_file_bytes, int)
        or max_dirty_file_bytes < 0
    ):
        raise SnapshotError("invalid dirty file byte ceiling")
    repo = Path(repo)
    canonical_root = Path(
        _text(_git(repo, "rev-parse", "--show-toplevel"), "repository root")
    ).resolve()
    repo = canonical_root
    configured_filters = _git(
        repo,
        "config",
        "--local",
        "--includes",
        "--name-only",
        "--get-regexp",
        r"^filter\..*\.(clean|smudge|process)$",
        allow_failure=True,
    )
    if configured_filters is not None:
        raise SnapshotError("executable Git filters are not allowed")
    git_dir_value, common_value, raw_head = _rev_parse_identity(repo)
    git_dir = Path(git_dir_value).resolve()
    git_common_dir = _resolve_common_dir(git_dir, canonical_root, common_value)

    if raw_head is None:
        head_sha = None
    else:
        head_values = _object_ids(raw_head, "HEAD")
        if len(head_values) != 1:
            raise SnapshotError("malformed Git output: HEAD")
        head_sha = head_values[0]

    # Independent read-only commands run concurrently; results are consumed in
    # the original sequential order so parsing and error precedence do not
    # change. Command lines are identical to the sequential implementation.
    jobs: list[Callable[[], object]] = [
        lambda: _git(repo, "symbolic-ref", "--short", "-q", "HEAD", allow_failure=True),
        lambda: _git(repo, "ls-files", "-z"),
        lambda: _git(repo, "ls-files", "--stage", "-z"),
        lambda: _git(
            repo, "diff", "--no-renames", "--ignore-submodules=dirty", "--cached", "--name-only", "-z"
        ),
        lambda: _git(repo, "diff", "--no-renames", "--ignore-submodules=all", "--name-only", "-z"),
        lambda: _git(repo, "ls-files", "--others", "--exclude-standard", "-z"),
        lambda: _git(
            repo, "status", "--no-renames", "--ignore-submodules=all", "--porcelain=v1",
            "-z", "--ignored=matching", "--untracked-files=normal",
        ),
    ]
    if head_sha is not None:
        jobs.insert(0, lambda: _git(repo, "rev-list", "--max-parents=0", "HEAD"))
    outcomes = _run_concurrently(jobs)
    offset = 0
    if head_sha is None:
        root_commits: tuple[str, ...] = ()
    else:
        root_commits = _object_ids(_outcome(outcomes, 0), "root commits")
        offset = 1
    raw_branch = _outcome(outcomes, offset)
    branch = None if raw_branch is None else _text(raw_branch, "branch")
    tracked_paths = _z_paths(_outcome(outcomes, offset + 1), "tracked paths")
    index_gitlinks = _index_gitlinks(_outcome(outcomes, offset + 2))
    staged_paths = _z_paths(_outcome(outcomes, offset + 3), "staged paths")
    unstaged_paths = _z_paths(_outcome(outcomes, offset + 4), "unstaged paths")
    untracked_paths = _z_paths(_outcome(outcomes, offset + 5), "untracked paths")
    statuses, ignored_entry_count = _status_paths(_outcome(outcomes, offset + 6))

    dirty_paths = tuple(sorted(set(staged_paths + unstaged_paths + untracked_paths)))
    dirty_values: list[str] = []
    dirty_bytes_hashed = 0
    binary_file_count = 0
    oversized_file_count = 0
    dirty_complete = not bool(index_gitlinks)
    tracked_dirty_paths = set(staged_paths + unstaged_paths)
    warnings: set[str] = set()
    if index_gitlinks:
        warnings.add("submodule-worktree-state-uninspected")
    for path in dirty_paths:
        flags = statuses.get(path)
        if flags is None:
            flags = "??" if path in untracked_paths else "  "
        if path in index_gitlinks and path in staged_paths:
            descriptor = "gitlink:" + ",".join(index_gitlinks[path])
            bytes_hashed, binary, complete, warning = 0, False, True, None
        else:
            descriptor, bytes_hashed, binary, complete, warning = _content_descriptor(
                canonical_root, path, max_dirty_file_bytes
            )
        dirty_values.extend((path, flags, descriptor))
        dirty_bytes_hashed += bytes_hashed
        binary_file_count += int(binary)
        oversized_file_count += int(descriptor.startswith("oversized:"))
        dirty_complete = dirty_complete and complete
        if warning is not None:
            warnings.add(warning)
            if warning.startswith("dirty-") and warning.endswith("-content-excluded"):
                warnings.add("dirty-content-excluded")

    inventory_paths = tuple(sorted(set(tracked_paths + untracked_paths)))
    generated_or_vendored_count = sum(
        bool(set(PurePosixPath(path).parts) & _GENERATED_OR_VENDORED_SEGMENTS)
        for path in inventory_paths
    )
    candidate_artifacts = tuple(
        path for path in inventory_paths if _is_candidate_artifact(path)
    )
    provider_markers = tuple(
        path
        for path in inventory_paths
        if path
        in {".taf/context/registration.json", ".taf/context/manifest.json"}
    )
    insertions = deletions = 0
    if tracked_dirty_paths:
        warnings.add("dirty-diff-statistics-incomplete")

    return RepositorySnapshot(
        schema_version="1",
        repository_identity=_fingerprint(list(root_commits)),
        canonical_root=str(canonical_root),
        canonical_root_fingerprint=_fingerprint([str(canonical_root)]),
        git_dir=str(git_dir),
        git_common_dir=str(git_common_dir),
        git_common_dir_fingerprint=_fingerprint([str(git_common_dir)]),
        worktree_identity=_fingerprint([str(canonical_root), str(git_dir)]),
        head_sha=head_sha,
        branch=branch,
        dirty_fingerprint=_fingerprint(dirty_values),
        dirty_fingerprint_complete=dirty_complete,
        tracked_paths=tracked_paths,
        staged_paths=staged_paths,
        unstaged_paths=unstaged_paths,
        untracked_paths=untracked_paths,
        ignored_entry_count=ignored_entry_count,
        generated_or_vendored_count=generated_or_vendored_count,
        binary_file_count=binary_file_count,
        oversized_file_count=oversized_file_count,
        language_counts=_language_counts(inventory_paths),
        candidate_artifacts=candidate_artifacts,
        provider_markers=provider_markers,
        insertions=insertions,
        deletions=deletions,
        dirty_bytes_hashed=dirty_bytes_hashed,
        warnings=tuple(sorted(warnings)),
    )


def manifest_from_snapshot(
    snapshot: RepositorySnapshot, created_at: str, storage_bytes: int = 0
) -> ContextManifest:
    """Convert a repository snapshot into the portable Level 0 manifest."""
    include_rules_hash = _fingerprint(list(sorted(_INCLUDE_RULES)))
    exclude_rules_hash = _fingerprint(list(sorted(_EXCLUDE_RULES)))
    head_marker = snapshot.head_sha if snapshot.head_sha is not None else "unborn"
    provider_index_id = _fingerprint(
        [
            snapshot.repository_identity,
            snapshot.worktree_identity,
            head_marker,
            snapshot.dirty_fingerprint,
            include_rules_hash,
            exclude_rules_hash,
        ]
    )
    warnings = set(snapshot.warnings)
    if not snapshot.dirty_fingerprint_complete:
        warnings.add("dirty-fingerprint-incomplete")
    return ContextManifest(
        schema_version="1",
        repository_identity=snapshot.repository_identity,
        canonical_root_fingerprint=snapshot.canonical_root_fingerprint,
        git_common_dir_fingerprint=snapshot.git_common_dir_fingerprint,
        worktree_identity=snapshot.worktree_identity,
        head_sha=snapshot.head_sha,
        dirty_fingerprint=snapshot.dirty_fingerprint,
        provider_name="taf-context",
        provider_version="0.1.1",
        provider_index_id=provider_index_id,
        provider_schema_version="1",
        index_levels=("level0",),
        capabilities=("repository-map", "status"),
        created_at=created_at,
        updated_at=created_at,
        include_rules_hash=include_rules_hash,
        exclude_rules_hash=exclude_rules_hash,
        language_coverage=tuple(
            (language, 1.0) for language, _count in snapshot.language_counts
        ),
        path_coverage=1.0,
        tracked_file_count=len(snapshot.tracked_paths),
        indexed_file_count=len(snapshot.tracked_paths) + len(snapshot.untracked_paths),
        skipped_file_count=snapshot.ignored_entry_count,
        parse_failure_count=0,
        generated_or_vendored_count=snapshot.generated_or_vendored_count,
        storage_bytes=storage_bytes,
        background_state=BackgroundState.READY,
        warnings=tuple(sorted(warnings)),
    )
