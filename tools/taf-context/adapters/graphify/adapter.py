#!/usr/bin/python3
"""Read-only Graphify v0.9.50 adapter for the TAF Level 1 contract."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys


TOOL_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(TOOL_ROOT))

from adapters._shared.adapter_runtime import (  # noqa: E402
    AdapterRuntimeError,
    canonical,
    parse_object,
    provider_command,
    read_envelope,
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
from taf_context.mcp_stdio import (  # noqa: E402
    MCPPolicy,
    MCPToolSchema,
    call_mcp_tool,
)
from taf_context.models import Confidence, Freshness  # noqa: E402


PROVIDER_VERSION = "0.9.50"
PROVIDER_IDENTITY = "graphify"
ADAPTER_IDENTITY = "taf.graphify.v0_9_50"
UNKNOWN_HEAD = "0" * 40
UNKNOWN_DIRTY = "sha256:" + "0" * 64
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_GRAPH_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_ENTRIES = 200_000
MAX_NODE_LINES = 64
MD5 = re.compile(r"[0-9a-f]{32}\Z")
LOCATION = re.compile(r"L([1-9][0-9]*)(?:-L?([1-9][0-9]*))?\Z")

QUERY_GRAPH_SCHEMA = {
    "name": "query_graph",
    "description": (
        "Search the knowledge graph using BFS or DFS. Returns relevant nodes "
        "and edges as text context."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "Natural language question or keyword search",
            },
            "mode": {
                "type": "string",
                "enum": ["bfs", "dfs"],
                "default": "bfs",
                "description": "bfs=broad context, dfs=trace a specific path",
            },
            "depth": {
                "type": "integer",
                "default": 3,
                "description": "Traversal depth (1-6)",
            },
            "token_budget": {
                "type": "integer",
                "default": 2000,
                "description": "Max output tokens",
            },
            "context_filter": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional explicit edge-context filter, e.g. "
                    "['call', 'field']"
                ),
            },
            "project_path": {
                "type": "string",
                "description": (
                    "Absolute path to a project directory containing "
                    "graphify-out/graph.json. Optional — defaults to the graph "
                    "this server was started with."
                ),
            },
        },
        "required": ["question"],
    },
}
QUERY_GRAPH_DIGEST = MCPToolSchema.from_dict(QUERY_GRAPH_SCHEMA).schema_digest


@dataclass(frozen=True)
class ManifestEntry:
    mtime: float
    ast_hash: str
    semantic_hash: str


@dataclass(frozen=True)
class GraphState:
    graph_path: Path
    index_identity: str
    storage_bytes: int
    entries: dict[str, ManifestEntry]


def main() -> None:
    envelope = read_envelope()
    command = provider_command(envelope, "mcp-stdio")
    repository = _repository(envelope)
    state = _graph_state(command, repository)
    phase = envelope.get("phase")
    if phase == "inspect":
        snapshot = envelope.get("snapshot")
        if type(snapshot) is not dict:
            raise AdapterRuntimeError("invalid-snapshot")
        write_result(_inspection(snapshot, state))
        return
    if phase == "query":
        request_value = envelope.get("request")
        if type(request_value) is not dict:
            raise AdapterRuntimeError("invalid-request")
        request = Level1Request.from_dict(request_value)
        if request.index_identity != state.index_identity:
            raise AdapterRuntimeError("index-binding-mismatch")
        result = _query(command, repository, state, request)
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
        metadata = path.lstat()
    except (OSError, RuntimeError) as error:
        raise AdapterRuntimeError("invalid-repository") from error
    if path != resolved or not stat.S_ISDIR(metadata.st_mode):
        raise AdapterRuntimeError("invalid-repository")
    return resolved


def _graph_state(command, repository: Path) -> GraphState:
    environment = dict(command.environment)
    if environment.get("GRAPHIFY_QUERY_LOG_DISABLE") != "1" or any(
        name in environment
        for name in (
            "GRAPHIFY_QUERY_LOG",
            "GRAPHIFY_QUERY_LOG_ENABLE",
            "GRAPHIFY_QUERY_LOG_RESPONSES",
        )
    ):
        raise AdapterRuntimeError("query-logging-not-disabled")
    graph_candidates = []
    for argument in command.arguments:
        candidate = Path(argument)
        if candidate.is_absolute() and candidate.name == "graph.json":
            graph_candidates.append(candidate)
    if len(graph_candidates) != 1:
        raise AdapterRuntimeError("graph-binding-ambiguous")
    graph = _regular_canonical(graph_candidates[0], "graph-binding-invalid")
    if not any(_is_within(graph, root) for root in map(Path, command.state_roots)):
        raise AdapterRuntimeError("graph-binding-invalid")
    manifest_path = graph.with_name("manifest.json")
    if not any(
        _is_within(manifest_path, root) for root in map(Path, command.state_roots)
    ):
        raise AdapterRuntimeError("manifest-binding-invalid")
    manifest_raw = _read_regular(manifest_path, MAX_MANIFEST_BYTES, "manifest-invalid")
    manifest = parse_object(manifest_raw, "manifest-invalid")
    if not manifest or len(manifest) > MAX_MANIFEST_ENTRIES:
        raise AdapterRuntimeError("manifest-invalid")
    entries: dict[str, ManifestEntry] = {}
    for raw_path, raw_entry in manifest.items():
        path = _portable_path(raw_path, "manifest-path-invalid")
        if type(raw_entry) is not dict or set(raw_entry) != {
            "mtime", "seen", "ast_hash", "semantic_hash",
        }:
            raise AdapterRuntimeError("manifest-schema-drift")
        mtime, seen = raw_entry["mtime"], raw_entry["seen"]
        ast_hash = raw_entry["ast_hash"]
        semantic_hash = raw_entry["semantic_hash"]
        if (
            isinstance(mtime, bool)
            or not isinstance(mtime, (int, float))
            or not math.isfinite(mtime)
            or isinstance(seen, bool)
            or not isinstance(seen, (int, float))
            or not math.isfinite(seen)
            or type(ast_hash) is not str
            or type(semantic_hash) is not str
            or (ast_hash and MD5.fullmatch(ast_hash) is None)
            or (semantic_hash and MD5.fullmatch(semantic_hash) is None)
            or not (ast_hash or semantic_hash)
        ):
            raise AdapterRuntimeError("manifest-schema-drift")
        source = repository.joinpath(*PurePosixPath(path).parts)
        source = _regular_canonical(source, "manifest-source-missing")
        if not _is_within(source, repository):
            raise AdapterRuntimeError("manifest-path-invalid")
        if source.stat().st_mtime != float(mtime):
            raise AdapterRuntimeError("manifest-source-stale")
        entries[path] = ManifestEntry(float(mtime), ast_hash, semantic_hash)
    graph_digest, graph_bytes = _digest_regular(
        graph, MAX_GRAPH_BYTES, "graph-invalid"
    )
    if graph_bytes == 0:
        raise AdapterRuntimeError("graph-invalid")
    material = {
        "provider": f"{PROVIDER_IDENTITY}@{PROVIDER_VERSION}",
        "graph_sha256": graph_digest,
        "manifest_sha256": hashlib.sha256(canonical(manifest)).hexdigest(),
    }
    identity = "sha256:" + hashlib.sha256(canonical(material)).hexdigest()
    return GraphState(
        graph,
        identity,
        graph_bytes + len(manifest_raw),
        entries,
    )


def _inspection(
    snapshot: dict[str, object], state: GraphState
) -> dict[str, object]:
    for field in (
        "repository_identity", "worktree_identity", "head_sha",
        "dirty_fingerprint",
    ):
        if type(snapshot.get(field)) is not str:
            raise AdapterRuntimeError("invalid-snapshot")
    return {
        "schema_version": "1",
        "adapter_identity": ADAPTER_IDENTITY,
        "provider_identity": PROVIDER_IDENTITY,
        "provider_version": PROVIDER_VERSION,
        "repository_identity": snapshot["repository_identity"],
        "worktree_identity": snapshot["worktree_identity"],
        "committed_head": UNKNOWN_HEAD,
        "dirty_overlay_fingerprint": UNKNOWN_DIRTY,
        "index_identity": state.index_identity,
        "readiness": "partial",
        "capabilities": ["search-symbols"],
        "path_coverage": 0.0,
        "language_coverage": 0.0,
        "storage_bytes": state.storage_bytes,
        "reason_codes": [
            "coverage-denominator-unavailable",
            "index-head-unverifiable",
        ],
        "warnings": ["graph-manifest-not-snapshot-bound"],
    }


def _query(command, repository: Path, state: GraphState, request: Level1Request):
    if request.operation is not Level1Operation.SEARCH_SYMBOLS:
        raise AdapterRuntimeError("operation-unsupported")
    if request.filters.symbol_kinds:
        raise AdapterRuntimeError("symbol-kind-filter-unsupported")
    source_types = {item.value for item in request.filters.source_types}
    if source_types - {"source"}:
        raise AdapterRuntimeError("source-type-filter-unsupported")
    token_budget = max(
        128, min(4000, request.maximum_model_output_characters // 3)
    )
    policy = MCPPolicy(
        protocol_version="2025-06-18",
        expected_tool_schema_digest=QUERY_GRAPH_DIGEST,
        timeout_seconds=10.0,
        maximum_stdout_bytes=256 * 1024,
        maximum_stderr_bytes=64 * 1024,
        maximum_message_bytes=256 * 1024,
        maximum_notifications=8,
        maximum_content_characters=64 * 1024,
        environment=command.environment,
    )
    called = call_mcp_tool(
        (command.executable, *command.arguments),
        "query_graph",
        {
            "question": request.query or "",
            "mode": "bfs",
            "depth": 1,
            "token_budget": token_budget,
        },
        policy,
    )
    if called.structured_content is not None or len(called.text_content) != 1:
        raise AdapterRuntimeError("query-result-schema-drift")
    findings, omitted = _findings(
        called.text_content[0], repository, state, request
    )
    rendered = render_level1_result(
        request,
        status=Level1ResultStatus.PARTIAL,
        provider_version=PROVIDER_VERSION,
        index_identity=state.index_identity,
        freshness=Freshness.UNKNOWN,
        parser_versions=((PROVIDER_IDENTITY, PROVIDER_VERSION),),
        coverage=Level1Coverage(
            0.0, 0.0, len(state.entries), 0, 0, 0, ()
        ),
        ranked_findings=findings,
        warnings=("graph-manifest-not-snapshot-bound",),
        next_safe_action="verify-cited-evidence",
        provider_omitted_count=omitted,
    )
    return rendered.result


def _findings(
    text: str,
    repository: Path,
    state: GraphState,
    request: Level1Request,
) -> tuple[tuple[Level1Finding, ...], int]:
    if text == "No matching nodes found.":
        return (), 0
    if text.startswith("Error executing ") or text.startswith("Unknown tool:"):
        raise AdapterRuntimeError("provider-tool-error")
    graph_prefix = f"Graph: {state.graph_path.as_posix()} ("
    headers = [line for line in text.splitlines() if line.startswith("Graph: ")]
    if len(headers) != 1 or not headers[0].startswith(graph_prefix):
        raise AdapterRuntimeError("graph-response-binding-mismatch")
    header_match = re.fullmatch(
        re.escape(graph_prefix)
        + r"([1-9][0-9]*) nodes\) \| .* \| ([0-9]+) nodes found",
        headers[0],
    )
    if header_match is None:
        raise AdapterRuntimeError("query-result-schema-drift")
    total_nodes = int(header_match.group(2))
    node_lines = [line for line in text.splitlines() if line.startswith("NODE ")]
    if len(node_lines) > MAX_NODE_LINES or len(node_lines) > total_nodes:
        raise AdapterRuntimeError("query-subgraph-oversized")
    candidates = []
    seen = set()
    for line in node_lines:
        label, path, start, end = _node_line(line)
        entry = state.entries.get(path)
        if entry is None:
            raise AdapterRuntimeError("citation-not-indexed")
        source = repository.joinpath(*PurePosixPath(path).parts)
        digest = _md5_regular(source)
        if digest not in {entry.ast_hash, entry.semantic_hash}:
            raise AdapterRuntimeError("citation-source-stale")
        key = (path, start, end)
        if key in seen:
            raise AdapterRuntimeError("duplicate-citation")
        seen.add(key)
        language = _language(path)
        if language == "Text":
            continue
        if request.filters.languages and language not in request.filters.languages:
            continue
        if request.filters.path_prefixes and not any(
            path == prefix or path.startswith(prefix + "/")
            for prefix in request.filters.path_prefixes
        ):
            continue
        candidates.append((path, start, end, label, language))
    candidates.sort()
    selected = candidates[:request.maximum_results]
    findings = []
    for path, start, end, label, language in selected:
        material = f"{request.index_identity}\0{path}\0{start}\0{end}\0{label}"
        identity = "sha256:" + hashlib.sha256(
            material.encode("utf-8")
        ).hexdigest()
        findings.append(Level1Finding(
            len(findings) + 1,
            identity,
            path,
            start,
            end,
            language,
            _record_kind(path, label),
            Level1SourceType.SOURCE,
            label,
            "graphify@0.9.50",
            Confidence.UNCERTAIN,
            "",
        ))
    omitted = max(0, total_nodes - len(selected))
    return tuple(findings), omitted


def _node_line(line: str) -> tuple[str, str, int, int]:
    if line.count(" [src=") != 1 or line.count(" loc=") != 1:
        raise AdapterRuntimeError("query-result-schema-drift")
    label, remainder = line[5:].split(" [src=", 1)
    raw_path, remainder = remainder.split(" loc=", 1)
    if remainder.count(" community=") != 1 or not remainder.endswith("]"):
        raise AdapterRuntimeError("query-result-schema-drift")
    raw_location, _community = remainder[:-1].split(" community=", 1)
    if not label or len(label) > 512:
        raise AdapterRuntimeError("query-result-schema-drift")
    path = _portable_path(raw_path, "citation-invalid")
    match = LOCATION.fullmatch(raw_location)
    if match is None:
        raise AdapterRuntimeError("citation-missing")
    start = int(match.group(1))
    end = int(match.group(2) or match.group(1))
    if end < start:
        raise AdapterRuntimeError("citation-invalid")
    return label, path, start, end


def _portable_path(value: object, reason: str) -> str:
    if type(value) is not str or "\\" in value:
        raise AdapterRuntimeError(reason)
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or value.startswith("//")
        or ":" in path.parts[0]
        or path.as_posix() != value
    ):
        raise AdapterRuntimeError(reason)
    return value


def _regular_canonical(path: Path, reason: str) -> Path:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise AdapterRuntimeError(reason) from error
    if path != resolved or not stat.S_ISREG(metadata.st_mode):
        raise AdapterRuntimeError(reason)
    return resolved


def _read_regular(path: Path, maximum: int, reason: str) -> bytes:
    path = _regular_canonical(path, reason)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(os.fspath(path), flags)
    except OSError as error:
        raise AdapterRuntimeError(reason) from error
    try:
        before = os.fstat(descriptor)
        if before.st_size > maximum:
            raise AdapterRuntimeError(reason)
        chunks = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(raw) > maximum or _file_identity(before) != _file_identity(after):
            raise AdapterRuntimeError(reason)
        return raw
    finally:
        os.close(descriptor)


def _digest_regular(path: Path, maximum: int, reason: str) -> tuple[str, int]:
    path = _regular_canonical(path, reason)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(os.fspath(path), flags)
    except OSError as error:
        raise AdapterRuntimeError(reason) from error
    try:
        before = os.fstat(descriptor)
        if before.st_size > maximum:
            raise AdapterRuntimeError(reason)
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise AdapterRuntimeError(reason)
            digest.update(chunk)
        after = os.fstat(descriptor)
        if _file_identity(before) != _file_identity(after):
            raise AdapterRuntimeError(reason)
        return digest.hexdigest(), total
    finally:
        os.close(descriptor)


def _md5_regular(path: Path) -> str:
    raw = _read_regular(path, 16 * 1024 * 1024, "citation-source-invalid")
    return hashlib.md5(raw).hexdigest()


def _file_identity(metadata) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _record_kind(path: str, label: str) -> Level1RecordKind:
    if label == PurePosixPath(path).name:
        return Level1RecordKind.MODULE
    return Level1RecordKind.DEFINITION


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
