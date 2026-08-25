"""Collect bounded, local-only repository metadata for Level 0 context."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
from pathlib import Path, PurePosixPath

from .models import BackgroundState, ContextManifest, RepositorySnapshot


_GIT_TIMEOUT_SECONDS = 20
_HASH_CHUNK_BYTES = 1024 * 1024
_OBJECT_ID = re.compile(r"[0-9a-fA-F]{40,64}")

_GENERATED_OR_VENDORED_SEGMENTS = {
    "vendor",
    "vendors",
    "third_party",
    "node_modules",
    "dist",
    "build",
    "generated",
    "coverage",
}

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


def _git(repo: Path, *args: str, allow_failure: bool = False) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
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
    if metadata.st_size > max_dirty_file_bytes:
        return (
            f"oversized:{metadata.st_size}:{metadata.st_mtime_ns}",
            0,
            False,
            False,
            "dirty-fingerprint-incomplete",
        )
    digest = hashlib.sha256()
    bytes_hashed = 0
    binary = False
    try:
        with path.open("rb") as source:
            while True:
                chunk = source.read(_HASH_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
                bytes_hashed += len(chunk)
                binary = binary or b"\x00" in chunk
    except OSError as exc:
        raise SnapshotError("dirty path content unavailable") from exc
    return (
        f"regular:{metadata.st_size}:sha256:{digest.hexdigest()}",
        bytes_hashed,
        binary,
        True,
        None,
    )


def _numstat(raw: bytes | None) -> tuple[int, int]:
    if raw is None or (raw and not raw.endswith(b"\x00")):
        raise SnapshotError("malformed Git output: numstat")
    fields = [] if not raw else raw[:-1].split(b"\x00")
    insertions = 0
    deletions = 0
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        parts = record.split(b"\t", 2)
        if len(parts) != 3:
            raise SnapshotError("malformed Git output: numstat")
        added, removed, path = parts
        if added != b"-":
            try:
                insertions += int(added)
            except ValueError as exc:
                raise SnapshotError("malformed Git output: numstat") from exc
        if removed != b"-":
            try:
                deletions += int(removed)
            except ValueError as exc:
                raise SnapshotError("malformed Git output: numstat") from exc
        if path:
            _path_text(path, "numstat")
        else:
            if index + 1 >= len(fields):
                raise SnapshotError("malformed Git output: numstat")
            _path_text(fields[index], "numstat")
            _path_text(fields[index + 1], "numstat")
            index += 2
    return insertions, deletions


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
    git_dir = Path(
        _text(_git(repo, "rev-parse", "--absolute-git-dir"), "Git directory")
    ).resolve()
    common_value = _text(
        _git(repo, "rev-parse", "--git-common-dir"), "Git common directory"
    )
    git_common_dir = _resolve_common_dir(git_dir, canonical_root, common_value)

    raw_head = _git(repo, "rev-parse", "--verify", "HEAD", allow_failure=True)
    if raw_head is None:
        head_sha = None
        root_commits: tuple[str, ...] = ()
    else:
        head_values = _object_ids(raw_head, "HEAD")
        if len(head_values) != 1:
            raise SnapshotError("malformed Git output: HEAD")
        head_sha = head_values[0]
        root_commits = _object_ids(
            _git(repo, "rev-list", "--max-parents=0", "HEAD"), "root commits"
        )

    raw_branch = _git(
        repo, "symbolic-ref", "--short", "-q", "HEAD", allow_failure=True
    )
    branch = None if raw_branch is None else _text(raw_branch, "branch")

    tracked_paths = _z_paths(_git(repo, "ls-files", "-z"), "tracked paths")
    staged_paths = _z_paths(
        _git(repo, "diff", "--cached", "--name-only", "-z"), "staged paths"
    )
    unstaged_paths = _z_paths(
        _git(repo, "diff", "--name-only", "-z"), "unstaged paths"
    )
    untracked_paths = _z_paths(
        _git(repo, "ls-files", "--others", "--exclude-standard", "-z"),
        "untracked paths",
    )
    statuses, ignored_entry_count = _status_paths(
        _git(
            repo,
            "status",
            "--porcelain=v1",
            "-z",
            "--ignored=matching",
            "--untracked-files=normal",
        )
    )

    dirty_paths = tuple(sorted(set(staged_paths + unstaged_paths + untracked_paths)))
    dirty_values: list[str] = []
    dirty_bytes_hashed = 0
    binary_file_count = 0
    oversized_file_count = 0
    dirty_complete = True
    warnings: set[str] = set()
    for path in dirty_paths:
        flags = statuses.get(path)
        if flags is None:
            flags = "??" if path in untracked_paths else "  "
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
    if head_sha is None:
        insertions = deletions = 0
    else:
        insertions, deletions = _numstat(
            _git(repo, "diff", "--numstat", "-z", "HEAD")
        )

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
        provider_version="0.1.0",
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
