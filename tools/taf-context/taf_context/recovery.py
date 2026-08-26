"""Bounded, read-only evidence collection for interrupted Git work."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .git_snapshot import _git_environment
from .models import Freshness
from .recovery_models import (
    EvidenceClass,
    RecoveryClaim,
    RecoveryCoverage,
    RecoveryDossier,
    WorkState,
    WorkstreamState,
)


_MAX_GIT_OUTPUT = 4 * 1024 * 1024
_DEFAULT_BUDGET = 4000
_ALLOWED_BUDGETS = (2000, 4000, 8000, 12000)


class RecoveryError(RuntimeError):
    """Raised when recovery evidence cannot be collected safely."""


@dataclass(frozen=True)
class BaseResolution:
    requested: str | None
    ref: str | None
    sha: str | None
    source: str
    warning: str | None


@dataclass(frozen=True)
class RecoveryRequest:
    repo: Path
    base: str | None = None
    max_chars: int = _DEFAULT_BUDGET
    untracked_content_paths: tuple[str, ...] = ()
    note_files: tuple[Path, ...] = ()
    test_result_files: tuple[Path, ...] = ()


@dataclass(frozen=True)
class RecoveryResult:
    dossier: RecoveryDossier
    model_text: str
    characters_used: int


def resolve_recovery_base(repo: Path, requested: str | None) -> BaseResolution:
    """Resolve a local base ref using the approved deterministic priority."""
    root = _repository_root(repo)
    if requested is not None:
        if not requested or len(requested) > 512 or any(char in requested for char in "\x00\r\n"):
            raise ValueError("invalid recovery base")
        sha = _rev_parse(root, requested)
        if sha is None:
            raise ValueError("invalid recovery base")
        return BaseResolution(requested, requested, sha, "explicit", None)

    upstream = _single_line(root, "rev-parse", "--symbolic-full-name", "@{upstream}", allow_failure=True)
    if upstream is None:
        upstream = _configured_upstream(root)
    if upstream and _is_main_ref(upstream):
        sha = _rev_parse(root, upstream)
        if sha:
            return BaseResolution(None, upstream, sha, "upstream-main", None)

    origin_head = _single_line(
        root,
        "symbolic-ref",
        "--quiet",
        "refs/remotes/origin/HEAD",
        allow_failure=True,
    )
    if origin_head:
        sha = _rev_parse(root, origin_head)
        if sha:
            return BaseResolution(None, origin_head, sha, "origin-head", None)

    for ref, source in (("refs/heads/main", "local-main"), ("refs/heads/master", "local-master")):
        sha = _rev_parse(root, ref)
        if sha:
            return BaseResolution(None, ref, sha, source, None)

    return BaseResolution(None, None, None, "unknown", "base-unresolved")


def collect_recovery(request: RecoveryRequest) -> RecoveryResult:
    """Collect current-worktree state and metadata-only candidate workstreams."""
    if request.max_chars not in _ALLOWED_BUDGETS:
        raise ValueError("max_chars must be one of 2000, 4000, 8000, or 12000")
    root = _repository_root(request.repo)
    base = resolve_recovery_base(root, request.base)
    repository_identity = _identity(_git_common_dir(root))
    current_identity = _identity(root)
    head = _rev_parse(root, "HEAD")
    branch = _single_line(root, "symbolic-ref", "--quiet", "--short", "HEAD", allow_failure=True)
    staged, unstaged, untracked = _current_status_counts(root)
    current = _classify(
        root,
        current_identity,
        branch,
        head,
        base.sha,
        staged,
        unstaged,
        untracked,
        metadata_only=False,
    )
    candidates = _candidate_worktrees(root, current_identity, base.sha)
    warnings = tuple(sorted(item for item in (base.warning,) if item))
    claim = RecoveryClaim.from_dict(
        {
            "claim_id": "current.state",
            "evidence_class": EvidenceClass.OBSERVED.value,
            "text": f"Current worktree state is {current.state.value}.",
            "repository_identity": repository_identity,
            "worktree_identity": current_identity,
            "provenance": ["git/status"],
            "freshness": Freshness.EXACT.value,
            "supports": ["current.next-action"],
            "conflicts": [],
            "qualifications": [],
        }
    )
    dossier = RecoveryDossier.from_dict(
        {
            "schema_version": "1",
            "repository_identity": repository_identity,
            "worktree_identity": current_identity,
            "current": current.to_dict(),
            "candidates": [candidate.to_dict() for candidate in candidates],
            "claims": [claim.to_dict()],
            "coverage": RecoveryCoverage(
                changed_path_count=staged + unstaged + untracked,
                examined_path_count=0,
                cluster_count=0,
                included_cluster_count=0,
                omitted_item_count=0,
                omitted_characters=0,
                budget_characters=request.max_chars,
            ).to_dict(),
            "warnings": list(warnings),
            "next_action_hint": _next_action(current),
        }
    )
    model_text = _render_state_only(dossier)
    if len(model_text) > request.max_chars:
        raise RecoveryError("recovery identity exceeds output budget")
    return RecoveryResult(dossier=dossier, model_text=model_text, characters_used=len(model_text))


def _repository_root(repo: Path) -> Path:
    candidate = Path(repo).resolve()
    root = _single_line(candidate, "rev-parse", "--show-toplevel", allow_failure=True)
    if root is None:
        raise RecoveryError("not a Git worktree")
    return Path(root).resolve()


def _git_common_dir(repo: Path) -> Path:
    raw = _single_line(repo, "rev-parse", "--git-common-dir")
    assert raw is not None
    path = Path(raw)
    if not path.is_absolute():
        path = repo / path
    return path.resolve()


def _run_git(repo: Path, *args: str, allow_failure: bool = False) -> bytes | None:
    command = ["git", *args]
    if args and args[0] == "diff":
        command[2:2] = ["--no-ext-diff", "--no-textconv"]
    try:
        result = subprocess.run(
            command,
            cwd=repo,
            env=_git_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RecoveryError("local Git command failed") from error
    if len(result.stdout) > _MAX_GIT_OUTPUT or len(result.stderr) > 64 * 1024:
        raise RecoveryError("local Git output exceeded safety limit")
    if result.returncode != 0:
        if allow_failure:
            return None
        raise RecoveryError("local Git command failed")
    return result.stdout


def _single_line(repo: Path, *args: str, allow_failure: bool = False) -> str | None:
    raw = _run_git(repo, *args, allow_failure=allow_failure)
    if raw is None:
        return None
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise RecoveryError("malformed Git output") from error
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        if allow_failure:
            return None
        raise RecoveryError("malformed Git output")
    return value


def _rev_parse(repo: Path, ref: str) -> str | None:
    value = _single_line(
        repo,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{ref}^{{commit}}",
        allow_failure=True,
    )
    if value is None or len(value) not in (40, 64) or any(char not in "0123456789abcdefABCDEF" for char in value):
        return None
    return value.lower()


def _is_main_ref(ref: str) -> bool:
    return ref in {
        "refs/heads/main",
        "refs/heads/master",
        "refs/remotes/origin/main",
        "refs/remotes/origin/master",
    }


def _configured_upstream(repo: Path) -> str | None:
    branch = _single_line(repo, "symbolic-ref", "--quiet", "--short", "HEAD", allow_failure=True)
    if branch is None or any(char in branch for char in "\x00\r\n"):
        return None
    merge = _single_line(repo, "config", "--get", f"branch.{branch}.merge", allow_failure=True)
    remote = _single_line(repo, "config", "--get", f"branch.{branch}.remote", allow_failure=True)
    if merge not in ("refs/heads/main", "refs/heads/master") or remote is None:
        return None
    leaf = merge.rsplit("/", 1)[-1]
    return merge if remote == "." else f"refs/remotes/{remote}/{leaf}"


def _identity(path: Path) -> str:
    return "sha256:" + hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()


def _current_status_counts(repo: Path) -> tuple[int, int, int]:
    raw = _run_git(repo, "status", "--porcelain=v2", "-z", "--branch", "--untracked-files=normal")
    assert raw is not None
    if raw and not raw.endswith(b"\x00"):
        raise RecoveryError("malformed Git status")
    staged = unstaged = untracked = 0
    records = [] if not raw else raw[:-1].split(b"\x00")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if record.startswith(b"# "):
            continue
        if record.startswith(b"? "):
            untracked += 1
            continue
        if record.startswith(b"! "):
            continue
        if record.startswith((b"1 ", b"2 ", b"u ")) and len(record) >= 4:
            status = record[2:4]
            if status[:1] not in (b".", b" "):
                staged += 1
            if status[1:2] not in (b".", b" "):
                unstaged += 1
            if record.startswith(b"2 "):
                if index >= len(records):
                    raise RecoveryError("malformed Git status")
                index += 1
            continue
        raise RecoveryError("malformed Git status")
    return staged, unstaged, untracked


def _relation(repo: Path, head: str | None, base: str | None) -> tuple[int | None, int | None, bool | None]:
    if head is None or base is None:
        return None, None, None
    raw = _single_line(repo, "rev-list", "--left-right", "--count", f"{head}...{base}")
    assert raw is not None
    parts = raw.split()
    if len(parts) != 2 or any(not part.isdigit() for part in parts):
        raise RecoveryError("malformed Git relation")
    ancestor_process = subprocess.run(
        ["git", "merge-base", "--is-ancestor", head, base],
        cwd=repo,
        env=_git_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=20,
    )
    if ancestor_process.returncode not in (0, 1):
        raise RecoveryError("local Git relation failed")
    return int(parts[0]), int(parts[1]), ancestor_process.returncode == 0


def _classify(
    repo: Path,
    worktree_identity: str,
    branch: str | None,
    head: str | None,
    base: str | None,
    staged: int,
    unstaged: int,
    untracked: int,
    *,
    metadata_only: bool,
) -> WorkstreamState:
    ahead, behind, ancestor = _relation(repo, head, base)
    reasons: set[str] = set()
    if metadata_only:
        reasons.add("metadata-only")
    if staged or unstaged:
        reasons.add("dirty-tracked")
    if untracked:
        reasons.add("untracked-present")
    if staged or unstaged or untracked:
        state = WorkState.ACTIVE_DIRTY
    elif base is None or head is None:
        state = WorkState.CLEAN_UNRESOLVED
        reasons.add("base-or-head-unresolved")
    elif ahead and behind:
        state = WorkState.DIVERGED
        reasons.add("ahead-and-behind")
    elif ancestor and head != base and metadata_only:
        state = WorkState.SUPERSEDED_STALE
        reasons.add("reachable-behind-base")
    elif ancestor:
        state = WorkState.INTEGRATED
        reasons.add("head-reachable-from-base")
    elif ahead and ahead > 0:
        state = WorkState.ACTIVE_COMMITTED
        reasons.add("unique-commits")
    else:
        state = WorkState.UNKNOWN
        reasons.add("relation-unknown")
    return WorkstreamState(
        worktree_identity=worktree_identity,
        branch=branch,
        head_sha=head,
        base_sha=base,
        state=state,
        staged_count=staged,
        unstaged_count=unstaged,
        untracked_count=untracked,
        ahead_count=ahead,
        behind_count=behind,
        reason_codes=tuple(sorted(reasons)),
    )


def _candidate_worktrees(repo: Path, current_identity: str, base: str | None) -> tuple[WorkstreamState, ...]:
    raw = _run_git(repo, "worktree", "list", "--porcelain", "-z")
    assert raw is not None
    if raw and not raw.endswith(b"\x00\x00"):
        raise RecoveryError("malformed Git worktree list")
    candidates: list[WorkstreamState] = []
    for block in raw.split(b"\x00\x00"):
        if not block:
            continue
        fields: dict[str, str] = {}
        for raw_line in block.split(b"\x00"):
            key, separator, raw_value = raw_line.partition(b" ")
            if not separator:
                continue
            try:
                fields[key.decode("ascii")] = raw_value.decode("utf-8", "surrogateescape")
            except UnicodeDecodeError as error:
                raise RecoveryError("malformed Git worktree list") from error
        path_value = fields.get("worktree")
        if not path_value:
            raise RecoveryError("malformed Git worktree list")
        identity = _identity(Path(path_value))
        if identity == current_identity:
            continue
        head = fields.get("HEAD")
        if head and (len(head) not in (40, 64) or any(char not in "0123456789abcdefABCDEF" for char in head)):
            raise RecoveryError("malformed Git worktree list")
        branch_ref = fields.get("branch")
        branch = branch_ref.removeprefix("refs/heads/") if branch_ref else None
        candidates.append(
            _classify(
                repo,
                identity,
                branch,
                head.lower() if head else None,
                base,
                0,
                0,
                0,
                metadata_only=True,
            )
        )
        if len(candidates) > 64:
            raise RecoveryError("too many worktree candidates")
    return tuple(sorted(candidates, key=lambda candidate: candidate.worktree_identity))


def _next_action(state: WorkstreamState) -> str:
    if state.state is WorkState.ACTIVE_DIRTY:
        return "Review the current tracked changes before continuing implementation."
    if state.state is WorkState.ACTIVE_COMMITTED:
        return "Review the unique commits before continuing implementation."
    if state.state is WorkState.DIVERGED:
        return "Resolve the branch relationship before continuing implementation."
    if state.state in (WorkState.INTEGRATED, WorkState.SUPERSEDED_STALE):
        return "Confirm whether this workstream still has unfinished intent."
    return "Establish the intended base and unfinished objective before changing code."


def _render_state_only(dossier: RecoveryDossier) -> str:
    current = dossier.current
    return (
        "# TAF Work Recovery Evidence\n"
        "## Scope\n"
        f"- Repository: {dossier.repository_identity}\n"
        f"- Worktree: {dossier.worktree_identity}\n"
        "## Current Workstream\n"
        f"- State: {current.state.value}\n"
        f"- Branch: {current.branch or 'unknown'}\n"
        f"- HEAD: {current.head_sha or 'unknown'}\n"
        f"- Base: {current.base_sha or 'unknown'}\n"
        "## Candidate Workstreams\n"
        f"- Count: {len(dossier.candidates)}\n"
        "## Evidence Claims\n"
        f"- [observed] {dossier.claims[0].text}\n"
        "## Validation State\n"
        "- Not run; recovery is read-only.\n"
        "## Coverage and Omissions\n"
        f"- Changed paths: {dossier.coverage.changed_path_count}; examined: 0; omitted: 0.\n"
        "## Next-Action Boundary\n"
        f"- {dossier.next_action_hint}\n"
    )
