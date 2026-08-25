"""Render bounded, metadata-only Level 0 repository context."""

from __future__ import annotations

from dataclasses import dataclass
import json

from .freshness import FreshnessAssessment
from .models import Freshness, RepositorySnapshot


_MIN_CHARS = 1024
_MAX_CHARS = 12000
_COVERAGE_WARNINGS = {
    "dirty-fingerprint-incomplete",
    "required-path-coverage-absent",
}


@dataclass(frozen=True)
class DossierResult:
    """A dossier and the exact accounting for its character budget."""

    markdown: str
    characters_used: int
    truncated: bool
    omitted_item_count: int


def build_dossier(
    snapshot: RepositorySnapshot,
    assessment: FreshnessAssessment,
    max_chars: int = _MAX_CHARS,
) -> DossierResult:
    """Build a line-atomic dossier without exposing checkout-local paths."""
    if (
        isinstance(max_chars, bool)
        or not isinstance(max_chars, int)
        or not _MIN_CHARS <= max_chars <= _MAX_CHARS
    ):
        raise ValueError("dossier character budget must be between 1024 and 12000")

    changed, candidates = _ranked_items(snapshot)
    total_items = len(changed) + len(candidates)
    warning_codes = set(snapshot.warnings)
    warning_codes.update(
        code for code in assessment.reason_codes if code != "exact-match"
    )
    visible_warnings = tuple(sorted(warning_codes & _COVERAGE_WARNINGS))

    def render(
        included_changed: list[str],
        included_candidates: list[str],
        omitted: int,
        *,
        compact: bool,
    ) -> str:
        lines = _mandatory_lines(
            snapshot,
            assessment,
            len(warning_codes),
            visible_warnings,
            compact=compact,
        )
        changed_index = lines.index("## Candidate Artifacts")
        lines[changed_index:changed_index] = included_changed
        warning_index = lines.index("## Coverage and Warnings")
        lines[warning_index:warning_index] = included_candidates
        lines.append(f"omitted-item-count={omitted}")
        return "\n".join(lines) + "\n"

    markdown = render([], [], total_items, compact=False)
    compact = len(markdown) > max_chars
    if compact:
        markdown = render([], [], total_items, compact=True)
    if len(markdown) > max_chars:  # Defensive: the hard minimum always fits today.
        raise ValueError("dossier mandatory metadata exceeds character budget")

    included_changed: list[str] = []
    included_candidates: list[str] = []
    ranked = tuple(("changed", line) for line in changed) + tuple(
        ("candidate", line) for line in candidates
    )
    for kind, line in ranked:
        next_changed = included_changed + ([line] if kind == "changed" else [])
        next_candidates = included_candidates + (
            [line] if kind == "candidate" else []
        )
        omitted = total_items - len(next_changed) - len(next_candidates)
        candidate = render(
            next_changed,
            next_candidates,
            omitted,
            compact=compact,
        )
        if len(candidate) > max_chars:
            break
        included_changed = next_changed
        included_candidates = next_candidates
        markdown = candidate

    omitted = total_items - len(included_changed) - len(included_candidates)
    return DossierResult(
        markdown=markdown,
        characters_used=len(markdown),
        truncated=omitted > 0,
        omitted_item_count=omitted,
    )


def _ranked_items(snapshot: RepositorySnapshot) -> tuple[tuple[str, ...], tuple[str, ...]]:
    seen: set[str] = set()
    changed: list[str] = []
    for state, paths in (
        ("staged", snapshot.staged_paths),
        ("unstaged", snapshot.unstaged_paths),
        ("untracked", snapshot.untracked_paths),
    ):
        for path in paths:
            if path in seen:
                continue
            seen.add(path)
            changed.append(_item_line(state, path))
    candidates = tuple(
        _item_line("metadata-only", path)
        for path in snapshot.candidate_artifacts
        if path not in seen
    )
    return tuple(changed), candidates


def _item_line(kind: str, path: str) -> str:
    rendered_path = json.dumps(path, ensure_ascii=False)
    return f"- kind={kind} path={rendered_path} extraction=git-metadata"


def _mandatory_lines(
    snapshot: RepositorySnapshot,
    assessment: FreshnessAssessment,
    warning_count: int,
    visible_warnings: tuple[str, ...],
    *,
    compact: bool,
) -> list[str]:
    next_action = _next_action(assessment.freshness)
    if compact:
        return [
            "# TAF Level 0 Context",
            "## Scope",
            "level=0 extraction=git-metadata",
            "## Freshness",
            f"freshness={assessment.freshness.value}",
            "## Worktree State",
            (
                f"staged={len(snapshot.staged_paths)} "
                f"unstaged={len(snapshot.unstaged_paths)} "
                f"untracked={len(snapshot.untracked_paths)}"
            ),
            "## Changed Paths",
            "## Candidate Artifacts",
            "## Coverage and Warnings",
            f"warning-count={warning_count}",
            "## Next Safe Action",
            f"next-action={next_action}",
        ]

    warning_line = ",".join(visible_warnings) if visible_warnings else "none"
    return [
        "# TAF Level 0 Context",
        "## Scope",
        (
            f"- level=0 extraction=git-metadata tracked={len(snapshot.tracked_paths)} "
            f"candidate-artifacts={len(snapshot.candidate_artifacts)}"
        ),
        "## Freshness",
        (
            f"- freshness={assessment.freshness.value} "
            f"reason-count={len(assessment.reason_codes)}"
        ),
        "## Worktree State",
        (
            f"- staged={len(snapshot.staged_paths)} "
            f"unstaged={len(snapshot.unstaged_paths)} "
            f"untracked={len(snapshot.untracked_paths)} "
            f"insertions={snapshot.insertions} deletions={snapshot.deletions}"
        ),
        "## Changed Paths",
        "## Candidate Artifacts",
        "## Coverage and Warnings",
        (
            f"- dirty-fingerprint-complete="
            f"{str(snapshot.dirty_fingerprint_complete).lower()} "
            f"warning-count={warning_count} warnings={warning_line}"
        ),
        "## Next Safe Action",
        f"- next-action={next_action}",
    ]


def _next_action(freshness: Freshness) -> str:
    return {
        Freshness.EXACT: "reuse-level0",
        Freshness.COMMIT_FRESH_WORKTREE_STALE: "incrementally-update",
        Freshness.INCREMENTALLY_STALE: "incrementally-update",
        Freshness.STRUCTURALLY_STALE: "rebuild-level0",
        Freshness.PARTIAL: "review-coverage-and-rebuild",
        Freshness.UNKNOWN: "verify-local-state",
        Freshness.UNUSABLE: "recreate-after-authorization",
    }[freshness]
