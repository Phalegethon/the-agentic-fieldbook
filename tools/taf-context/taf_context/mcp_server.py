"""MCP stdio server exposing bounded repository context tools over one engine session."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
import time
from typing import Any, BinaryIO, Callable, Mapping, Protocol, TextIO

from .context_operations import (
    FILTER_LANGUAGES,
    FILTER_SYMBOL_KINDS,
    IDENTITY_QUERY_OPERATIONS,
    MAXIMUM_RELATED_ANCHORS,
    PrepareCLIError,
    QUERY_DIRECTIONS,
    QueryArguments,
    _SHA256,
    default_maximum_results,
    default_output_characters,
    normalize_change_base,
    normalize_filter_values,
    run_build,
    run_inspect,
    run_query,
)
from .engine_session import Level1Session, SessionTransport
from .git_snapshot import SnapshotError
from .native_transport import NativeTransport

SERVER_NAME = "taf-repo-context"
SERVER_VERSION = "1.3.0"
LEGACY_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
MODERN_VERSION = "2026-07-28"
INSTRUCTIONS = (
    "Bounded repository context from the TAF native engine. Every tool takes the "
    "repository or worktree absolute path as `repo`. Results follow the "
    "prepare-repo-context skill's references/result-contract.md: `preview` is a "
    "display hint, not evidence; quote source only from source_snippets; follow "
    "`next_safe_action`. A query refreshes a bound index incrementally. When "
    "`inspect` reports `install-native-engine`, or state must be removed or "
    "reclaimed, use the prepare-repo-context skill. Never pass "
    "`confirm_state_write: true` to `build` without the user's explicit approval "
    "in this conversation."
)
_READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
_SOURCE_TYPES = ("source", "document", "configuration")
_OUTPUT_BUDGETS = (2000, 4000, 8000, 12000)
_QUERY_TOOLS = {
    "repository_map": "repository-map",
    "search_symbols": "search-symbols",
    "search_docs": "search-docs",
    "source_snippets": "source-snippets",
    "related_symbols": "related-symbols",
    "changed_symbols": "changed-symbols",
    "impact_candidates": "impact-candidates",
    "repository_overview": "repository-overview",
}


class Operations(Protocol):
    def inspect(self, repository: Path) -> dict[str, object]: ...

    def build(self, repository: Path) -> dict[str, object]: ...

    def query(self, repository: Path, arguments: QueryArguments) -> dict[str, object]: ...

    def close(self) -> None: ...


def _repo_property() -> dict[str, Any]:
    return {
        "type": "string",
        "description": "Absolute path of the Git repository or worktree.",
    }


def _base_property() -> dict[str, Any]:
    return {
        "type": "string",
        "description": (
            "Git ref or commit the change set is measured against. Defaults to "
            "the branch's upstream main, then origin/HEAD, then a local "
            "main/master; without staged, uncommitted changes are always "
            "included. Exclusive with staged."
        ),
    }


def _staged_property() -> dict[str, Any]:
    return {
        "type": "boolean",
        "default": False,
        "description": (
            "Measure the change set as the index against HEAD - what git commit "
            "would record - instead of a base ref. Untracked and unstaged edits "
            "are excluded. Exclusive with base."
        ),
    }


def _maximum_output_characters_property(default: int = 4000) -> dict[str, Any]:
    return {
        "type": "integer",
        "enum": list(_OUTPUT_BUDGETS),
        "default": default,
    }


def _maximum_results_property(default: int = 8) -> dict[str, Any]:
    return {"type": "integer", "minimum": 1, "maximum": 64, "default": default}


def _filter_properties() -> dict[str, Any]:
    return {
        "path_prefixes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Repository-relative path prefixes (case-sensitive).",
        },
        "languages": {
            "type": "array",
            "items": {"type": "string", "enum": sorted(FILTER_LANGUAGES)},
        },
        "symbol_kinds": {
            "type": "array",
            "items": {"type": "string", "enum": sorted(FILTER_SYMBOL_KINDS)},
        },
        "source_types": {
            "type": "array",
            "items": {"type": "string", "enum": list(_SOURCE_TYPES)},
        },
        "maximum_results": _maximum_results_property(),
        "maximum_output_characters": _maximum_output_characters_property(),
        "allow_inferred": {
            "type": "boolean",
            "default": False,
            "description": "Include findings whose evidence class is inferred.",
        },
    }


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def tool_definitions() -> list[dict[str, Any]]:
    """The ten tools, in `tools/list` order."""
    query_description_suffix = (
        " A query on a bound index behind the working tree refreshes it incrementally first."
    )
    return [
        {
            "name": "inspect",
            "title": "Inspect repository context",
            "description": (
                "Report native engine availability, index freshness, eligible and excluded "
                "path counts, state usage, required authorizations, and next_safe_action for "
                "a repository. Read-only apart from an incremental refresh of an already "
                "bound index."
            ),
            "inputSchema": _schema({"repo": _repo_property()}, ["repo"]),
            "annotations": dict(_READ_ONLY),
        },
        {
            "name": "build",
            "title": "Build repository index",
            "description": (
                "Build a fresh bounded index for the repository in user-local TAF state "
                "(never inside the repository). Requires confirm_state_write: true, which "
                "you may only pass after the user explicitly approved writing state in this "
                "conversation. If the repository is bound to an index an older runtime "
                "wrote, that record is removed under the same authorization before the "
                "new index is built, and the summary reports incompatible-generation. "
                "Query only when the result's next_safe_action is use-index."
            ),
            "inputSchema": _schema(
                {
                    "repo": _repo_property(),
                    "confirm_state_write": {
                        "type": "boolean",
                        "description": "Must be true; the user's explicit state-write authorization.",
                    },
                },
                ["repo", "confirm_state_write"],
            ),
            "annotations": {
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
            "_meta": {"anthropic/requiresUserInteraction": True},
        },
        {
            "name": "repository_map",
            "title": "Repository map",
            "description": (
                "One representative record per file, in path order; narrow with path_prefixes "
                "to answer 'what is in directory D'." + query_description_suffix
            ),
            "inputSchema": _schema(
                {"repo": _repo_property(), **_filter_properties()}, ["repo"]
            ),
            "annotations": dict(_READ_ONLY),
        },
        {
            "name": "search_symbols",
            "title": "Search symbols",
            "description": (
                "Find definitions, imports, modules, entry points, and configuration keys by "
                "name; every query word must match (camelCase and snake_case split). Use "
                'symbol_kinds: ["definition"] for \'where is X defined\' and ["import"] for '
                "'who imports X'." + query_description_suffix
            ),
            "inputSchema": _schema(
                {
                    "repo": _repo_property(),
                    "query": {
                        "type": "string",
                        "description": "Exact name or a few words; prefixes match.",
                    },
                    **_filter_properties(),
                },
                ["repo", "query"],
            ),
            "annotations": dict(_READ_ONLY),
        },
        {
            "name": "search_docs",
            "title": "Search documentation",
            "description": (
                "Find Markdown headings and document chunks; headings rank above body chunks. "
                "Use two or three words from the expected heading." + query_description_suffix
            ),
            "inputSchema": _schema(
                {"repo": _repo_property(), "query": {"type": "string"}, **_filter_properties()},
                ["repo", "query"],
            ),
            "annotations": dict(_READ_ONLY),
        },
        {
            "name": "source_snippets",
            "title": "Source snippets",
            "description": (
                "Fetch the verified source lines for result identities returned by an earlier "
                "query; the only tool whose output may be quoted as evidence. If the result is "
                "`partial` with `returned_count` 0 and `omitted_count` 1, the snippet did not fit "
                "the output budget: call again with a larger `maximum_output_characters` (8000 or "
                "12000)."
                + query_description_suffix
            ),
            "inputSchema": _schema(
                {
                    "repo": _repo_property(),
                    "result_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "description": "result_identity values from a previous query.",
                    },
                    "maximum_results": _filter_properties()["maximum_results"],
                    "maximum_output_characters": _filter_properties()["maximum_output_characters"],
                    "allow_inferred": _filter_properties()["allow_inferred"],
                },
                ["repo", "result_ids"],
            ),
            "annotations": dict(_READ_ONLY),
        },
        {
            "name": "related_symbols",
            "title": "Related symbols",
            "description": (
                "Follow the relationships of result identities returned by an earlier query: "
                "direction callers answers 'who calls X', callees 'what does X depend on', "
                "importers 'who uses module M', and imports 'what does X import'. Every finding "
                "carries relation, edge_evidence, reference_line, and reference_count; "
                "edge_evidence inferred is a name match, never proof, and is returned only with "
                "allow_inferred." + query_description_suffix
            ),
            "inputSchema": _schema(
                {
                    "repo": _repo_property(),
                    "result_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": MAXIMUM_RELATED_ANCHORS,
                        "description": "result_identity values of the anchor symbols or modules.",
                    },
                    "direction": {
                        "type": "string",
                        "enum": list(QUERY_DIRECTIONS),
                        "description": "Which side of the relationship to return.",
                    },
                    **_filter_properties(),
                },
                ["repo", "result_ids", "direction"],
            ),
            "annotations": dict(_READ_ONLY),
        },
        {
            "name": "changed_symbols",
            "title": "Changed symbols",
            "description": (
                "Answer 'what did I change on this branch': the definitions, entry points, "
                "and modules whose lines a changed hunk touches, between a base ref and the "
                "working tree (committed, staged, unstaged, and untracked changes). The "
                "result carries the resolved base; the warning changed-path-not-indexed "
                "means the change set reached files the index does not carry."
                + query_description_suffix
            ),
            "inputSchema": _schema(
                {
                    "repo": _repo_property(),
                    "base": _base_property(),
                    "staged": _staged_property(),
                    **_filter_properties(),
                },
                ["repo"],
            ),
            "annotations": dict(_READ_ONLY),
        },
        {
            "name": "impact_candidates",
            "title": "Impact candidates",
            "description": (
                "Answer 'what could my change break': the one-hop callers of the changed "
                "definitions and the importers of the changed modules and definitions, each "
                "candidate attributed to the changed symbols it depends on in `anchors`. Every "
                "candidate's edge is the strongest of its anchors; edge_evidence inferred is "
                "a name match, never proof, and is returned only with allow_inferred. Read "
                "`changed` for the change set itself. maximum_results defaults to 16 here, not "
                "the other tools' 8, because the candidates are what this operation answers "
                "and the output budget already bounds them. maximum_output_characters defaults to "
                "8000 here, not the other tools' 4000, because the answer carries the change "
                "set as well as the candidates. The two layers share that budget and neither "
                "pays for the other, cheapest loss first: an answer over budget takes the "
                "compact form (path, qualified_name) for every changed entry right away, and "
                "only a compact list still over its third of the budget loses entries from "
                "the tail, counted in changed_trimmed_count with the warning "
                "changed-list-trimmed, so a short `changed` list is always a counted one and "
                "changed_count still says how many symbols the change touched. The candidates "
                "then drop from the tail until the answer fits, and only an answer with no "
                "candidate and no changed entry left reports output-budget-exceeded. A "
                "trimmed or compacted change set is one changed_symbols call away, and every "
                "kept candidate names its changed symbols in `anchors` whatever the list lost."
                + query_description_suffix
            ),
            "inputSchema": _schema(
                {
                    "repo": _repo_property(),
                    "base": _base_property(),
                    "staged": _staged_property(),
                    "maximum_results": _maximum_results_property(16),
                    "maximum_output_characters": _maximum_output_characters_property(8000),
                    "allow_inferred": _filter_properties()["allow_inferred"],
                },
                ["repo"],
            ),
            "annotations": dict(_READ_ONLY),
        },
        {
            "name": "repository_overview",
            "title": "Repository overview",
            "description": (
                "Answer 'how is this repository organized, where is the code, where do I "
                "start': one row per directory in `groups` with its file, definition, "
                "entry-point, document, and configuration counts and its languages, plus a "
                "ranked file layer in `findings` that leads with entry points and well-known "
                "entry file names. `overview.root` names what was described and "
                "`overview.other_group_count` how many directories the row `*` folds "
                "together. Start here in an unfamiliar repository, then narrow with "
                "path_prefixes to answer 'what is in directory D'; several path_prefixes "
                "are not composed, so the first in sorted order becomes the root and the "
                "warning overview-root-first-prefix says so. A path_prefix names whole "
                "directory segments, so a file path or a partial segment answers with an "
                "empty table and the warning overview-root-not-a-directory. The group "
                "table has no fixed width: maximum_output_characters sizes it, and the "
                "table and the file layer take half of it each. A table wider than its "
                "half folds its tail into the row `*` until it fits, down to a single "
                "directory row; a table already inside its half is never folded and the "
                "file layer gets everything the table did not spend; findings then drop "
                "from the tail until the whole answer fits, and only a one-row table with "
                "no findings reports output-budget-exceeded. A larger budget therefore "
                "answers with a wider table as well as more files, while path_prefixes is "
                "the way to go deeper into one subtree. maximum_output_characters defaults "
                "to 8000 here, not the other tools' 4000, because the group table alone is "
                "about 3700 characters of canonical JSON, leaving room for the ranked file "
                "layer. maximum_results defaults to 24 here, not the other tools' 8, because "
                "the file layer is a repository-wide sample rather than a search result: 24 "
                "files give a fuller feel for an unfamiliar repository at the default budget."
                + query_description_suffix
            ),
            "inputSchema": _schema(
                {
                    "repo": _repo_property(),
                    "path_prefixes": _filter_properties()["path_prefixes"],
                    "languages": _filter_properties()["languages"],
                    "maximum_results": _maximum_results_property(24),
                    "maximum_output_characters": _maximum_output_characters_property(8000),
                    "allow_inferred": _filter_properties()["allow_inferred"],
                },
                ["repo"],
            ),
            "annotations": dict(_READ_ONLY),
        },
    ]


class InvalidArguments(ValueError):
    """A tool call whose arguments violate the schema (JSON-RPC -32602)."""


def _check_schema(schema: dict[str, Any], value: Any, path: str) -> None:
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            raise InvalidArguments(f"{path} must be an object")
        for name in schema.get("required", []):
            if name not in value:
                raise InvalidArguments(f"missing required argument {name}")
        for name, item in value.items():
            if name not in schema["properties"]:
                raise InvalidArguments(f"unknown argument {name}")
            _check_schema(schema["properties"][name], item, name)
    elif kind == "array":
        if not isinstance(value, list):
            raise InvalidArguments(f"{path} must be an array")
        if len(value) < schema.get("minItems", 0):
            raise InvalidArguments(f"{path} must not be empty")
        if len(value) > schema.get("maxItems", len(value)):
            raise InvalidArguments(f"{path} accepts at most {schema['maxItems']} items")
        for item in value:
            _check_schema(schema["items"], item, path)
    elif kind == "string":
        if not isinstance(value, str):
            raise InvalidArguments(f"{path} must be a string")
    elif kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidArguments(f"{path} must be an integer")
        if value < schema.get("minimum", value) or value > schema.get("maximum", value):
            raise InvalidArguments(f"{path} is out of range")
    elif kind == "boolean":
        if not isinstance(value, bool):
            raise InvalidArguments(f"{path} must be a boolean")
    if "enum" in schema and not _in_enum(schema["enum"], value):
        raise InvalidArguments(
            f"{path} must be one of {', '.join(str(item) for item in schema['enum'])}"
        )


def _in_enum(allowed: list[Any], value: Any) -> bool:
    # Enumerated filter values are canonicalized (lower-cased) after validation,
    # so accept any case here and let the canonicalization below settle the form.
    if isinstance(value, str):
        return value.strip().lower() in allowed
    return value in allowed


def _query_arguments(operation: str, arguments: dict[str, Any]) -> QueryArguments:
    query = arguments.get("query")
    if operation in {"search-symbols", "search-docs"} and (
        not isinstance(query, str) or not query.strip()
    ):
        raise InvalidArguments("query must not be empty")
    identities = tuple(sorted(set(arguments.get("result_ids", []))))
    if operation in IDENTITY_QUERY_OPERATIONS and any(
        _SHA256.fullmatch(item) is None for item in identities
    ):
        raise InvalidArguments("result_ids must be sha256:<64 hex> result identities")
    direction = arguments.get("direction")
    if direction is not None:
        # The schema enum accepts any case, as the filter values do; canonicalize
        # before the broker sees it.
        direction = direction.strip().lower()
    base = arguments.get("base")
    if base is not None:
        # Only the two change tools declare the key, so a base anywhere else is
        # already an unknown argument. The broker owns the normalization, so
        # the CLI resolves a padded base exactly as this surface does.
        try:
            base = normalize_change_base(base)
        except PrepareCLIError as exc:
            raise InvalidArguments("base must be a Git ref or commit") from exc
    staged = bool(arguments.get("staged", False))
    if staged and base is not None:
        # Only the two change tools declare the key, so an operation that
        # cannot accept staged is already an unknown argument; the only rule
        # left to enforce here is the same mutual exclusion the CLI refuses.
        raise InvalidArguments("staged and base are mutually exclusive")
    try:
        languages = normalize_filter_values(
            arguments.get("languages", []), "languages", FILTER_LANGUAGES
        )
        symbol_kinds = normalize_filter_values(
            arguments.get("symbol_kinds", []), "symbol_kinds", FILTER_SYMBOL_KINDS
        )
        source_types = normalize_filter_values(
            arguments.get("source_types", []), "source_types", frozenset(_SOURCE_TYPES)
        )
    except PrepareCLIError as exc:
        raise InvalidArguments(str(exc)) from exc
    return QueryArguments(
        operation=operation,
        query=query.strip() if isinstance(query, str) else None,
        result_identities=identities,
        direction=direction,
        base=base,
        staged=staged,
        path_prefixes=sorted(set(arguments.get("path_prefixes", []))),
        languages=languages,
        symbol_kinds=symbol_kinds,
        source_types=source_types,
        # Both operation defaults live in the broker, so this surface and the
        # CLI answer an unbudgeted request with the same result count and the
        # same output budget.
        maximum_results=int(
            arguments.get("maximum_results", default_maximum_results(operation))
        ),
        maximum_output_characters=int(
            arguments.get("maximum_output_characters", default_output_characters(operation))
        ),
        allow_inferred=bool(arguments.get("allow_inferred", False)),
    )


class _MethodNotFound(Exception):
    pass


class _Server:
    def __init__(self, stdout: BinaryIO, stderr: TextIO, operations: Operations) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self._operations = operations
        self._modern = False
        self._tools = {tool["name"]: tool for tool in tool_definitions()}

    # -- framing ---------------------------------------------------------

    def handle_line(self, raw: bytes) -> None:
        text = raw.strip()
        if not text:
            return
        try:
            message = json.loads(text.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._send(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
            )
            return
        if isinstance(message, list):
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "batch requests are not supported"},
                }
            )
            return
        if not isinstance(message, dict) or "method" not in message:
            return  # a response or an unknown shape addressed to us: nothing to answer
        method = message["method"]
        if "id" not in message:
            return  # notifications need no answer
        identifier = message["id"]
        params = message.get("params") or {}
        try:
            result = self._dispatch(method, params if isinstance(params, dict) else {})
        except _MethodNotFound:
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": identifier,
                    "error": {"code": -32601, "message": f"method not found: {method}"},
                }
            )
            return
        except InvalidArguments as exc:
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": identifier,
                    "error": {"code": -32602, "message": str(exc)},
                }
            )
            return
        if self._modern and isinstance(result, dict):
            result = {"resultType": "complete", **result}
        self._send({"jsonrpc": "2.0", "id": identifier, "result": result})

    def _send(self, message: dict[str, Any]) -> None:
        # Every byte on stdout stays ASCII, so no client's line reader can
        # mis-frame a message on a separator it happens to honour (U+2028 and
        # U+2029 split Python's own ``str.splitlines``).
        self._stdout.write(
            json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + b"\n"
        )
        self._stdout.flush()

    # -- methods ---------------------------------------------------------

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in LEGACY_VERSIONS else LEGACY_VERSIONS[-1]
            return {
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": INSTRUCTIONS,
            }
        if method == "server/discover":
            self._modern = True
            return {
                "supportedVersions": [MODERN_VERSION],
                "capabilities": {"tools": {}},
                "_meta": {
                    "io.modelcontextprotocol/serverInfo": {
                        "name": SERVER_NAME,
                        "version": SERVER_VERSION,
                    }
                },
                "instructions": INSTRUCTIONS,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": tool_definitions()}
        if method == "tools/call":
            return self._call(params)
        raise _MethodNotFound(method)

    def _call(self, params: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            return self._invoke(params, started)
        except InvalidArguments:
            # Spec §3 promises one diagnostic line per tool call, and an
            # argument rejection — a refused `confirm_state_write` above all —
            # is the one a host most needs to see. An unknown tool is named by
            # what the client asked for.
            self._log(f"{params.get('name')} {self._elapsed(started)}ms invalid-arguments")
            raise

    def _invoke(self, params: dict[str, Any], started: float) -> dict[str, Any]:
        name = params.get("name")
        tool = self._tools.get(name) if isinstance(name, str) else None
        if tool is None:
            raise InvalidArguments(f"unknown tool {name}")
        arguments = params.get("arguments", {})
        _check_schema(tool["inputSchema"], arguments, "arguments")
        if not os.path.isabs(arguments["repo"]):
            raise InvalidArguments("repo must be an absolute path")
        repository = Path(arguments["repo"])
        try:
            if name == "inspect":
                result = self._operations.inspect(repository)
            elif name == "build":
                if arguments["confirm_state_write"] is not True:
                    raise InvalidArguments("confirm_state_write must be true; ask the user first")
                result = self._operations.build(repository)
            else:
                result = self._operations.query(
                    repository, _query_arguments(_QUERY_TOOLS[name], arguments)
                )
            # Serialized inside the guard: a result the wire cannot carry is a
            # tool error like any other, never an uncaught exception that would
            # leave the request unanswered and end the session.
            text = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        except InvalidArguments:
            raise
        except (PrepareCLIError, SnapshotError, ValueError) as exc:
            self._log(f"{name} {self._elapsed(started)}ms error")
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        except OSError:
            self._log(f"{name} {self._elapsed(started)}ms error OSError")
            return {
                "content": [{"type": "text", "text": "context state is unavailable"}],
                "isError": True,
            }
        except Exception as exc:  # noqa: BLE001 - the server must survive any operation failure
            self._log(f"{name} {self._elapsed(started)}ms error {type(exc).__name__}")
            return {
                "content": [{"type": "text", "text": "repository context operation failed"}],
                "isError": True,
            }
        self._log(f"{name} {self._elapsed(started)}ms ok")
        return {"content": [{"type": "text", "text": text}], "structuredContent": result}

    @staticmethod
    def _elapsed(started: float) -> int:
        return int((time.perf_counter() - started) * 1000)

    def _log(self, line: str) -> None:
        self._stderr.write(line + "\n")
        self._stderr.flush()


def serve(stdin: BinaryIO, stdout: BinaryIO, stderr: TextIO, operations: Operations) -> int:
    """Run the JSON-RPC loop until stdin closes; always closes the operations."""
    server = _Server(stdout, stderr, operations)
    try:
        for raw in iter(stdin.readline, b""):
            server.handle_line(raw)
    finally:
        operations.close()
    return 0


class NativeOperations:
    """Production operations: broker flows over one lazily started engine session per binary."""

    def __init__(self, environment: Mapping[str, str], *, log: Callable[[str], None]) -> None:
        self._environment = dict(environment)
        self._sessions: dict[Path, Level1Session] = {}
        self._log = log

    def _transport_for(self, binary: Path) -> NativeTransport:
        session = self._sessions.get(binary)
        if session is None:
            session = Level1Session(binary, on_start=lambda pid: self._log(f"engine-start pid={pid}"))
            self._sessions[binary] = session
        return SessionTransport(session)

    def inspect(self, repository: Path) -> dict[str, object]:
        return run_inspect(
            repository, environment=self._environment, transport_for=self._transport_for
        )

    def build(self, repository: Path) -> dict[str, object]:
        return run_build(
            repository, environment=self._environment, transport_for=self._transport_for
        )

    def query(self, repository: Path, arguments: QueryArguments) -> dict[str, object]:
        return run_query(
            repository,
            arguments,
            environment=self._environment,
            transport_for=self._transport_for,
        )

    def close(self) -> None:
        for session in self._sessions.values():
            # One session that refuses to go away must not strand the others.
            try:
                session.close()
            except Exception as exc:  # noqa: BLE001 - shutdown closes everything it can
                self._log(f"engine-close failed {type(exc).__name__}")
        self._sessions.clear()


def main(argv: list[str] | None = None) -> int:
    """Entry point: stdio MCP server over the process environment."""
    if argv:
        sys.stderr.write("taf_context_mcp takes no arguments\n")
        return 2
    stderr = sys.stderr
    operations = NativeOperations(
        os.environ, log=lambda line: (stderr.write(line + "\n"), stderr.flush())
    )

    def _terminate(signum: int, _frame: object) -> None:
        operations.close()
        raise SystemExit(0)

    for name in ("SIGTERM", "SIGINT"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _terminate)
    return serve(sys.stdin.buffer, sys.stdout.buffer, stderr, operations)
