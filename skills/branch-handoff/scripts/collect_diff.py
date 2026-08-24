from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path

DEFAULT_FETCH_TIMEOUT = 20
UNTRACKED_TEXT_CHAR_LIMIT = 4096
UNTRACKED_TEXT_READ_BYTES = UNTRACKED_TEXT_CHAR_LIMIT * 4


class CollectorError(RuntimeError):
    pass


@dataclass(frozen=True)
class BaseResolution:
    requested: str
    sha: str
    source: str
    freshness: str
    remote: str | None
    warning: str | None = None


def run_command(
    argv: list[str], cwd: Path, timeout: int, input_data: bytes | None = None
) -> bytes:
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            input=input_data,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CollectorError(f"command failed: {argv[0]}: {error}") from error
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise CollectorError(message or f"command exited {result.returncode}")
    return result.stdout


def git(
    repo: Path,
    *args: str,
    timeout: int = DEFAULT_FETCH_TIMEOUT,
    input_data: bytes | None = None,
) -> bytes:
    return run_command(["git", *args], repo, timeout, input_data)


def resolve_ref(repo: Path, ref: str) -> str:
    try:
        return git(
            repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"
        ).decode().strip()
    except CollectorError as error:
        raise CollectorError(f"cannot resolve Git ref: {ref}") from error


def _remote_names(repo: Path) -> list[str]:
    return [name for name in git(repo, "remote").decode().splitlines() if name]


def _split_remote_base(base: str, remotes: list[str]) -> tuple[str | None, str]:
    remote_prefix = "refs/remotes/"
    if base.startswith(remote_prefix):
        remote, separator, branch = base[len(remote_prefix) :].partition("/")
        if separator and remote in remotes and branch:
            return remote, branch
    head_prefix = "refs/heads/"
    if base.startswith(head_prefix) and base[len(head_prefix) :]:
        return None, base[len(head_prefix) :]
    remote, separator, branch = base.partition("/")
    if separator and remote in remotes and branch:
        return remote, branch
    return None, base


def select_remote(repo: Path, base: str, requested: str | None) -> str | None:
    remotes = _remote_names(repo)
    if requested in remotes:
        return requested

    _, branch = _split_remote_base(base, remotes)
    try:
        upstream = git(repo, "config", "--get", f"branch.{branch}.remote").decode().strip()
    except CollectorError:
        upstream = ""
    if upstream in remotes:
        return upstream
    if "origin" in remotes:
        return "origin"
    if len(remotes) == 1:
        return remotes[0]
    return None


def _is_local_only_ref(repo: Path, base: str) -> bool:
    if base.startswith(("refs/heads/", "refs/remotes/")):
        return False
    if (
        base in {"HEAD", "FETCH_HEAD", "ORIG_HEAD", "MERGE_HEAD"}
        or base.startswith("refs/")
        or "^" in base
        or "~" in base
        or re.fullmatch(r"[0-9a-fA-F]{7,64}", base)
    ):
        return True
    try:
        git(repo, "show-ref", "--verify", "--quiet", f"refs/tags/{base}")
    except CollectorError:
        return False
    return True


def resolve_base(
    repo: Path,
    base: str,
    offline: bool,
    remote: str | None,
    fetch_timeout: int,
) -> BaseResolution:
    if _is_local_only_ref(repo, base):
        return BaseResolution(
            requested=base,
            sha=resolve_ref(repo, base),
            source="local",
            freshness="unverified",
            remote=None,
        )

    remotes = _remote_names(repo)
    base_remote, fetch_ref = _split_remote_base(base, remotes)
    selected = select_remote(repo, fetch_ref, remote if remote is not None else base_remote)
    cache_remote = selected if selected is not None else remote
    warning = None

    if selected is not None and not offline:
        try:
            git(
                repo,
                "fetch",
                "--quiet",
                "--no-tags",
                "--no-recurse-submodules",
                selected,
                fetch_ref,
                timeout=fetch_timeout,
            )
            return BaseResolution(
                requested=base,
                sha=resolve_ref(repo, "FETCH_HEAD"),
                source="fetched",
                freshness="verified",
                remote=selected,
            )
        except CollectorError as error:
            warning = f"fetch failed: {error}"

    if cache_remote is not None:
        try:
            return BaseResolution(
                requested=base,
                sha=resolve_ref(repo, f"refs/remotes/{cache_remote}/{fetch_ref}"),
                source="cached-remote",
                freshness="unverified",
                remote=cache_remote,
                warning=warning,
            )
        except CollectorError:
            pass

    return BaseResolution(
        requested=base,
        sha=resolve_ref(repo, fetch_ref),
        source="local",
        freshness="unverified",
        remote=selected,
        warning=warning,
    )


def merge_base(repo: Path, base_sha: str, head_sha: str) -> str:
    try:
        return git(repo, "merge-base", base_sha, head_sha).decode().strip()
    except CollectorError as error:
        raise CollectorError("no merge base; history may be shallow") from error


