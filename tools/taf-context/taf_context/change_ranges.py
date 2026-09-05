"""Changed line ranges between a resolved Git base and the working tree.

The base is the one work recovery resolves (explicit request, upstream main,
`origin/HEAD`, then a local `main`/`master`). A single `git diff -U0` from that
base to the working tree carries committed, staged, and unstaged changes; the
untracked paths of the Level 0 snapshot are added as whole-file entries. The
result is bounded and deterministic so it can be sent to the engine as is.

A second, additive mode measures the staged content instead: `changed_ranges`
with `staged=True` runs `git diff --cached` against `HEAD` - the index as
`git commit` would record it - and never adds untracked or unstaged paths,
since neither is part of a commit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .git_snapshot import SnapshotError, _git
from .recovery import _repository_root, _resolve_recovery_base
from .refresh import _safe_update_path


MAXIMUM_CHANGED_PATHS = 200
MAXIMUM_RANGES_PER_PATH = 64

WARNING_NO_HEAD = "no-head"
WARNING_BASE_UNRESOLVED = "base-unresolved"
WARNING_PATH_UNSAFE = "changed-path-unsafe"
WARNING_PATHS_LIMIT = "changed-paths-limit"
WARNING_RANGES_COLLAPSED = "changed-ranges-collapsed"
WARNING_DIFF_UNAVAILABLE = "changed-diff-unavailable"

# Emission order of the warnings; the caller must see a stable list.
_WARNING_ORDER = (
    WARNING_NO_HEAD,
    WARNING_BASE_UNRESOLVED,
    WARNING_DIFF_UNAVAILABLE,
    WARNING_PATH_UNSAFE,
    WARNING_PATHS_LIMIT,
    WARNING_RANGES_COLLAPSED,
)

_MAXIMUM_LINE = (1 << 31) - 1
# Diff output above this size is treated as unavailable rather than parsed.
_MAXIMUM_DIFF_BYTES = 16 * 1024 * 1024

_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_COMMIT_SHA = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")
_QUOTED_ESCAPES = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11, '"': 34, "\\": 92}
_OCTAL_DIGITS = "01234567"


@dataclass(frozen=True)
class ChangeBase:
    """The base a change set is measured against; mirrors `BaseResolution`."""

    requested: str | None
    ref: str | None
    sha: str | None
    source: str
    warning: str | None


@dataclass(frozen=True)
class ChangedPath:
    """One changed repository path; an empty range tuple means the whole file."""

    path: str
    ranges: tuple[tuple[int, int], ...]


def resolve_change_base(
    repo: Path, requested: str | None, *, root: Path | None = None
) -> ChangeBase:
    """Resolve the base with the work-recovery priority and warnings.

    `root` is the already-resolved repository root (H2): when the caller has
    one, passing it here skips a second `git rev-parse --show-toplevel`.
    """
    resolved_root = root if root is not None else _repository_root(Path(repo))
    resolution = _resolve_recovery_base(resolved_root, requested)
    return ChangeBase(
        resolution.requested,
        resolution.ref,
        resolution.sha,
        resolution.source,
        resolution.warning,
    )


def staged_change_base(repo: Path, *, root: Path | None = None) -> ChangeBase:
    """The staged base: `HEAD` itself, never the work-recovery priority.

    `git commit` records the index against `HEAD`, so the staged change set
    is always measured there instead of an upstream or a local main/master.
    On the very first commit of a repository `HEAD` is unborn and `sha` is
    `None`; `changed_ranges` turns that into the `no-head` warning instead of
    running a diff no `HEAD` could anchor.
    """
    resolved_root = root if root is not None else _repository_root(Path(repo))
    raw = _git(resolved_root, "rev-parse", "--verify", "HEAD", allow_failure=True)
    sha = None
    if raw is not None:
        text = raw.decode("ascii", "replace").strip()
        if _COMMIT_SHA.fullmatch(text):
            sha = text.lower()
    return ChangeBase(None, "HEAD", sha, "staged", None)


@dataclass
class _FileDiff:
    """Parser state for one `diff --git` section."""

    header_path: str | None
    left_path: str | None = None
    left_seen: bool = False
    right_path: str | None = None
    right_seen: bool = False
    added: bool = False
    whole_file: bool = False
    seen_hunk: bool = False
    ranges: list[tuple[int, int]] = field(default_factory=list)

    @property
    def path(self) -> str | None:
        if self.right_seen and self.right_path is not None:
            return self.right_path
        if self.left_seen and self.left_path is not None:
            return self.left_path
        return self.header_path

    @property
    def is_addition(self) -> bool:
        # `new file mode` and `--- /dev/null` mark a file absent at the base.
        return self.added or (self.left_seen and self.left_path is None)


def parse_unified_diff_ranges(text: str) -> dict[str, list[tuple[int, int]] | None]:
    """Map each path of a unified diff to its new-side ranges.

    A `None` value means the whole file: a binary change, a file added since
    the base, or a section whose hunk headers cannot be read. Paths with no
    line change at all (a mode-only section) are absent. Ranges are merged and
    sorted; a pure deletion becomes the single line adjacent to the removal.
    """
    sections: list[_FileDiff] = []
    current: _FileDiff | None = None
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            current = _FileDiff(_symmetric_header_path(line[len("diff --git "):]))
            sections.append(current)
            continue
        if current is None:
            continue
        if line.startswith("@@"):
            # Content lines start with '+', '-', ' ' or '\\', so '@@' here is a header.
            current.seen_hunk = True
            match = _HUNK_HEADER.match(line)
            if match is None:
                current.whole_file = True
                continue
            start = max(int(match.group(1)), 1)
            count = 1 if match.group(2) is None else int(match.group(2))
            end = start if count <= 0 else start + count - 1
            current.ranges.append((min(start, _MAXIMUM_LINE), min(end, _MAXIMUM_LINE)))
            continue
        if current.seen_hunk:
            # Inside a hunk body every line is content, never a header.
            continue
        if line.startswith("--- "):
            current.left_path, current.left_seen = _side_path(line[4:], "a/"), True
            continue
        if line.startswith("+++ "):
            current.right_path, current.right_seen = _side_path(line[4:], "b/"), True
            continue
        if line.startswith("new file mode "):
            # An added empty file has no `---`/`+++` header pair at all.
            current.added = True
            continue
        if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            current.whole_file = True
    found: dict[str, list[tuple[int, int]] | None] = {}
    for section in sections:
        path = section.path
        if path is None:
            continue
        if section.whole_file or section.is_addition:
            found[path] = None
            continue
        if not section.ranges or found.get(path, ()) is None:
            continue  # the path is already known to change as a whole
        found[path] = _merge_ranges(list(found.get(path) or []) + section.ranges)
    return found


def changed_ranges(
    repo: Path,
    base: ChangeBase,
    snapshot,
    *,
    root: Path | None = None,
    staged: bool = False,
) -> tuple[tuple[ChangedPath, ...], list[str]]:
    """Return the bounded changed paths of `repo` against `base`, plus warnings.

    `root` is the already-resolved repository root (H2): when the caller has
    one, passing it here skips a second `git rev-parse --show-toplevel`.
    `staged` measures the index against `HEAD` instead of the working tree -
    what `git commit` would record - and never adds the snapshot's untracked
    paths, since they are not part of a commit either.
    """
    warnings: set[str] = set()
    if base.warning:
        warnings.add(base.warning)
    root = root if root is not None else _repository_root(Path(repo))
    if staged and base.sha is None:
        # The very first commit of a repository has no HEAD to measure the
        # index against; there is nothing to diff, so the hook stays silent.
        # This branch is this module's own contract, not a result warning a
        # caller can observe: every query path resolves the repository first,
        # and that step refuses a repository without a commit before any
        # change query runs. It is therefore deliberately undocumented in
        # `result-contract.md` - documenting a warning that cannot reach a
        # result would be worse than not documenting it - and kept here so
        # the function stays correct for any future caller of its own.
        warnings.add(WARNING_NO_HEAD)
        return (), _ordered_warnings(warnings)
    diff_arguments = ["diff"]
    if staged:
        diff_arguments.append("--cached")
    diff_arguments += [
        "-U0",
        "--no-color",
        "--no-renames",
        "--ignore-submodules=all",
        # Pin the path prefixes the parser assumes: a repo-local
        # diff.mnemonicPrefix or diff.noprefix would otherwise silently
        # corrupt every parsed path. Command-line flags override config.
        "--src-prefix=a/",
        "--dst-prefix=b/",
        # An unresolved base still reports the uncommitted work of the
        # worktree; staged mode always measures the index against HEAD.
        "HEAD" if staged or base.sha is None else base.sha,
        "--",
    ]
    try:
        raw = _git(root, *diff_arguments, allow_failure=True)
    except SnapshotError:
        raw = None
    if raw is None or len(raw) > _MAXIMUM_DIFF_BYTES:
        warnings.add(WARNING_DIFF_UNAVAILABLE)
        return (), _ordered_warnings(warnings)
    parsed = parse_unified_diff_ranges(raw.decode("utf-8", "surrogateescape"))
    if not staged:
        for path in snapshot.untracked_paths:
            parsed[path] = None  # untracked files contribute every definition

    kept: list[str] = []
    for path in sorted(parsed):  # code-point order equals the engine's byte order
        if path.startswith('"') or not _safe_update_path(path):
            warnings.add(WARNING_PATH_UNSAFE)
            continue
        kept.append(path)
    if len(kept) > MAXIMUM_CHANGED_PATHS:
        kept = kept[:MAXIMUM_CHANGED_PATHS]
        warnings.add(WARNING_PATHS_LIMIT)

    changed: list[ChangedPath] = []
    for path in kept:
        ranges = parsed[path]
        if ranges is None:
            changed.append(ChangedPath(path, ()))
            continue
        if not ranges:  # pragma: no cover - the parser never stores an empty list
            continue
        if len(ranges) > MAXIMUM_RANGES_PER_PATH:
            warnings.add(WARNING_RANGES_COLLAPSED)
            changed.append(ChangedPath(path, ()))
            continue
        changed.append(ChangedPath(path, tuple(ranges)))
    return tuple(changed), _ordered_warnings(warnings)


def _ordered_warnings(warnings: set[str]) -> list[str]:
    return [warning for warning in _WARNING_ORDER if warning in warnings]


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sort and merge overlapping or adjacent ranges."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
            continue
        merged.append((start, end))
    return merged


def _side_path(value: str, prefix: str) -> str | None:
    """The path of a `---`/`+++` header line; None for `/dev/null`."""
    if value == "/dev/null":
        return None
    unquoted = _unquote_path(value)
    return unquoted[len(prefix):] if unquoted.startswith(prefix) else unquoted


def _symmetric_header_path(rest: str) -> str | None:
    """The path of `diff --git a/X b/X`, using that both sides are equal.

    Renames are disabled, so the two sides differ only in their one-letter
    prefix; splitting in the middle is unambiguous even when the path contains
    spaces, and it is the only source of the path for a binary section.
    """
    if len(rest) % 2 == 0:
        return None
    middle = len(rest) // 2
    if rest[middle] != " ":
        return None
    left, right = _unquote_path(rest[:middle]), _unquote_path(rest[middle + 1:])
    if not left.startswith("a/") or not right.startswith("b/") or left[2:] != right[2:]:
        return None
    return right[2:] or None


def _unquote_path(value: str) -> str:
    """Decode Git's C-style path quoting; unquoted or malformed input is returned as is."""
    if len(value) < 2 or not value.startswith('"') or not value.endswith('"'):
        return value
    body = value[1:-1]
    decoded = bytearray()
    index = 0
    while index < len(body):
        character = body[index]
        if character != "\\":
            decoded.extend(character.encode("utf-8", "surrogateescape"))
            index += 1
            continue
        index += 1
        if index >= len(body):
            return value
        marker = body[index]
        if marker in _QUOTED_ESCAPES:
            decoded.append(_QUOTED_ESCAPES[marker])
            index += 1
            continue
        digits = body[index:index + 3]
        if len(digits) != 3 or any(digit not in _OCTAL_DIGITS for digit in digits):
            return value
        code = int(digits, 8)
        if code > 0xFF:
            return value
        decoded.append(code)
        index += 3
    return decoded.decode("utf-8", "surrogateescape")
