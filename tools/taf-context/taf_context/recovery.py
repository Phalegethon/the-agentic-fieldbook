"""Bounded, read-only evidence collection for interrupted Git work."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .git_snapshot import (
    _excluded_dirty_category,
    _git_environment,
    _open_regular_beneath,
    _same_file_state,
)
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
_ARTIFACT_BYTE_LIMIT = 64 * 1024
_CONTENT_EXCERPT_CHARS = 420
_SECRET_ASSIGNMENT = re.compile(
    r"(?im)^(\s*[a-z0-9_-]*(?:api[_-]?key|token|password|passwd|secret)[a-z0-9_-]*\s*[:=]\s*).*$"
)
_ABSOLUTE_PATH = re.compile(r"(?<![\w.-])/(?:Users|home|private|var|tmp)/[^\s'\"`]+")


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
    staged_paths, unstaged_paths, untracked_paths = _current_status_paths(root)
    staged, unstaged, untracked = len(staged_paths), len(unstaged_paths), len(untracked_paths)
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
    warning_set = {item for item in (base.warning,) if item}
    state_claim = RecoveryClaim.from_dict(
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
    diff_claims = _tracked_diff_claims(
        root,
        repository_identity,
        current_identity,
        staged_paths,
        unstaged_paths,
    )
    commit_claims = _committed_metadata_claims(
        root,
        repository_identity,
        current_identity,
        current,
    )
    untracked_claims, untracked_warnings = _untracked_claims(
        root,
        repository_identity,
        current_identity,
        untracked_paths,
        request.untracked_content_paths,
    )
    warning_set.update(untracked_warnings)
    dirty_fingerprint = _dirty_fingerprint(root, staged_paths, unstaged_paths, untracked_paths)
    artifact_claims = _artifact_claims(
        repository_identity,
        current_identity,
        current,
        dirty_fingerprint,
        request.note_files,
        request.test_result_files,
    )
    optional_claims = tuple(diff_claims + commit_claims + untracked_claims + artifact_claims)
    dossier, model_text = _budgeted_dossier(
        request.max_chars,
        repository_identity,
        current_identity,
        current,
        candidates,
        state_claim,
        optional_claims,
        tuple(sorted(warning_set)),
        len(set(staged_paths + unstaged_paths + untracked_paths)),
        len(set(staged_paths + unstaged_paths)) + len(set(request.untracked_content_paths)),
    )
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
    command = ["git", "--no-pager", *args]
    if args and args[0] == "diff":
        command[3:3] = ["--no-ext-diff", "--no-textconv"]
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


def _current_status_paths(repo: Path) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    raw = _run_git(repo, "status", "--porcelain=v2", "-z", "--branch", "--untracked-files=normal")
    assert raw is not None
    if raw and not raw.endswith(b"\x00"):
        raise RecoveryError("malformed Git status")
    staged: set[str] = set()
    unstaged: set[str] = set()
    untracked: set[str] = set()
    records = [] if not raw else raw[:-1].split(b"\x00")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if record.startswith(b"# "):
            continue
        if record.startswith(b"? "):
            untracked.add(_decode_path(record[2:]))
            continue
        if record.startswith(b"! "):
            continue
        if record.startswith((b"1 ", b"2 ", b"u ")) and len(record) >= 4:
            status = record[2:4]
            if record.startswith(b"1 "):
                parts = record.split(b" ", 8)
                path_raw = parts[8] if len(parts) == 9 else b""
            elif record.startswith(b"2 "):
                parts = record.split(b" ", 9)
                path_raw = parts[9] if len(parts) == 10 else b""
            else:
                parts = record.split(b" ", 10)
                path_raw = parts[10] if len(parts) == 11 else b""
            path = _decode_path(path_raw)
            if status[:1] not in (b".", b" "):
                staged.add(path)
            if status[1:2] not in (b".", b" "):
                unstaged.add(path)
            if record.startswith(b"2 "):
                if index >= len(records):
                    raise RecoveryError("malformed Git status")
                index += 1
            continue
        raise RecoveryError("malformed Git status")
    raw_untracked = _run_git(repo, "ls-files", "--others", "--exclude-standard", "-z")
    assert raw_untracked is not None
    if raw_untracked and not raw_untracked.endswith(b"\x00"):
        raise RecoveryError("malformed untracked path list")
    expanded_untracked = {
        _decode_path(item) for item in ([] if not raw_untracked else raw_untracked[:-1].split(b"\x00"))
    }
    return tuple(sorted(staged)), tuple(sorted(unstaged)), tuple(sorted(expanded_untracked))


def _decode_path(raw: bytes) -> str:
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RecoveryError("non-UTF-8 repository path") from error
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise RecoveryError("unsafe repository path")
    return value


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
    state, reasons = classify_recovery_state(
        staged_count=staged,
        unstaged_count=unstaged,
        untracked_count=untracked,
        ahead_count=ahead,
        behind_count=behind,
        head_reachable_from_base=ancestor,
        head_matches_base=head is not None and head == base,
        metadata_only=metadata_only,
        base_known=base is not None,
        head_known=head is not None,
    )
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
        reason_codes=reasons,
    )


def classify_recovery_state(
    *,
    staged_count: int,
    unstaged_count: int,
    untracked_count: int,
    ahead_count: int | None,
    behind_count: int | None,
    head_reachable_from_base: bool | None,
    head_matches_base: bool,
    metadata_only: bool,
    base_known: bool,
    head_known: bool,
) -> tuple[WorkState, tuple[str, ...]]:
    """Classify normalized evidence without consulting a filesystem or provider."""
    reasons: set[str] = set()
    if metadata_only:
        reasons.add("metadata-only")
    if staged_count or unstaged_count:
        reasons.add("dirty-tracked")
    if untracked_count:
        reasons.add("untracked-present")
    if staged_count or unstaged_count or untracked_count:
        state = WorkState.ACTIVE_DIRTY
    elif not base_known or not head_known:
        state = WorkState.CLEAN_UNRESOLVED
        reasons.add("base-or-head-unresolved")
    elif ahead_count and behind_count:
        state = WorkState.DIVERGED
        reasons.add("ahead-and-behind")
    elif head_reachable_from_base and not head_matches_base and metadata_only:
        state = WorkState.SUPERSEDED_STALE
        reasons.add("reachable-behind-base")
    elif head_reachable_from_base:
        state = WorkState.INTEGRATED
        reasons.add("head-reachable-from-base")
    elif ahead_count and ahead_count > 0:
        state = WorkState.ACTIVE_COMMITTED
        reasons.add("unique-commits")
    else:
        state = WorkState.UNKNOWN
        reasons.add("relation-unknown")
    return state, tuple(sorted(reasons))


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


def _tracked_diff_claims(
    repo: Path,
    repository_identity: str,
    worktree_identity: str,
    staged_paths: tuple[str, ...],
    unstaged_paths: tuple[str, ...],
) -> list[RecoveryClaim]:
    claims: list[RecoveryClaim] = []
    for kind, paths, cached in (
        ("staged", staged_paths, True),
        ("unstaged", unstaged_paths, False),
    ):
        for path in paths:
            arguments = ["diff", "--unified=3", "--no-renames", "--ignore-submodules=all"]
            if cached:
                arguments.append("--cached")
            arguments.extend(("--", path))
            raw = _run_git(repo, *arguments)
            assert raw is not None
            try:
                diff = raw.decode("utf-8")
            except UnicodeDecodeError:
                diff = "[binary-or-non-UTF-8 diff content excluded]"
            excerpt = _excerpt(_redact(diff))
            claims.append(
                RecoveryClaim.from_dict(
                    {
                        "claim_id": f"diff.{kind}.{_claim_path_id(path)}",
                        "evidence_class": "observed",
                        "text": f"Tracked {kind} diff for {path}: {excerpt}",
                        "repository_identity": repository_identity,
                        "worktree_identity": worktree_identity,
                        "provenance": [f"git/diff/{kind}/{path}"],
                        "freshness": "exact",
                        "supports": ["current.next-action"],
                        "conflicts": [],
                        "qualifications": [],
                    }
                )
            )
    return claims


def _committed_metadata_claims(
    repo: Path,
    repository_identity: str,
    worktree_identity: str,
    current: WorkstreamState,
) -> list[RecoveryClaim]:
    if not current.ahead_count or current.branch is None:
        return []
    subject = _single_line(
        repo,
        "for-each-ref",
        "--count=1",
        "--format=%(subject)",
        f"refs/heads/{current.branch}",
        allow_failure=True,
    )
    if subject is None:
        return []
    return [
        RecoveryClaim.from_dict(
            {
                "claim_id": "commit.tip-subject",
                "evidence_class": "observed",
                "text": f"Current branch tip subject: {_excerpt(_redact(subject))}",
                "repository_identity": repository_identity,
                "worktree_identity": worktree_identity,
                "provenance": ["git/ref/current-subject"],
                "freshness": "exact",
                "supports": ["current.next-action"],
                "conflicts": [],
                "qualifications": ["metadata-only"],
            }
        )
    ]


def _untracked_claims(
    repo: Path,
    repository_identity: str,
    worktree_identity: str,
    untracked_paths: tuple[str, ...],
    authorized_paths: tuple[str, ...],
) -> tuple[list[RecoveryClaim], set[str]]:
    authorized = _validated_relative_set(authorized_paths, "untracked authorization")
    unknown = authorized - set(untracked_paths)
    if unknown:
        raise ValueError("untracked authorization does not name an untracked path")
    claims: list[RecoveryClaim] = []
    warnings: set[str] = set()
    for path in untracked_paths:
        qualifications = ["metadata-only"]
        text = f"Untracked path {path}; content not inspected."
        if path in authorized:
            content, exclusion = _read_untracked(repo, path)
            if exclusion:
                qualifications = ["content-excluded", exclusion]
                text = f"Untracked path {path}; content-excluded ({exclusion})."
                warnings.add(f"untracked-{exclusion}")
            else:
                qualifications = ["content-authorized"]
                text = f"Authorized untracked content for {path}: {_excerpt(_redact(content))}"
        claims.append(
            RecoveryClaim.from_dict(
                {
                    "claim_id": f"untracked.{_claim_path_id(path)}",
                    "evidence_class": "observed",
                    "text": text,
                    "repository_identity": repository_identity,
                    "worktree_identity": worktree_identity,
                    "provenance": [f"git/untracked/{path}"],
                    "freshness": "exact",
                    "supports": ["current.next-action"],
                    "conflicts": [],
                    "qualifications": sorted(qualifications),
                }
            )
        )
    return claims, warnings


def _validated_relative_set(paths: tuple[str, ...], label: str) -> set[str]:
    if len(paths) != len(set(paths)):
        raise ValueError(f"duplicate {label}")
    result: set[str] = set()
    for value in paths:
        path = PurePosixPath(value)
        if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
            raise ValueError(f"invalid {label}")
        result.add(value)
    return result


def _read_untracked(repo: Path, relative: str) -> tuple[str, str | None]:
    path = repo / relative
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ValueError("untracked content metadata unavailable") from error
    excluded = _excluded_dirty_category(relative)
    if excluded is not None:
        return "", excluded
    if not stat.S_ISREG(metadata.st_mode):
        return "", "unsafe-file-type"
    if metadata.st_size > _ARTIFACT_BYTE_LIMIT:
        return "", "oversized"
    descriptor = -1
    parents: list[int] = []
    try:
        descriptor, parents = _open_regular_beneath(repo, relative)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not _same_file_state(metadata, before):
            return "", "changed-during-read"
        chunks: list[bytes] = []
        remaining = _ARTIFACT_BYTE_LIMIT + 1
        while remaining:
            chunk = os.read(descriptor, min(8192, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
    except OSError:
        return "", "unsafe-read"
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        for parent in reversed(parents):
            os.close(parent)
    if not _same_file_state(before, after):
        return "", "changed-during-read"
    raw = b"".join(chunks)
    if len(raw) > _ARTIFACT_BYTE_LIMIT or b"\x00" in raw:
        return "", "binary-or-oversized"
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        return "", "non-UTF-8"


def _artifact_claims(
    repository_identity: str,
    worktree_identity: str,
    current: WorkstreamState,
    dirty_fingerprint: str,
    note_files: tuple[Path, ...],
    test_result_files: tuple[Path, ...],
) -> list[RecoveryClaim]:
    if len(note_files) + len(test_result_files) > 8:
        raise ValueError("at most eight supplied artifacts are allowed")
    claims: list[RecoveryClaim] = []
    for index, path in enumerate(sorted((Path(item) for item in note_files), key=lambda item: str(item)), 1):
        text = _read_artifact(path)
        qualifications = []
        lowered = text.lower()
        if current.state is WorkState.ACTIVE_DIRTY and any(word in lowered for word in ("complete", "done", "finished")):
            qualifications.append("state-conflict")
        claims.append(
            _reported_claim(
                f"note.{index:02d}",
                f"Supplied note reports: {_excerpt(_redact(text))}",
                f"supplied/note-{index:02d}",
                repository_identity,
                worktree_identity,
                qualifications,
            )
        )
    for index, path in enumerate(sorted((Path(item) for item in test_result_files), key=lambda item: str(item)), 1):
        raw = _read_artifact(path)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError("malformed test result artifact") from error
        if type(parsed) is not dict or set(parsed) != {"head_sha", "dirty_fingerprint", "summary"}:
            raise ValueError("malformed test result artifact")
        if any(not isinstance(parsed[field], str) or not parsed[field] for field in parsed):
            raise ValueError("malformed test result artifact")
        current_validation = parsed["head_sha"] == current.head_sha and parsed["dirty_fingerprint"] == dirty_fingerprint
        qualification = "validation-current" if current_validation else "stale-validation"
        claims.append(
            _reported_claim(
                f"validation.{index:02d}",
                f"Supplied validation reports: {_excerpt(_redact(parsed['summary']))}",
                f"supplied/validation-{index:02d}",
                repository_identity,
                worktree_identity,
                [qualification],
            )
        )
    return claims


def _reported_claim(
    claim_id: str,
    text: str,
    provenance: str,
    repository_identity: str,
    worktree_identity: str,
    qualifications: list[str],
) -> RecoveryClaim:
    return RecoveryClaim.from_dict(
        {
            "claim_id": claim_id,
            "evidence_class": "reported",
            "text": text,
            "repository_identity": repository_identity,
            "worktree_identity": worktree_identity,
            "provenance": [provenance],
            "freshness": "unknown",
            "supports": ["current.next-action"],
            "conflicts": [],
            "qualifications": sorted(qualifications),
        }
    )


def _read_artifact(path: Path) -> str:
    try:
        before = path.lstat()
    except OSError as error:
        raise ValueError("supplied artifact unavailable") from error
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode) or before.st_size > _ARTIFACT_BYTE_LIMIT:
        raise ValueError("unsafe supplied artifact")
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if not _same_file_state(before, opened):
            raise ValueError("unsafe supplied artifact")
        raw = os.read(descriptor, _ARTIFACT_BYTE_LIMIT + 1)
        after = os.fstat(descriptor)
    except OSError as error:
        raise ValueError("unsafe supplied artifact") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not _same_file_state(opened, after) or len(raw) > _ARTIFACT_BYTE_LIMIT or b"\x00" in raw:
        raise ValueError("unsafe supplied artifact")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("invalid UTF-8 supplied artifact") from error


def _dirty_fingerprint(
    repo: Path,
    staged_paths: tuple[str, ...],
    unstaged_paths: tuple[str, ...],
    untracked_paths: tuple[str, ...],
) -> str:
    digest = hashlib.sha256()
    for cached, paths in ((True, staged_paths), (False, unstaged_paths)):
        if not paths:
            continue
        args = ["diff", "--unified=0", "--no-renames", "--ignore-submodules=all"]
        if cached:
            args.append("--cached")
        args.extend(("--", *paths))
        raw = _run_git(repo, *args)
        assert raw is not None
        digest.update(raw)
    for path in untracked_paths:
        metadata = (repo / path).lstat()
        digest.update(path.encode("utf-8"))
        digest.update(f":{metadata.st_mode}:{metadata.st_size}:{metadata.st_mtime_ns}".encode("ascii"))
    return "sha256:" + digest.hexdigest()


def _redact(text: str) -> str:
    if "-----BEGIN PRIVATE KEY-----" in text:
        return "[private-key-content-redacted]"
    text = _SECRET_ASSIGNMENT.sub(r"\1[redacted]", text)
    return _ABSOLUTE_PATH.sub("[absolute-path-redacted]", text)


def _excerpt(text: str) -> str:
    line_atomic = text.replace("\r", "").replace("\n", "\\n").strip()
    if len(line_atomic) > _CONTENT_EXCERPT_CHARS:
        return line_atomic[: _CONTENT_EXCERPT_CHARS - 1] + "…"
    return line_atomic or "[empty]"


def _claim_path_id(path: str) -> str:
    components = []
    for component in PurePosixPath(path).parts:
        normalized = re.sub(r"[^a-z0-9.]+", "-", component.lower())
        normalized = re.sub(r"\.+", ".", normalized).strip("-.") or "path"
        components.append(normalized)
    candidate = ".".join(components)
    if len(candidate) <= 96:
        return candidate
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
    return candidate[:79].rstrip(".-_:") + "." + digest


def _budgeted_dossier(
    budget: int,
    repository_identity: str,
    worktree_identity: str,
    current: WorkstreamState,
    candidates: tuple[WorkstreamState, ...],
    state_claim: RecoveryClaim,
    optional_claims: tuple[RecoveryClaim, ...],
    warnings: tuple[str, ...],
    changed_path_count: int,
    examined_path_count: int,
) -> tuple[RecoveryDossier, str]:
    optional_items: tuple[tuple[str, object, str], ...] = tuple(
        ("claim", claim, _claim_line(claim)) for claim in optional_claims
    ) + tuple(
        ("candidate", candidate, _candidate_line(candidate)) for candidate in candidates
    )
    optional_lines = tuple(item[2] for item in optional_items)
    selected: list[int] = []

    def build(indices: list[int]) -> tuple[RecoveryDossier, str]:
        selected_set = set(indices)
        omitted = [line for index, line in enumerate(optional_lines) if index not in selected_set]
        selected_claims = [
            optional_items[index][1]
            for index in indices
            if optional_items[index][0] == "claim"
        ]
        selected_candidates = [
            optional_items[index][1]
            for index in indices
            if optional_items[index][0] == "candidate"
        ]
        coverage = RecoveryCoverage(
            changed_path_count=changed_path_count,
            examined_path_count=min(examined_path_count, changed_path_count),
            cluster_count=len(optional_lines),
            included_cluster_count=len(indices),
            omitted_item_count=len(omitted),
            omitted_characters=sum(len(line) for line in omitted),
            budget_characters=budget,
        )
        dossier = RecoveryDossier.from_dict(
            {
                "schema_version": "1",
                "repository_identity": repository_identity,
                "worktree_identity": worktree_identity,
                "current": current.to_dict(),
                "candidates": [candidate.to_dict() for candidate in selected_candidates],
                "claims": [state_claim.to_dict()] + [claim.to_dict() for claim in selected_claims],
                "coverage": coverage.to_dict(),
                "warnings": list(warnings),
                "next_action_hint": _next_action(current),
            }
        )
        return dossier, _render_dossier(dossier)

    dossier, model_text = build(selected)
    if len(model_text) > budget:
        raise RecoveryError("recovery identity exceeds output budget")
    for index in range(len(optional_lines)):
        candidate_indices = [*selected, index]
        candidate_dossier, candidate_text = build(candidate_indices)
        if len(candidate_text) <= budget:
            selected = candidate_indices
            dossier, model_text = candidate_dossier, candidate_text
    return dossier, model_text


def _claim_line(claim: RecoveryClaim) -> str:
    qualifications = ",".join(claim.qualifications) or "none"
    return f"- [{claim.evidence_class.value}] {claim.claim_id}: {claim.text} (qualifications={qualifications})\n"


def _candidate_line(candidate: WorkstreamState) -> str:
    return f"- {candidate.branch or 'detached'}: {candidate.state.value} (metadata-only)\n"


def _render_dossier(dossier: RecoveryDossier) -> str:
    current = dossier.current
    claims = "".join(_claim_line(claim) for claim in dossier.claims)
    candidate_lines = "".join(_candidate_line(candidate) for candidate in dossier.candidates) or "- None included.\n"
    validation = [claim for claim in dossier.claims if claim.claim_id.startswith("validation.")]
    validation_line = (
        "- " + "; ".join(
            f"{claim.claim_id}={','.join(claim.qualifications) or 'reported'}" for claim in validation
        ) + "\n"
        if validation
        else "- Not run or supplied; recovery performs no validation.\n"
    )
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
        f"{candidate_lines}"
        "## Evidence Claims\n"
        f"{claims}"
        "## Validation State\n"
        f"{validation_line}"
        "## Coverage and Omissions\n"
        f"- Changed paths: {dossier.coverage.changed_path_count}; examined: {dossier.coverage.examined_path_count}.\n"
        f"- Clusters: {dossier.coverage.included_cluster_count}/{dossier.coverage.cluster_count}; omitted items: {dossier.coverage.omitted_item_count}; omitted characters: {dossier.coverage.omitted_characters}; budget: {dossier.coverage.budget_characters}.\n"
        "## Next-Action Boundary\n"
        f"- {dossier.next_action_hint}\n"
    )