@dataclass(frozen=True)
class FileChange:
    status: str
    old_path: str | None
    path: str
    added: int | None
    deleted: int | None
    binary: bool
    classification: str
    source_kind: str
    cluster: str = ""
    redacted: bool = False


@dataclass(frozen=True)
class OptionalSource:
    path: str
    kind: str
    content: str
    truncated: bool
    redacted: bool
    empty: bool = False


@dataclass(frozen=True)
class WorktreeCollection:
    changes: list[FileChange]
    patch: str
    warnings: list[str]


def _decode_path(value: bytes) -> str:
    return value.decode("utf-8", "surrogateescape")


def parse_name_status_z(raw: bytes) -> list[tuple[str, str | None, str]]:
    fields = raw.split(b"\x00")
    changes: list[tuple[str, str | None, str]] = []
    index = 0
    while index < len(fields):
        status_bytes = fields[index]
        if not status_bytes:
            break
        status = _decode_path(status_bytes)
        index += 1
        if status.startswith(("R", "C")):
            old_path = _decode_path(fields[index])
            path = _decode_path(fields[index + 1])
            index += 2
        else:
            old_path = None
            path = _decode_path(fields[index])
            index += 1
        changes.append((status, old_path, path))
    return changes


def _numstat_count(value: bytes) -> int | None:
    return None if value == b"-" else int(value)


def parse_numstat_z(raw: bytes) -> dict[tuple[str | None, str], tuple[int | None, int | None]]:
    fields = raw.split(b"\x00")
    result: dict[tuple[str | None, str], tuple[int | None, int | None]] = {}
    index = 0
    while index < len(fields):
        record = fields[index]
        if not record:
            break
        added_bytes, deleted_bytes, path_bytes = record.split(b"\t", 2)
        counts = (_numstat_count(added_bytes), _numstat_count(deleted_bytes))
        index += 1
        if path_bytes:
            result[(None, _decode_path(path_bytes))] = counts
            continue
        old_path = _decode_path(fields[index])
        path = _decode_path(fields[index + 1])
        index += 2
        result[(old_path, path)] = counts
    return result


LOCK_FILENAMES = {
    "bun.lockb",
    "cargo.lock",
    "composer.lock",
    "flake.lock",
    "gemfile.lock",
    "go.sum",
    "gradle.lockfile",
    "mix.lock",
    "npm-shrinkwrap.json",
    "package-lock.json",
    "packages.lock.json",
    "pipfile.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "podfile.lock",
    "renv.lock",
    "uv.lock",
    "yarn.lock",
}
GENERATED_COMPONENTS = {"dist", "build", "generated", "coverage", ".next", "target"}
VENDORED_COMPONENTS = {"vendor", "node_modules"}


def _attribute_is_set(attributes: dict[str, str]) -> bool:
    return any(
        value not in {"unspecified", "unset", "false", "0", ""}
        for name, value in attributes.items()
        if name in {"generated", "linguist-generated"}
    )


def classify_path(path: str, *, binary: bool, attributes: dict[str, str]) -> str:
    parts = path.lower().split("/")
    filename = parts[-1]
    if binary:
        return "binary"
    if filename in LOCK_FILENAMES:
        return "lock"
    if (
        _attribute_is_set(attributes)
        or bool(set(parts) & GENERATED_COMPONENTS)
    ):
        return "generated"
    if ".min." in filename:
        return "minified"
    if bool(set(parts) & VENDORED_COMPONENTS):
        return "vendored"
    return "text"


def _changed_path_attributes(repo: Path, paths: list[str]) -> dict[str, dict[str, str]]:
    if not paths:
        return {}
    raw = git(
        repo,
        "check-attr",
        "-z",
        "--stdin",
        "generated",
        "linguist-generated",
        input_data=b"".join(path.encode("utf-8", "surrogateescape") + b"\x00" for path in paths),
    )
    fields = raw.split(b"\x00")
    attributes: dict[str, dict[str, str]] = {}
    for index in range(0, len(fields) - 2, 3):
        path_bytes, name_bytes, value_bytes = fields[index : index + 3]
        if not path_bytes:
            continue
        path = _decode_path(path_bytes)
        attributes.setdefault(path, {})[_decode_path(name_bytes)] = _decode_path(value_bytes)
    return attributes


def collect_committed_changes(repo: Path, start: str, end: str) -> tuple[list[FileChange], str]:
    return _collect_diff_with_args(
        repo, (resolve_ref(repo, start), resolve_ref(repo, end)), "committed"
    )


