#!/usr/bin/python3
"""Synthetic Codebase Memory v0.10.8 CLI envelope fixture."""

import json
import os
import sys


MODE = sys.argv[1]
ROOT = os.environ["TAF_FIXTURE_ROOT"]

if sys.argv[2:] == ["--version"]:
    version = "0.10.7" if MODE == "wrong-version" else "0.10.8"
    print(f"codebase-memory-mcp {version}")
    raise SystemExit(0)

tool = sys.argv[4]
arguments = json.loads(sys.stdin.buffer.readline())


def emit(value, *, error=False):
    outer = {
        "content": [{"type": "text", "text": json.dumps(value, separators=(",", ":"))}],
        "isError": error,
    }
    print(json.dumps(outer, separators=(",", ":")))


if MODE == "provider-error":
    emit({"error": "must-not-escape"}, error=True)
    raise SystemExit(0)

if tool == "list_projects":
    project = {
        "name": "fixture-project",
        "root_path": ROOT,
        "nodes": 5,
        "edges": 4,
        "size_bytes": 4096,
    }
    projects = [project]
    if MODE == "missing-project":
        projects[0]["root_path"] = "/nonexistent/other"
    if MODE == "ambiguous-project":
        projects.append(dict(project, name="fixture-project-copy"))
    value = {
        "projects": projects,
        "total": len(projects),
        "offset": 0,
        "limit": 100,
        "returned": len(projects),
        "has_more": MODE == "list-pagination",
    }
    if MODE == "list-pagination":
        value["total"] = 101
    if MODE == "list-schema-drift":
        value["generation"] = "new-field"
    emit(value)
elif tool == "index_status":
    root = "/nonexistent/other" if MODE == "wrong-status-root" else ROOT
    value = {
        "project": arguments["project"],
        "nodes": 5,
        "edges": 4,
        "status": "ready",
        "root_path": root,
        "git": {"head_sha": "f" * 40},
        "parse_partial": {"files": [], "count": 0, "truncated": False},
        "skipped": {"files": [], "count": 0, "truncated": False},
        "not_indexed": {
            "dirs": [], "dirs_count": 0, "files": [],
            "files_count": 0, "truncated": False,
        },
    }
    if MODE == "coverage-truncated":
        value["skipped"]["truncated"] = True
    emit(value)
elif tool == "search_graph":
    path = "src\\widget.py" if MODE == "windows-path" else "src/widget.py"
    if MODE == "absolute-path":
        path = "/private/escape.py"
    lines = "" if MODE == "citation-missing" else "2-4"
    rows = [["Widget", "Class", lines, 1, 2]]
    if MODE == "duplicate-citation":
        rows.append(list(rows[0]))
    value = {
        "total": 7 if MODE == "query-pagination" else len(rows),
        "count": len(rows),
        "cols": ["name", "label", "lines", "in", "out"],
        "groups": [{"qn_prefix": "fixture", "file": path, "rows": rows}],
        "has_more": MODE == "query-pagination",
    }
    if MODE == "search-schema-drift":
        value["cols"] = ["name", "file"]
    emit(value)
else:
    emit({"error": "unexpected tool"}, error=True)
