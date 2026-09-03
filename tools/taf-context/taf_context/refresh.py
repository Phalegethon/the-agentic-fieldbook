"""Incremental index refresh: binding memory, change deltas, and the change document."""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .git_snapshot import SnapshotError, _git, _z_paths


MAXIMUM_BINDING_DIRTY_PATHS = 5000


@dataclass(frozen=True)
class Binding:
    """What the bound index was built against; schema-1 bindings have no delta inputs."""

    index_identity: str
    head_sha: str | None
    dirty_fingerprint: str | None
    dirty_paths: tuple[str, ...] | None

    @property
    def has_delta_inputs(self) -> bool:
        return (
            self.head_sha is not None
            and self.dirty_fingerprint is not None
            and self.dirty_paths is not None
        )


def dirty_paths_of(snapshot: object) -> tuple[str, ...]:
    return tuple(
        sorted(
            set(snapshot.staged_paths) | set(snapshot.unstaged_paths) | set(snapshot.untracked_paths)
        )
    )


CHANGE_DOCUMENT_NAME = ".taf-update.json"
MAXIMUM_CHANGED_PATHS = 10000
_CHANGE_MANIFEST_PREFIX = b"taf-level0-change-manifest-v1\x00"
_JSON_ESCAPES = (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"), (" ", "\\u2028"), (" ", "\\u2029"))


def _safe_update_path(value: str) -> bool:
    """Mirror the engine's safeUpdatePath: clean, relative, no empty/dot segments."""
    if not value or "\\" in value or "\x00" in value or value.startswith("/"):
        return False
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:  # surrogate-escaped bytes cannot enter the JSON document
        return False
    return True


def changed_paths_between(binding: Binding, snapshot) -> list[str] | None:
    """Paths whose records may differ between the bound index and the snapshot.

    None means the delta cannot be computed and the caller must fall back to
    the explicit rebuild path. Every path dirty before or now is listed, plus
    every path `git diff` reports between the two heads.
    """
    if not binding.has_delta_inputs or snapshot.head_sha is None:
        return None
    changed = set(binding.dirty_paths) | set(dirty_paths_of(snapshot))
    if binding.head_sha != snapshot.head_sha:
        try:
            raw = _git(
                Path(snapshot.canonical_root), "diff", "--name-only", "--no-renames", "-z",
                binding.head_sha, snapshot.head_sha, allow_failure=True,
            )
            if raw is None:
                return None
            changed |= set(_z_paths(raw, "changed paths"))
        except SnapshotError:
            return None
    if len(changed) > MAXIMUM_CHANGED_PATHS:
        return None
    # valid UTF-8 only (surrogates rejected below); code-point order then equals the engine's byte order
    ordered = sorted(changed)
    if not all(_safe_update_path(path) for path in ordered):
        return None
    return ordered


def _canonical_json(value: dict) -> bytes:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    for raw, escaped in _JSON_ESCAPES:
        text = text.replace(raw, escaped)
    return text.encode("utf-8")


def change_manifest_identity(fields: dict) -> str:
    """The engine's Level 0 change manifest identity over the eleven identity fields."""
    return "sha256:" + hashlib.sha256(_CHANGE_MANIFEST_PREFIX + _canonical_json(fields)).hexdigest()


def build_change_document(binding: Binding, snapshot, changed_paths: list[str]) -> dict:
    fields = {
        "schema_version": "1",
        "prior_index_identity": binding.index_identity,
        "before_repository_identity": snapshot.repository_identity,
        "before_worktree_identity": snapshot.worktree_identity,
        "before_committed_head": binding.head_sha,
        "before_dirty_overlay_fingerprint": binding.dirty_fingerprint,
        "after_repository_identity": snapshot.repository_identity,
        "after_worktree_identity": snapshot.worktree_identity,
        "after_committed_head": snapshot.head_sha,
        "after_dirty_overlay_fingerprint": snapshot.dirty_fingerprint,
        "changed_paths": list(changed_paths),
    }
    return {**fields, "level0_change_manifest_identity": change_manifest_identity(fields)}


def write_change_document(state_root: Path, document: dict) -> str:
    """Write the document privately under the state root; returns its relative name."""
    import tempfile  # refresh-path only; keeps `import taf_context.cli` free of tempfile

    payload = _canonical_json(document)
    descriptor, temporary = tempfile.mkstemp(prefix=".taf-update-", dir=state_root)
    handed_off = False  # tracks whether os.fdopen took ownership of the descriptor
    try:
        os.fchmod(descriptor, 0o600)
        stream = os.fdopen(descriptor, "wb", closefd=True)
        handed_off = True
        with stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, state_root / CHANGE_DOCUMENT_NAME)
    except OSError:
        if not handed_off:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return CHANGE_DOCUMENT_NAME


def remove_change_document(state_root: Path) -> None:
    try:
        os.unlink(state_root / CHANGE_DOCUMENT_NAME)
    except OSError:
        return


class RefreshLock:
    """Best-effort mutual exclusion between concurrent refreshes of one state root.

    The engine's publication barrier is the real safety; this only avoids
    duplicate work. Portable: O_EXCL creation, a PID + timestamp payload, a
    staleness age, and a bounded wait after which the refresh proceeds anyway.
    """

    def __init__(self, state_root: Path, *, stale_after: float = 30.0, wait: float = 5.0, poll: float = 0.1) -> None:
        self.path = state_root / ".refresh.lock"
        self.stale_after, self.wait, self.poll = stale_after, wait, poll
        self.waited = False
        self._owned = False

    def _try_acquire(self) -> bool:
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(f"{os.getpid()} {time.time():.0f}\n")
        self._owned = True
        return True

    def _is_stale(self) -> bool:
        try:
            return time.time() - self.path.lstat().st_mtime > self.stale_after
        except OSError:
            return True  # vanished: retry the acquisition

    def __enter__(self) -> "RefreshLock":
        deadline = time.monotonic() + self.wait
        while not self._try_acquire():
            if self._is_stale():
                try:
                    os.unlink(self.path)
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                break  # proceed without the lock; the engine barrier protects publication
            self.waited = True
            time.sleep(self.poll)
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._owned:
            try:
                os.unlink(self.path)
            except OSError:
                pass