def _collect_diff_with_args(
    repo: Path, diff_args: tuple[str, ...], source_kind: str
) -> tuple[list[FileChange], str]:
    safe_diff_options = ("--no-ext-diff", "--no-textconv")
    name_status = parse_name_status_z(
        git(
            repo, "diff", *safe_diff_options,
            "--name-status", "-z", "-M", "-C", *diff_args, "--",
        )
    )
    numstat = parse_numstat_z(
        git(
            repo, "diff", *safe_diff_options,
            "--numstat", "-z", "-M", "-C", *diff_args, "--",
        )
    )
    path_attributes = _changed_path_attributes(repo, [path for _, _, path in name_status])
    patch = git(
        repo,
        "diff",
        "--patch",
        "--unified=3",
        "--no-color",
        *safe_diff_options,
        "-M",
        "-C",
        *diff_args,
        "--",
    ).decode("utf-8", "surrogateescape")
    changes: list[FileChange] = []
    for status, old_path, path in name_status:
        added, deleted = numstat.get((old_path, path), numstat.get((None, path), (None, None)))
        binary = added is None and deleted is None
        changes.append(
            FileChange(
                status=status,
                old_path=old_path,
                path=path,
                added=added,
                deleted=deleted,
                binary=binary,
                classification=classify_path(
                    path, binary=binary, attributes=path_attributes.get(path, {})
                ),
                source_kind=source_kind,
            )
        )
    return changes, patch


def _synthetic_untracked_patch(path: str, content: str | None) -> str:
    if content is None:
        return f"diff --git a/{path} b/{path}\nBinary files /dev/null and b/{path} differ\n"
    lines = content.splitlines(keepends=True)
    if not lines:
        lines = [""]
    added = "".join("+" + line if line.endswith("\n") else "+" + line + "\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n"
        f"+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{added}"
    )


def _read_regular_prefix(path: Path, byte_limit: int) -> tuple[bytes, bool]:
    if byte_limit < 1:
        raise CollectorError("read limit must be positive")
    if path.is_symlink():
        raise CollectorError(f"source is not a regular file: {path}")
    try:
        mode = path.stat().st_mode
    except OSError as error:
        raise CollectorError(f"cannot stat source: {path}") from error
    if not stat.S_ISREG(mode):
        raise CollectorError(f"source is not a regular file: {path}")
    try:
        with path.open("rb") as handle:
            sample = handle.read(byte_limit + 1)
    except OSError as error:
        raise CollectorError(f"cannot read source: {path}") from error
    return sample[:byte_limit], len(sample) > byte_limit


def _read_untracked_regular_prefix(path: Path, byte_limit: int) -> tuple[bytes, bool]:
    if byte_limit < 1:
        raise CollectorError("read limit must be positive")
    try:
        before = path.lstat()
    except OSError as error:
        raise CollectorError(f"cannot stat source: {path}") from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise CollectorError(f"source is not a regular file: {path}")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise CollectorError(f"cannot safely open source: {path}") from error
    try:
        opened = os.fstat(descriptor)
        try:
            after = path.lstat()
        except OSError as error:
            raise CollectorError(f"source changed while opening: {path}") from error
        identity = (before.st_dev, before.st_ino)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(after.st_mode)
            or not stat.S_ISREG(after.st_mode)
            or identity != (opened.st_dev, opened.st_ino)
            or identity != (after.st_dev, after.st_ino)
        ):
            raise CollectorError(f"source changed while opening: {path}")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            sample = handle.read(byte_limit + 1)
    finally:
        if descriptor != -1:
            os.close(descriptor)
    return sample[:byte_limit], len(sample) > byte_limit


def collect_worktree_changes(repo: Path, head: str) -> WorktreeCollection:
    head_sha = resolve_ref(repo, head)
    staged, staged_patch = _collect_diff_with_args(repo, ("--cached", head_sha), "staged")
    unstaged, unstaged_patch = _collect_diff_with_args(repo, (), "unstaged")
    paths = [item for item in git(repo, "ls-files", "--others", "--exclude-standard", "-z").split(b"\x00") if item]
    attributes = _changed_path_attributes(repo, [_decode_path(item) for item in paths])
    untracked: list[FileChange] = []
    untracked_sections: list[str] = []
    warnings: list[str] = []
    for raw_path in paths:
        path = _decode_path(raw_path)
        candidate = repo / path
        content: str | None = None
        binary = True
        try:
            raw, _ = _read_untracked_regular_prefix(candidate, UNTRACKED_TEXT_READ_BYTES)
        except CollectorError as error:
            warnings.append(f"untracked path omitted from payload: {path}: {error}")
        else:
            if b"\x00" not in raw:
                binary = False
                content = raw.decode("utf-8", "replace")[:UNTRACKED_TEXT_CHAR_LIMIT]
        untracked.append(
            FileChange(
                status="A", old_path=None, path=path, added=None, deleted=None,
                binary=binary,
                classification=classify_path(path, binary=binary, attributes=attributes.get(path, {})),
                source_kind="untracked",
            )
        )
        untracked_sections.append(_synthetic_untracked_patch(path, content))
    changes = [*staged, *unstaged, *untracked]
    if changes:
        warnings.insert(0, "dirty working tree included")
    return WorktreeCollection(changes, staged_patch + unstaged_patch + "".join(untracked_sections), warnings)


