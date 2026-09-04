"""Inspect and reclaim the user-local TAF context state root."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
from typing import Iterator

from .state_paths import StateError

CURRENT_RUNTIME_VERSION = "0.6.0"

REPOSITORIES_DIRECTORY = "repositories"
RUNTIME_DIRECTORY = "runtime"
BINDING_FILENAME = "binding.json"
NATIVE_DIRECTORY = "native"
GENERATIONS_DIRECTORY = "generations"
CURRENT_FILENAME = "CURRENT"
MANIFEST_FILENAME = "manifest.json"
STAGING_PREFIX = ".stage-"
TRASH_PREFIX = ".trash-"

# The generation manifest format this runtime reads. A generation written in
# any other format is unusable: the engine can neither answer from it nor
# publish over it, and it refuses without a warning of its own, so the broker
# has to recognize it here.
CURRENT_MANIFEST_FORMAT_VERSION = "3"
UNKNOWN_ENGINE_VERSION = "unknown"

_IDENTITY_LENGTH = 64
_MAX_BINDING_BYTES = 1024 * 1024
_MAX_MANIFEST_BYTES = 256 * 1024
_ENGINE_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}\Z")


def touch_binding(binding_path: Path) -> None:
    """Record last use by refreshing the binding mtime; never raise."""
    try:
        os.utime(binding_path, None)
    except OSError:
        return


def summarize_state(root: Path) -> dict[str, int]:
    """Count entries, orphans, stale runtimes, and bytes without following symlinks."""
    summary = {
        "root_bytes": 0,
        "entry_count": 0,
        "orphan_count": 0,
        "stale_runtime_count": 0,
        "incompatible_generation_count": 0,
    }
    if not _is_real_directory(root):
        return summary
    summary["root_bytes"] = _tree_bytes(root)
    for entry in iter_entries(root):
        summary["entry_count"] += 1
        if not has_valid_binding(entry):
            summary["orphan_count"] += 1
            continue
        if incompatible_generation_version(entry / NATIVE_DIRECTORY) is not None:
            summary["incompatible_generation_count"] += 1
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
        and value.get("schema_version") in {"1", "2"}
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
    incompatible: list[Candidate] = []
    generations: list[Candidate] = []
    doomed_entries: set[Path] = set()
    cutoff = now - unused_for_days * _SECONDS_PER_DAY
    for entry in iter_entries(root):
        if not has_valid_binding(entry):
            orphans.append(_candidate("orphan-entry", root, entry))
            doomed_entries.add(entry)
            continue
        # An entry whose last use is exactly unused_for_days old counts as unused.
        if (entry / BINDING_FILENAME).lstat().st_mtime <= cutoff:
            unused.append(_candidate("unused-entry", root, entry))
            doomed_entries.add(entry)
            continue
        if incompatible_generation_version(entry / NATIVE_DIRECTORY) is not None:
            # The whole record goes, not the generation alone: its binding
            # names an index this runtime cannot read, and removing the
            # generation by itself would leave CURRENT and the binding
            # pointing at nothing. Rebuilding writes the record again.
            incompatible.append(_candidate("incompatible-generation", root, entry))
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
            try:
                residual_files = sum(
                    _file_bytes(item) for item in repository.iterdir() if _is_real_file(item)
                )
            except OSError:
                residual_files = 0
            empty_parents.append(
                Candidate("empty-parent", repository.relative_to(root).as_posix(), residual_files)
            )
    ordered = orphans + unused + incompatible + runtimes + generations + legacy + trash + empty_parents
    return [item for group in _group_by_category(ordered) for item in sorted(group, key=lambda c: c.relative_path)]


def _unreferenced_generations(root: Path, entry: Path) -> list[Candidate]:
    """Generation and staging directories not named by CURRENT. Without an exact CURRENT token, propose nothing."""
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


def incompatible_generation_version(state_root: Path) -> str | None:
    """The engine version of a bound generation this runtime cannot read.

    `state_root` is the entry's ``native`` directory, the one the engine is
    handed. The answer is ``None`` whenever the generation is readable,
    absent, or unidentifiable: only a manifest that positively names a format
    version other than the current one is treated as incompatible, so a
    corrupt or half-written generation is left to the engine's own refusal
    instead of being deleted on a guess. An old manifest whose engine version
    cannot be read safely answers ``"unknown"``, never the raw file content.
    """
    token = _read_current(state_root / CURRENT_FILENAME)
    if not token:
        return None
    manifest = _read_manifest(
        state_root / GENERATIONS_DIRECTORY / token / MANIFEST_FILENAME
    )
    if manifest is None:
        return None
    format_version = manifest.get("format_version")
    if type(format_version) is not str or format_version == CURRENT_MANIFEST_FORMAT_VERSION:
        return None
    engine_version = manifest.get("engine_version")
    if type(engine_version) is not str or not _ENGINE_VERSION.match(engine_version):
        return UNKNOWN_ENGINE_VERSION
    return engine_version


def _read_manifest(path: Path) -> dict | None:
    """The generation manifest as a plain object, or None when unreadable."""
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_MANIFEST_BYTES:
            return None
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeError, ValueError):
        return None
    return value if type(value) is dict else None


def current_generation_token(state_root: Path) -> str:
    """Public wrapper: the CURRENT generation token directly under `state_root`, or ""."""
    return _read_current(state_root / CURRENT_FILENAME)


def _read_current(path: Path) -> str:
    """Return the CURRENT generation token, or "" unless it is exactly 64 hex characters with at most one trailing newline."""
    try:
        if not _is_real_file(path):
            return ""
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""
    if text.endswith("\n"):
        text = text[:-1]
    return text if _is_identity_name(text) else ""


def _is_real_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def plan_prune_generations(root: Path, entry: Path, *, now: float, grace_seconds: float = 60.0) -> list[Candidate]:
    """Unreferenced generations old enough that no reader can still be loading them."""
    cutoff = now - grace_seconds
    aged: list[Candidate] = []
    for candidate in _unreferenced_generations(root, entry):
        try:
            modified = (root / candidate.relative_path).lstat().st_mtime
        except OSError:
            continue
        if modified <= cutoff:
            aged.append(candidate)
    return aged


def _group_by_category(items: list[Candidate]) -> list[list[Candidate]]:
    order = ["orphan-entry", "unused-entry", "incompatible-generation", "stale-runtime",
             "unreferenced-generation", "legacy-control-file", "trash-leftover", "empty-parent"]
    groups: dict[str, list[Candidate]] = {name: [] for name in order}
    for item in items:
        groups[item.category].append(item)
    return [groups[name] for name in order]
