"""Inspect and reclaim the user-local TAF context state root."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
from typing import Iterator

from .state_paths import StateError

CURRENT_RUNTIME_VERSION = "0.1.1"

REPOSITORIES_DIRECTORY = "repositories"
RUNTIME_DIRECTORY = "runtime"
BINDING_FILENAME = "binding.json"
NATIVE_DIRECTORY = "native"
GENERATIONS_DIRECTORY = "generations"
CURRENT_FILENAME = "CURRENT"
STAGING_PREFIX = ".stage-"
TRASH_PREFIX = ".trash-"

_IDENTITY_LENGTH = 64
_MAX_BINDING_BYTES = 16 * 1024


def touch_binding(binding_path: Path) -> None:
    """Record last use by refreshing the binding mtime; never raise."""
    try:
        os.utime(binding_path, None)
    except OSError:
        return


def summarize_state(root: Path) -> dict[str, int]:
    """Count entries, orphans, stale runtimes, and bytes without following symlinks."""
    summary = {"root_bytes": 0, "entry_count": 0, "orphan_count": 0, "stale_runtime_count": 0}
    if not _is_real_directory(root):
        return summary
    summary["root_bytes"] = _tree_bytes(root)
    for entry in iter_entries(root):
        summary["entry_count"] += 1
        if not has_valid_binding(entry):
            summary["orphan_count"] += 1
    for runtime in iter_runtime_versions(root):
        if runtime.name != CURRENT_RUNTIME_VERSION:
            summary["stale_runtime_count"] += 1
    return summary


def iter_entries(root: Path) -> Iterator[Path]:
    """Yield ``repositories/<repo>/<worktree>`` directories in sorted order."""
    repositories = root / REPOSITORIES_DIRECTORY
    if not _is_real_directory(repositories):
        return
    for repository in _sorted_real_directories(repositories):
        if not _is_identity_name(repository.name):
            continue
        for worktree in _sorted_real_directories(repository):
            if _is_identity_name(worktree.name):
                yield worktree


def iter_runtime_versions(root: Path) -> Iterator[Path]:
    runtime = root / RUNTIME_DIRECTORY
    if not _is_real_directory(runtime):
        return
    yield from _sorted_real_directories(runtime)


def has_valid_binding(entry: Path) -> bool:
    binding = entry / BINDING_FILENAME
    try:
        metadata = binding.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_BINDING_BYTES:
        return False
    try:
        value = json.loads(binding.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return (
        type(value) is dict
        and value.get("schema_version") == "1"
        and isinstance(value.get("index_identity"), str)
    )


def _is_identity_name(name: str) -> bool:
    return len(name) == _IDENTITY_LENGTH and all(c in "0123456789abcdef" for c in name)


def _is_real_directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except OSError:
        return False


def _sorted_real_directories(path: Path) -> list[Path]:
    try:
        children = sorted(path.iterdir())
    except OSError:
        return []
    return [child for child in children if _is_real_directory(child)]


def _tree_bytes(path: Path) -> int:
    total = 0
    for current, directories, files in os.walk(path):
        directories[:] = [d for d in directories if _is_real_directory(Path(current) / d)]
        for name in files:
            try:
                metadata = (Path(current) / name).lstat()
            except OSError:
                continue
            if stat.S_ISREG(metadata.st_mode):
                total += metadata.st_size
    return total


@dataclass(frozen=True)
class Candidate:
    """One filesystem entry a plan proposes to remove."""

    category: str
    relative_path: str
    bytes: int


def plan_remove(root: Path, repository_key: str, worktree_key: str) -> list[Candidate]:
    """Plan deletion of one worktree entry; empty when it does not exist."""
    entry = root / REPOSITORIES_DIRECTORY / repository_key / worktree_key
    if not _is_real_directory(entry):
        return []
    return [_candidate("worktree-entry", root, entry)]


def apply_plan(root: Path, candidates: list[Candidate]) -> list[Candidate]:
    """Delete candidates with a two-phase rename. Validate everything first."""
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise StateError("state-root-unavailable") from exc
    targets: list[tuple[Candidate, Path]] = []
    for candidate in candidates:
        target = root / candidate.relative_path
        try:
            metadata = target.lstat()
        except OSError as exc:
            raise StateError("state-boundary-violation") from exc
        if stat.S_ISLNK(metadata.st_mode) or ".." in Path(candidate.relative_path).parts:
            raise StateError("state-boundary-violation")
        resolved = target.resolve(strict=True)
        if resolved == resolved_root or resolved_root not in resolved.parents:
            raise StateError("state-boundary-violation")
        targets.append((candidate, target))
    removed: list[Candidate] = []
    for candidate, target in targets:
        trash = root / (TRASH_PREFIX + secrets.token_hex(8))
        try:
            trash.mkdir(mode=0o700)
            os.rename(target, trash / target.name)
            shutil.rmtree(trash)
        except OSError as exc:
            raise StateError("state-removal-failed") from exc
        removed.append(candidate)
    return removed


def _candidate(category: str, root: Path, path: Path) -> Candidate:
    size = _tree_bytes(path) if _is_real_directory(path) else _file_bytes(path)
    return Candidate(category, path.relative_to(root).as_posix(), size)


def _file_bytes(path: Path) -> int:
    try:
        return path.lstat().st_size
    except OSError:
        return 0


LEGACY_CONTROL_FILES = ("audit.jsonl", "consent.json", "providers.json")
_SECONDS_PER_DAY = 86400


def plan_gc(root: Path, *, unused_for_days: int, now: float) -> list[Candidate]:
    """Plan reclaimable state. Fresh bound entries are never candidates."""
    if unused_for_days < 0:
        raise StateError("invalid-unused-for")
    if not _is_real_directory(root):
        return []
    orphans: list[Candidate] = []
    unused: list[Candidate] = []
    generations: list[Candidate] = []
    doomed_entries: set[Path] = set()
    cutoff = now - unused_for_days * _SECONDS_PER_DAY
    for entry in iter_entries(root):
        if not has_valid_binding(entry):
            orphans.append(_candidate("orphan-entry", root, entry))
            doomed_entries.add(entry)
            continue
        if (entry / BINDING_FILENAME).lstat().st_mtime <= cutoff:
            unused.append(_candidate("unused-entry", root, entry))
            doomed_entries.add(entry)
            continue
        generations.extend(_unreferenced_generations(root, entry))
    runtimes = [
        _candidate("stale-runtime", root, runtime)
        for runtime in iter_runtime_versions(root)
        if runtime.name != CURRENT_RUNTIME_VERSION
    ]
    legacy = [
        _candidate("legacy-control-file", root, root / name)
        for name in LEGACY_CONTROL_FILES
        if _is_real_file(root / name)
    ]
    trash = [
        _candidate("trash-leftover", root, child)
        for child in _sorted_real_directories(root)
        if child.name.startswith(TRASH_PREFIX)
    ]
    empty_parents: list[Candidate] = []
    repositories = root / REPOSITORIES_DIRECTORY
    for repository in _sorted_real_directories(repositories) if _is_real_directory(repositories) else []:
        if not _is_identity_name(repository.name):
            continue
        worktrees = _sorted_real_directories(repository)
        if all(worktree in doomed_entries for worktree in worktrees):
            residual_directories = sum(
                _tree_bytes(child) for child in worktrees if child not in doomed_entries
            )
            residual_files = sum(
                _file_bytes(item) for item in repository.iterdir() if _is_real_file(item)
            )
            empty_parents.append(
                Candidate(
                    "empty-parent",
                    repository.relative_to(root).as_posix(),
                    residual_directories + residual_files,
                )
            )
    ordered = orphans + unused + runtimes + generations + legacy + trash + empty_parents
    return [item for group in _group_by_category(ordered) for item in sorted(group, key=lambda c: c.relative_path)]


def _unreferenced_generations(root: Path, entry: Path) -> list[Candidate]:
    """Generations not named by CURRENT. Without a readable CURRENT, propose nothing."""
    generations = entry / NATIVE_DIRECTORY / GENERATIONS_DIRECTORY
    if not _is_real_directory(generations):
        return []
    current = _read_current(entry / NATIVE_DIRECTORY / CURRENT_FILENAME)
    if not _is_identity_name(current):
        return []
    return [
        _candidate("unreferenced-generation", root, child)
        for child in _sorted_real_directories(generations)
        if child.name != current
    ]


def _read_current(path: Path) -> str:
    try:
        if not _is_real_file(path):
            return ""
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def _is_real_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _group_by_category(items: list[Candidate]) -> list[list[Candidate]]:
    order = ["orphan-entry", "unused-entry", "stale-runtime", "unreferenced-generation",
             "legacy-control-file", "trash-leftover", "empty-parent"]
    groups: dict[str, list[Candidate]] = {name: [] for name in order}
    for item in items:
        groups[item.category].append(item)
    return [groups[name] for name in order]