_CLUSTER_PREFIXES = ("auth", "payment", "data", "api", "config", "integration", "ui", "test", "area")
_GENERIC_DIRECTORIES = {"app", "lib", "src", "source", "server", "client"}
_SECRET_KEY_PATTERN = re.compile(
    r"(?im)^(\s*(?:export\s+)?(?:api_key|apikey|token|secret|password|passwd|credential|client_secret|private_key)\s*[:=]\s*).*$"
)
_PRIVATE_KEY_BEGIN_PATTERN = re.compile(
    r"^\s*-----BEGIN (?P<kind>[A-Z0-9 ]*PRIVATE KEY)-----\s*$"
)
_TRUNCATION_MARKER = "\n... evidence truncated by budget ...\n"
_MINIMUM_OPTIONAL_CONTENT = 1
_EMPTY_SOURCE_MARKER = "<empty source>"


def cluster_for(change: FileChange) -> str:
    path = change.path.lower()
    parts = [part for part in path.split("/") if part]
    text = " ".join(parts)
    if any(word in text for word in ("auth", "login", "identity", "permission", "session", "role")):
        prefix = "auth"
    elif any(word in text for word in ("payment", "billing", "checkout", "invoice", "charge")):
        prefix = "payment"
    elif change.status.startswith("D") or any(word in text for word in ("migration", "schema", "database", "persistence", "serialize", "model")):
        prefix = "data"
    elif any(word in text for word in ("api", "route", "controller", "endpoint", "handler")):
        prefix = "api"
    elif any(word in text for word in ("config", "deploy", "infra", "feature", ".env", "setting")):
        prefix = "config"
    elif any(word in text for word in ("integration", "webhook", "adapter", "client", "provider")):
        prefix = "integration"
    elif any(word in text for word in ("ui", "frontend", "component", "view", "page", "screen")) or path.endswith((".tsx", ".jsx", ".vue", ".svelte")):
        prefix = "ui"
    elif any(word in text for word in ("test", "spec")):
        prefix = "test"
    else:
        prefix = "area"

    directories = parts[:-1]
    meaningful = [part for part in directories if part not in _GENERIC_DIRECTORIES]
    suffix_parts = (meaningful or directories or [parts[-1] if parts else "root"])[:2]
    return f"{prefix}:{'/'.join(suffix_parts)}"


def risk_weight(change: FileChange) -> int:
    cluster = change.cluster or cluster_for(change)
    prefix = cluster.split(":", 1)[0]
    if prefix in {"auth", "payment"} or prefix == "data":
        return 5
    if prefix in {"api", "config", "integration"}:
        return 4
    if prefix == "ui":
        return 2
    return 1


def split_patch_sections(patch: str) -> list[str]:
    sections: list[str] = []
    current: list[str] | None = None
    for line in patch.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current is not None:
                sections.append("".join(current))
            current = [line]
        elif current is not None:
            current.append(line)
    if current is not None:
        sections.append("".join(current))
    return sections


def _line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    return ""


def _private_key_begin_kind(line: str) -> str | None:
    match = _PRIVATE_KEY_BEGIN_PATTERN.match(line.rstrip("\r\n"))
    return match.group("kind") if match else None


def _is_matching_private_key_end(line: str, kind: str) -> bool:
    return line.rstrip("\r\n").strip() == f"-----END {kind}-----"


def _redacted_private_key_line(line: str) -> str:
    ending = _line_ending(line)
    body = line[: -len(ending)] if ending else line
    indentation = body[: len(body) - len(body.lstrip())]
    return indentation + "<REDACTED>" + ending


def _redact_private_key_text(text: str) -> tuple[str, bool]:
    redacted_lines: list[str] = []
    key_kind: str | None = None
    changed = False
    for line in text.splitlines(keepends=True):
        if key_kind is None:
            key_kind = _private_key_begin_kind(line)
            if key_kind is not None:
                changed = True
            redacted_lines.append(line)
            continue
        if _is_matching_private_key_end(line, key_kind):
            key_kind = None
            redacted_lines.append(line)
            continue
        changed = True
        redacted_lines.append(_redacted_private_key_line(line))
    return "".join(redacted_lines), changed


def _redact_all_text(text: str) -> tuple[str, bool]:
    redacted, private_changed = _redact_private_key_text(text)
    redacted, assignment_count = _SECRET_KEY_PATTERN.subn(r"\1<REDACTED>", redacted)
    return redacted, bool(private_changed or assignment_count)


