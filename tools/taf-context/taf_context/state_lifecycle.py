"""Inspect and reclaim the user-local TAF context state root."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
from typing import Iterator

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
