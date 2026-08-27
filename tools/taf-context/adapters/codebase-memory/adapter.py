#!/usr/bin/python3
"""Read-only Codebase Memory v0.10.8 adapter for the TAF Level 1 contract."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys


TOOL_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(TOOL_ROOT))

from adapters._shared.adapter_runtime import (  # noqa: E402
    AdapterRuntimeError,
    canonical,
    provider_command,
    read_envelope,
    run_child,
    run_json_tool,
    write_result,
)
from taf_context.level1_models import (  # noqa: E402
    Level1Coverage,
    Level1Finding,
    Level1Operation,
    Level1RecordKind,
    Level1Request,
    Level1ResultStatus,
    Level1SourceType,
)
from taf_context.level1_render import render_level1_result  # noqa: E402
from taf_context.models import Confidence, Freshness  # noqa: E402


PROVIDER_VERSION = "0.10.8"
PROVIDER_IDENTITY = "codebase-memory-mcp"
ADAPTER_IDENTITY = "taf.codebase-memory.v0_10_8"
UNKNOWN_HEAD = "0" * 40
UNKNOWN_DIRTY = "sha256:" + "0" * 64
LINE_RANGE = re.compile(r"([1-9][0-9]*)-([1-9][0-9]*)\Z")
LABELS = {
    "class": "Class",
    "function": "Function",
    "interface": "Interface",
    "method": "Method",
    "module": "Module",
    "variable": "Variable",
}


def main() -> None:
    envelope = read_envelope()
    command = provider_command(envelope, "cli-json")
    version = run_child(command, ("--version",)).decode("utf-8")
    if version != f"codebase-memory-mcp {PROVIDER_VERSION}":
        raise AdapterRuntimeError("provider-version-mismatch")
    phase = envelope.get("phase")
    repository = _repository(envelope)
    project, status, index_identity, storage = _inspect_state(
        command, repository
    )
    if phase == "inspect":
        snapshot = envelope.get("snapshot")
        if type(snapshot) is not dict:
            raise AdapterRuntimeError("invalid-snapshot")
        write_result(_inspection(snapshot, index_identity, storage))
        return
    if phase == "query":
        request_value = envelope.get("request")
        if type(request_value) is not dict:
            raise AdapterRuntimeError("invalid-request")
        request = Level1Request.from_dict(request_value)
        if request.index_identity != index_identity:
            raise AdapterRuntimeError("index-binding-mismatch")
        result = _query(command, project, status, request)
        write_result(result.to_dict())
        return
    raise AdapterRuntimeError("unsupported-phase")


def _repository(envelope: dict[str, object]) -> Path:
    value = envelope.get("repository_root")
    if type(value) is not str:
        raise AdapterRuntimeError("invalid-repository")
    path = Path(value)
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise AdapterRuntimeError("invalid-repository") from error
    if path != resolved or not resolved.is_dir():
        raise AdapterRuntimeError("invalid-repository")
    return resolved


def _inspect_state(command, repository: Path):
    listed = run_json_tool(
        command,
        "list_projects",
        {"offset": 0, "limit": 100, "include_details": True},
    )
    required = {"projects", "total", "offset", "limit", "returned", "has_more"}
    if not required.issubset(listed) or not set(listed).issubset(required | {"hint"}):
        raise AdapterRuntimeError("list-projects-schema-drift")
    projects = listed["projects"]
    if (
        type(projects) is not list
        or type(listed["has_more"]) is not bool
        or listed["has_more"]
        or listed["offset"] != 0
        or listed["limit"] != 100
        or listed["returned"] != len(projects)
        or listed["total"] != len(projects)
    ):
        raise AdapterRuntimeError("list-projects-incomplete")
    matches = []
    for item in projects:
        if type(item) is not dict or not {
            "name", "root_path", "nodes", "edges", "size_bytes"
        }.issubset(item):
            raise AdapterRuntimeError("list-projects-schema-drift")
        root_path = item["root_path"]
        if type(root_path) is not str:
            raise AdapterRuntimeError("list-projects-schema-drift")
        try:
            if Path(root_path).resolve(strict=True) == repository:
                matches.append(item)
        except (OSError, RuntimeError):
            continue
    if len(matches) != 1:
        raise AdapterRuntimeError("project-binding-ambiguous")
    match = matches[0]
    project = match["name"]
    storage = match["size_bytes"]
    if type(project) is not str or type(storage) is not int or storage < 0:
        raise AdapterRuntimeError("list-projects-schema-drift")
    status = run_json_tool(
        command, "index_status", {"project": project, "verbose": True}
    )
    status_root = status.get("root_path")
    try:
        status_root_matches = (
            type(status_root) is str
            and Path(status_root).resolve(strict=True) == repository
        )
    except (OSError, RuntimeError):
        status_root_matches = False
    if (
        status.get("project") != project
        or not status_root_matches
        or status.get("status") != "ready"
        or type(status.get("nodes")) is not int
        or status["nodes"] <= 0
        or type(status.get("edges")) is not int
        or not {"parse_partial", "skipped", "not_indexed", "git"}.issubset(status)
    ):
        raise AdapterRuntimeError("index-status-unusable")
    identity = "sha256:" + hashlib.sha256(canonical(status)).hexdigest()
    return project, status, identity, storage


def _inspection(
    snapshot: dict[str, object], index_identity: str, storage: int
) -> dict[str, object]:
    for field in (
        "repository_identity", "worktree_identity", "head_sha",
        "dirty_fingerprint",
    ):
        if type(snapshot.get(field)) is not str:
            raise AdapterRuntimeError("invalid-snapshot")
    head = snapshot["head_sha"]
    unknown_head = "0" * len(head) if len(head) in {40, 64} else UNKNOWN_HEAD
    return {
        "schema_version": "1",
        "adapter_identity": ADAPTER_IDENTITY,
        "provider_identity": PROVIDER_IDENTITY,
        "provider_version": PROVIDER_VERSION,
        "repository_identity": snapshot["repository_identity"],
        "worktree_identity": snapshot["worktree_identity"],
        "committed_head": unknown_head,
        "dirty_overlay_fingerprint": UNKNOWN_DIRTY,
        "index_identity": index_identity,
        "readiness": "partial",
        "capabilities": ["repository-map", "search-symbols"],
        "path_coverage": 0.0,
        "language_coverage": 0.0,
        "storage_bytes": storage,
        "reason_codes": [
            "coverage-denominator-unavailable",
            "index-head-unverifiable",
        ],
        "warnings": ["provider-live-git-is-not-index-freshness"],
    }


def _query(command, project: str, status: dict[str, object], request: Level1Request):
    if request.operation not in {
        Level1Operation.REPOSITORY_MAP,
        Level1Operation.SEARCH_SYMBOLS,
    }:
        raise AdapterRuntimeError("operation-unsupported")
    arguments: dict[str, object] = {
        "project": project,
        "format": "json",
        "limit": min(100, request.maximum_results + 1),
        "offset": 0,
    }
    if request.operation is Level1Operation.SEARCH_SYMBOLS:
        arguments["name_pattern"] = ".*" + re.escape(request.query or "") + ".*"
        kinds = request.filters.symbol_kinds
        if len(kinds) > 1 or (kinds and kinds[0] not in LABELS):
            raise AdapterRuntimeError("symbol-kind-filter-unsupported")
        if kinds:
            arguments["label"] = LABELS[kinds[0]]
    else:
        arguments["label"] = "File"
    prefixes = request.filters.path_prefixes
    if prefixes:
        joined = "|".join(re.escape(item) for item in prefixes)
        arguments["file_pattern"] = rf"^(?:{joined})(?:/|$).*"
    source_types = {item.value for item in request.filters.source_types}
    if source_types - {"source"}:
        raise AdapterRuntimeError("source-type-filter-unsupported")
    searched = run_json_tool(command, "search_graph", arguments)
    findings, provider_omitted = _findings(searched, request)
    coverage = _coverage(status)
    warnings = ["index-head-unverifiable"]
    if provider_omitted:
        warnings.append("provider-results-truncated")
    rendered = render_level1_result(
        request,
        status=Level1ResultStatus.PARTIAL,
        provider_version=PROVIDER_VERSION,
        index_identity=request.index_identity,
        freshness=Freshness.UNKNOWN,
        parser_versions=((PROVIDER_IDENTITY, PROVIDER_VERSION),),
        coverage=coverage,
        ranked_findings=findings,
        warnings=tuple(sorted(warnings)),
        next_safe_action="verify-cited-evidence",
        provider_omitted_count=provider_omitted,
    )
    return rendered.result


def _findings(
    value: dict[str, object], request: Level1Request
) -> tuple[tuple[Level1Finding, ...], int]:
    required = {"total", "count", "cols", "groups", "has_more"}
    if not required.issubset(value) or not set(value).issubset(required | {"hint"}):
        raise AdapterRuntimeError("search-schema-drift")
    if value["cols"] != ["name", "label", "lines", "in", "out"]:
        raise AdapterRuntimeError("search-schema-drift")
    total, count, groups, has_more = (
        value["total"], value["count"], value["groups"], value["has_more"]
    )
    if (
        type(total) is not int
        or type(count) is not int
        or type(groups) is not list
        or type(has_more) is not bool
        or not 0 <= count <= total
        or has_more is not (total > count)
    ):
        raise AdapterRuntimeError("search-schema-drift")
    candidates = []
    for group in groups:
        if type(group) is not dict or set(group) != {"qn_prefix", "file", "rows"}:
            raise AdapterRuntimeError("search-schema-drift")
        prefix, raw_path, rows = group["qn_prefix"], group["file"], group["rows"]
        if type(prefix) is not str or type(raw_path) is not str or type(rows) is not list:
            raise AdapterRuntimeError("search-schema-drift")
        path = _relative_path(raw_path)
        for row in rows:
            if type(row) is not list or len(row) != 5:
                raise AdapterRuntimeError("search-schema-drift")
            name, label, lines, inbound, outbound = row
            if (
                type(name) is not str
                or type(label) is not str
                or type(lines) is not str
                or type(inbound) is not int
                or type(outbound) is not int
            ):
                raise AdapterRuntimeError("search-schema-drift")
            match = LINE_RANGE.fullmatch(lines)
            if match is None:
                raise AdapterRuntimeError("citation-missing")
            start, end = int(match.group(1)), int(match.group(2))
            if end < start:
                raise AdapterRuntimeError("citation-invalid")
            language = _language(path)
            if request.filters.languages and language not in request.filters.languages:
                continue
            qualified = f"{prefix}.{name}" if prefix else name
            candidates.append((path, start, end, qualified, label, language))
    if sum(len(group["rows"]) for group in groups) != count:
        raise AdapterRuntimeError("search-count-mismatch")
    candidates.sort()
    findings = []
    identities = set()
    for path, start, end, qualified, label, language in candidates:
        material = f"{request.index_identity}\0{path}\0{start}\0{end}\0{qualified}"
        identity = "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()
        if identity in identities:
            raise AdapterRuntimeError("duplicate-citation")
        identities.add(identity)
        findings.append(Level1Finding(
            len(findings) + 1,
            identity,
            path,
            start,
            end,
            language,
            _record_kind(label),
            Level1SourceType.SOURCE,
            qualified,
            "codebase-memory-mcp@0.10.8",
            Confidence.UNCERTAIN,
            "",
        ))
        if len(findings) >= request.maximum_results:
            break
    omitted = max(0, total - len(findings))
    return tuple(findings), omitted


def _coverage(status: dict[str, object]) -> Level1Coverage:
    def count(field: str) -> int:
        value = status.get(field)
        if type(value) is not dict or type(value.get("count")) is not int:
            raise AdapterRuntimeError("coverage-schema-drift")
        if value.get("truncated") is True:
            raise AdapterRuntimeError("coverage-truncated")
        return value["count"]

    partial = count("parse_partial")
    skipped = count("skipped")
    return Level1Coverage(
        0.0,
        0.0,
        0,
        skipped,
        0,
        partial,
        tuple(sorted((key, value) for key, value in (
            ("parse-partial", partial), ("skipped", skipped)
        ) if value)),
    )


def _relative_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or ".." in path.parts
        or ":" in path.parts[0]
        or normalized.startswith("//")
    ):
        raise AdapterRuntimeError("citation-invalid")
    return path.as_posix()


def _record_kind(label: str) -> Level1RecordKind:
    if label in {"File", "Folder", "Module", "Package"}:
        return Level1RecordKind.MODULE
    if label == "Route":
        return Level1RecordKind.ENTRY_POINT
    if label in {"Class", "Function", "Interface", "Method", "Variable"}:
        return Level1RecordKind.DEFINITION
    raise AdapterRuntimeError("unsupported-node-label")


def _language(path: str) -> str:
    return {
        ".c": "C", ".cpp": "C++", ".go": "Go", ".java": "Java",
        ".js": "JavaScript", ".kt": "Kotlin", ".php": "PHP",
        ".py": "Python", ".rs": "Rust", ".ts": "TypeScript",
    }.get(PurePosixPath(path).suffix.lower(), "Text")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit(2)