def redact_sensitive_text(text: str, *, added_only: bool) -> tuple[str, bool]:
    if not added_only:
        return _redact_all_text(text)

    changed = False
    redacted_lines: list[str] = []
    key_kind: str | None = None
    for line in text.splitlines(keepends=True):
        if not line.startswith("+") or line.startswith("+++"):
            redacted_lines.append(line)
            continue
        body = line[1:]
        if key_kind is None:
            key_kind = _private_key_begin_kind(body)
            if key_kind is None:
                redacted_body, line_changed = _redact_all_text(body)
                changed = changed or line_changed
                redacted_lines.append("+" + redacted_body)
                continue
            changed = True
            redacted_lines.append(line)
            continue
        if _is_matching_private_key_end(body, key_kind):
            key_kind = None
            redacted_lines.append(line)
            continue
        changed = True
        redacted_lines.append("+" + _redacted_private_key_line(body))
    return "".join(redacted_lines), changed


def redact_added_lines(section: str) -> tuple[str, bool]:
    return redact_sensitive_text(section, added_only=True)


def _truncate_text(text: str, limit: int, marker: str = _TRUNCATION_MARKER) -> str:
    if len(text) <= limit:
        return text
    if limit <= len(marker) + 2:
        return text[:limit]
    remaining = limit - len(marker)
    start_length = remaining // 2
    end_length = remaining - start_length
    return text[:start_length] + marker + text[-end_length:]


def load_optional_source(path: Path, kind: str, max_chars: int) -> OptionalSource:
    if max_chars < 1:
        raise CollectorError("optional source character limit must be positive")
    raw, has_more = _read_regular_prefix(path, max_chars * 4)
    if b"\x00" in raw:
        raise CollectorError(f"optional source is binary: {path}")
    text = raw.decode("utf-8", "replace")
    redacted_text, redacted = redact_sensitive_text(text, added_only=False)
    truncated = has_more or len(redacted_text) > max_chars
    return OptionalSource(
        path=str(path),
        kind=kind,
        content=_truncate_text(redacted_text, max_chars),
        truncated=truncated,
        redacted=redacted,
        empty=not redacted_text,
    )


def _optional_content(source: OptionalSource) -> str:
    return _EMPTY_SOURCE_MARKER if source.empty or not source.content else source.content


def _decode_git_path_token(token: str) -> str | None:
    if not token.startswith('"'):
        return token
    if len(token) < 2 or not token.endswith('"'):
        return None
    encoded = bytearray()
    index = 1
    end = len(token) - 1
    escapes = {"a": b"\a", "b": b"\b", "f": b"\f", "n": b"\n", "r": b"\r", "t": b"\t", "v": b"\v"}
    while index < end:
        character = token[index]
        if character != "\\":
            encoded.extend(character.encode("utf-8"))
            index += 1
            continue
        index += 1
        if index >= end:
            return None
        escape = token[index]
        if escape in escapes:
            encoded.extend(escapes[escape])
            index += 1
        elif escape in {'\\', '"'}:
            encoded.extend(escape.encode("ascii"))
            index += 1
        elif escape in "01234567" and index + 2 < end:
            octal = token[index : index + 3]
            if not all(value in "01234567" for value in octal):
                return None
            encoded.append(int(octal, 8))
            index += 3
        else:
            return None
    return encoded.decode("utf-8", "surrogateescape")


def _parse_git_header_paths(header: str) -> tuple[str, str] | None:
    prefix = "diff --git "
    if not header.startswith(prefix):
        return None
    tokens: list[str] = []
    index = len(prefix)
    while len(tokens) < 2:
        if index >= len(header):
            return None
        if header[index] == '"':
            start = index
            index += 1
            while index < len(header):
                if header[index] == "\\":
                    index += 2
                    continue
                if header[index] == '"':
                    index += 1
                    break
                index += 1
            else:
                return None
            token = header[start:index]
        else:
            start = index
            while index < len(header) and header[index] != " ":
                index += 1
            token = header[start:index]
        decoded = _decode_git_path_token(token)
        if decoded is None:
            return None
        tokens.append(decoded)
        if len(tokens) == 2:
            break
        if index >= len(header) or header[index] != " ":
            return None
        index += 1
    if index != len(header):
        return None
    return tokens[0], tokens[1]


def _metadata_path(line: str, prefix: str) -> str | None:
    if not line.startswith(prefix):
        return None
    value = line[len(prefix) :]
    if not value.startswith('"'):
        return _decode_git_path_token(value)
    index = 1
    while index < len(value):
        if value[index] == "\\":
            index += 2
            continue
        if value[index] == '"':
            token = value[: index + 1]
            suffix = value[index + 1 :]
            return _decode_git_path_token(token) if suffix in {"", "\t"} else None
        index += 1
    return None


def _header_matches_paths(header: str, old_path: str, new_path: str) -> bool:
    expected = f"diff --git {old_path} {new_path}"
    return header == expected or _parse_git_header_paths(header) == (old_path, new_path)


def _metadata_matches_path(line: str, prefix: str, expected_path: str) -> bool:
    if not line.startswith(prefix):
        return False
    value = line[len(prefix) :]
    return value in {expected_path, expected_path + "\t"} or _metadata_path(line, prefix) == expected_path


def _section_has_meaningful_diff(section: str, change: FileChange) -> bool:
    lines = section.splitlines()
    if not lines:
        return False
    old_path = change.old_path or change.path
    header_old = f"a/{old_path}"
    header_new = f"b/{change.path}"
    if not _header_matches_paths(lines[0], header_old, header_new):
        return False
    body = lines[1:]
    expected_old = "/dev/null" if change.status.startswith("A") else f"a/{old_path}"
    expected_new = "/dev/null" if change.status.startswith("D") else f"b/{change.path}"
    text_diff = (
        any(_metadata_matches_path(line, "--- ", expected_old) for line in body)
        and any(_metadata_matches_path(line, "+++ ", expected_new) for line in body)
        and any(line.startswith("@@ ") for line in body)
    )
    binary_diff = any(
        line.startswith("Binary files ") and line.endswith(" differ") for line in body
    ) or "GIT binary patch" in body
    mode_diff = (
        any(line.startswith("old mode ") for line in body)
        and any(line.startswith("new mode ") for line in body)
    ) or any(line.startswith(("new file mode ", "deleted file mode ")) for line in body)
    rename_diff = (
        any(line.startswith(("rename from ", "copy from ")) for line in body)
        and any(line.startswith(("rename to ", "copy to ")) for line in body)
    )
    return text_diff or binary_diff or mode_diff or rename_diff


def _evidence_excerpt(section: str, limit: int) -> str:
    if len(section) <= limit:
        return section
    first_line_end = section.find("\n") + 1
    header = section[:first_line_end] if first_line_end else ""
    minimum = len(header) + len(_TRUNCATION_MARKER) + 2
    if limit < minimum:
        return _truncate_text(section, limit)
    remainder = section[len(header):]
    remaining = limit - len(header) - len(_TRUNCATION_MARKER)
    start_length = remaining // 2
    end_length = remaining - start_length
    return header + remainder[:start_length] + _TRUNCATION_MARKER + remainder[-end_length:]


def _consolidate_changes(changes: list[FileChange]) -> list[FileChange]:
    grouped: dict[str, list[FileChange]] = {}
    order: list[str] = []
    for change in changes:
        if change.path not in grouped:
            grouped[change.path] = []
            order.append(change.path)
        grouped[change.path].append(change)

    classification_order = {
        "binary": 0,
        "generated": 1,
        "lock": 2,
        "minified": 3,
        "vendored": 4,
        "text": 5,
    }
    consolidated: list[FileChange] = []
    for path in order:
        group = grouped[path]
        statuses = list(dict.fromkeys(change.status for change in group))
        sources = list(dict.fromkeys(change.source_kind for change in group))
        added_values = [change.added for change in group]
        deleted_values = [change.deleted for change in group]
        consolidated.append(
            FileChange(
                status="+".join(statuses),
                old_path=next(
                    (change.old_path for change in group if change.old_path is not None),
                    None,
                ),
                path=path,
                added=(
                    sum(value for value in added_values if value is not None)
                    if all(value is not None for value in added_values)
                    else None
                ),
                deleted=(
                    sum(value for value in deleted_values if value is not None)
                    if all(value is not None for value in deleted_values)
                    else None
                ),
                binary=all(change.binary for change in group),
                classification=min(
                    (change.classification for change in group),
                    key=lambda value: classification_order.get(value, 99),
                ),
                source_kind="+".join(sources),
                redacted=any(change.redacted for change in group),
            )
        )
    return consolidated


def build_dossier(
    changes: list[FileChange],
    patch: str,
    max_chars: int,
    optional_sources: tuple[OptionalSource, ...] = (),
    preamble: str = "",
) -> tuple[str, list[FileChange], dict]:
    if max_chars < 4096:
        raise CollectorError("dossier character budget must be at least 4096")

    sections = split_patch_sections(patch)
    committed_indexes = [
        index for index, change in enumerate(changes) if change.source_kind == "committed"
    ]
    if len(sections) == len(changes):
        section_indexes = list(range(len(changes)))
    elif len(sections) == len(committed_indexes):
        section_indexes = committed_indexes
    else:
        raise CollectorError("patch/ledger section count mismatch")
    section_by_index = dict(zip(section_indexes, sections))
    for index, section in section_by_index.items():
        if not _section_has_meaningful_diff(section, changes[index]):
            raise CollectorError("malformed patch section")
    raw_updated: list[FileChange] = []
    for index, change in enumerate(changes):
        section = section_by_index.get(index, "")
        _, redacted = redact_added_lines(section) if section else (section, False)
        raw_updated.append(replace(change, redacted=redacted))
    updated = [
        replace(change, cluster=cluster_for(change))
        for change in _consolidate_changes(raw_updated)
    ]
    cluster_by_path = {change.path: change.cluster for change in updated}

    cluster_changes: dict[str, list[FileChange]] = {}
    for change in updated:
        cluster_changes.setdefault(change.cluster, []).append(change)
    cluster_names = sorted(cluster_changes)
    evidence_by_cluster = {cluster: "" for cluster in cluster_names}
    for index, change in enumerate(raw_updated):
        section = section_by_index.get(index, "")
        if change.classification == "text" and section:
            evidence_by_cluster[cluster_by_path[change.path]] += redact_added_lines(section)[0]

    summary_clusters = [
        {
            "name": cluster,
            "file_count": len(cluster_changes[cluster]),
            "weight": max(risk_weight(change) for change in cluster_changes[cluster]),
            "evidence_chars": 0,
        }
        for cluster in cluster_names
    ]
    file_lines = [
        f"- {change.path} | {change.status} | {change.classification} | {change.cluster}\n"
        for change in updated
    ]
    optional_metadata = [
        f"- {source.kind}: {source.path} (truncated={source.truncated}, redacted={source.redacted})\n"
        for source in optional_sources
    ]
    base = (
        preamble
        + "# Branch handoff evidence dossier\n"
        f"Files: {len(updated)}\n"
        "## Coverage metadata\n"
        + "".join(file_lines)
        + "## Optional sources\n"
        + "".join(optional_metadata)
    )
    evidence_names = [name for name in cluster_names if evidence_by_cluster[name]]
    minimum_contents = {
        name: min(len(evidence_by_cluster[name]), max(80, len(evidence_by_cluster[name].split("\n", 1)[0]) + len(_TRUNCATION_MARKER) + 2))
        for name in evidence_names
    }
    evidence_headers = {name: f"## Evidence: {name}\n" for name in evidence_names}
    optional_headers = [f"### {source.kind}: {source.path}\n" for source in optional_sources]
    optional_contents = [_optional_content(source) for source in optional_sources]
    optional_minimums = [
        len(content) if content == _EMPTY_SOURCE_MARKER else min(
            len(content), _MINIMUM_OPTIONAL_CONTENT
        )
        for content in optional_contents
    ]
    optional_required = sum(
        len(header) + minimum + 1
        for header, minimum in zip(optional_headers, optional_minimums)
    )
    required_minimum = len(base) + sum(
        len(evidence_headers[name]) + minimum_contents[name] for name in evidence_names
    ) + optional_required
    if required_minimum > max_chars:
        raise CollectorError(
            f"dossier budget too small; required minimum is {required_minimum} characters"
        )

    reserved_evidence = sum(
        len(evidence_headers[name]) + minimum_contents[name] for name in evidence_names
    )
    optional_allocations = list(optional_minimums)
    remaining = max_chars - len(base) - reserved_evidence - optional_required
    for index, content in enumerate(optional_contents):
        if remaining == 0:
            break
        addition = min(len(content) - optional_allocations[index], remaining)
        optional_allocations[index] += addition
        remaining -= addition
    optional_blocks = [
        header + _truncate_text(content, allocation) + "\n"
        for header, content, allocation in zip(optional_headers, optional_contents, optional_allocations)
    ]
    allocations = dict(minimum_contents)
    weights = {name: max(risk_weight(change) for change in cluster_changes[name]) for name in evidence_names}
    while remaining > 0 and evidence_names:
        progressed = False
        for name in evidence_names:
            if allocations[name] < len(evidence_by_cluster[name]):
                share = max(1, remaining * weights[name] // sum(weights.values()))
                addition = min(share, len(evidence_by_cluster[name]) - allocations[name], remaining)
                allocations[name] += addition
                remaining -= addition
                progressed = True
                if remaining == 0:
                    break
        if not progressed:
            break

    evidence_blocks = []
    for cluster in summary_clusters:
        name = cluster["name"]
        if name not in allocations:
            continue
        excerpt = _evidence_excerpt(evidence_by_cluster[name], allocations[name])
        cluster["evidence_chars"] = len(excerpt)
        evidence_blocks.append(evidence_headers[name] + excerpt)
    dossier = base + "".join(optional_blocks) + "".join(evidence_blocks)
    if len(dossier) > max_chars:
        raise CollectorError("dossier budget accounting error")
    summary = {
        "file_count": len(updated),
        "clusters": summary_clusters,
        "optional_sources": [
            {
                "kind": source.kind,
                "path": source.path,
                "truncated": source.truncated,
                "redacted": source.redacted,
            }
            for source in optional_sources
        ],
    }
    return dossier, updated, summary


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        os.replace(temporary, path)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def write_artifacts(
    scope: dict, changes: list[FileChange], dossier: str, summary: dict,
    warnings: list[str], output_dir: Path,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = output_dir / "coverage-ledger.jsonl"
    dossier_path = output_dir / "model-dossier.md"
    manifest_path = output_dir / "manifest.json"
    ordered_changes = sorted(changes, key=lambda change: (change.path, change.source_kind, change.old_path or ""))
    ledger = "".join(json.dumps(asdict(change), sort_keys=True, ensure_ascii=False) + "\n" for change in ordered_changes)
    classifications: dict[str, int] = {}
    sources: dict[str, int] = {}
    for change in changes:
        classifications[change.classification] = classifications.get(change.classification, 0) + 1
        sources[change.source_kind] = sources.get(change.source_kind, 0) + 1
    manifest = {
        "schema_version": 1,
        **scope,
        "artifact_paths": {
            "coverage_ledger": str(ledger_path),
            "model_dossier": str(dossier_path),
            "manifest": str(manifest_path),
        },
        "file_count": len(changes),
        "cluster_count": len(summary["clusters"]),
        "clusters": summary["clusters"],
        "classification_counts": dict(sorted(classifications.items())),
        "source_counts": dict(sorted(sources.items())),
        "evidence_budget": scope["evidence_budget"],
        "dossier_chars": len(dossier),
        "optional_sources": summary["optional_sources"],
        "redaction_count": sum(change.redacted for change in changes) + sum(
            source["redacted"] for source in summary["optional_sources"]
        ),
        "warnings": warnings,
    }
    _atomic_write(ledger_path, ledger)
    _atomic_write(dossier_path, dossier)
    _atomic_write(manifest_path, json.dumps(manifest, sort_keys=True, ensure_ascii=False) + "\n")
    return manifest


def _dossier_preamble(scope: dict, warnings: list[str]) -> str:
    return (
        "# Scope\n"
        f"Requested base: {scope['requested_base']}\n"
        f"Requested head: {scope['requested_head']}\n"
        f"Base SHA: {scope['base_sha']}\n"
        f"Head SHA: {scope['head_sha']}\n"
        f"Merge base: {scope['merge_base']}\n"
        f"Base freshness: {scope['freshness']} ({scope['base_source']})\n"
        "## Warnings\n"
        + "".join(f"- {warning}\n" for warning in warnings)
    )


def _output_is_within_repo(repo: Path, output_dir: Path) -> bool:
    try:
        output_dir.resolve().relative_to(repo.resolve())
    except ValueError:
        return False
    return True


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect bounded branch handoff evidence")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--base", default="main")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--remote")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--include-worktree", action="store_true")
    parser.add_argument("--max-patch-chars", type=int, default=120000)
    parser.add_argument("--fetch-timeout", type=int, default=DEFAULT_FETCH_TIMEOUT)
    parser.add_argument("--context-file", type=Path, action="append", default=[])
    parser.add_argument("--test-results-file", type=Path, action="append", default=[])
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse_args(argv)
        if args.max_patch_chars < 4096:
            raise CollectorError("dossier character budget must be at least 4096")
        repo = Path(git(args.repo, "rev-parse", "--show-toplevel").decode().strip()).resolve()
        output_dir = args.output_dir.resolve() if args.output_dir is not None else Path(tempfile.mkdtemp(prefix="branch-handoff-"))
        if _output_is_within_repo(repo, output_dir):
            raise CollectorError("output directory must not be inside the target repository")
        base = resolve_base(repo, args.base, args.offline, args.remote, args.fetch_timeout)
        head_sha = resolve_ref(repo, args.head)
        base_merge = merge_base(repo, base.sha, head_sha)
        committed, committed_patch = collect_committed_changes(repo, base_merge, head_sha)
        dirty = bool(git(repo, "status", "--porcelain=v1", "-z"))
        warnings: list[str] = []
        if base.warning:
            warnings.append(base.warning)
        if not committed:
            warnings.append("no committed changes found")
        worktree = WorktreeCollection([], "", [])
        if args.include_worktree:
            worktree = collect_worktree_changes(repo, head_sha)
            warnings.extend(worktree.warnings)
        elif dirty:
            warnings.append("dirty working tree excluded; rerun with --include-worktree to include it")
        optional: list[OptionalSource] = []
        for kind, paths in (("context", args.context_file), ("test-results", args.test_results_file)):
            for path in paths:
                try:
                    optional.append(load_optional_source(path, kind, args.max_patch_chars))
                except CollectorError as error:
                    warnings.append(f"optional {kind} source unavailable: {path}: {error}")
        scope = {
            "requested_base": args.base,
            "requested_head": args.head,
            "resolved_base": base.sha,
            "resolved_head": head_sha,
            "base_sha": base.sha,
            "head_sha": head_sha,
            "merge_base": base_merge,
            "freshness": base.freshness,
            "base_source": base.source,
            "dirty": dirty,
            "evidence_budget": args.max_patch_chars,
        }
        dossier, changes, summary = build_dossier(
            [*committed, *worktree.changes], committed_patch + worktree.patch,
            args.max_patch_chars, tuple(optional), _dossier_preamble(scope, warnings),
        )
        manifest = write_artifacts(scope, changes, dossier, summary, warnings, output_dir)
        print(json.dumps(manifest, sort_keys=True, ensure_ascii=False))
        return 0
    except (CollectorError, OSError) as error:
        print(f"collector: {error}", file=sys.stderr)
        return 2
    except SystemExit as error:
        return int(error.code)


if __name__ == "__main__":
    raise SystemExit(main())
