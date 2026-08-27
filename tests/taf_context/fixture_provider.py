#!/usr/bin/python3
"""Deterministic stdio fixture for provider-process boundary tests."""

import json
import os
import socket
import sys
import time


mode = sys.argv[1]
request = json.loads(sys.stdin.buffer.readline())

if mode == "timeout":
    time.sleep(5)
if mode == "nonzero":
    raise SystemExit(17)
if mode == "write-repo":
    with open(os.path.join(request["repository_root"], "escape.txt"), "w") as handle:
        handle.write("escape")
if mode == "network":
    connection = socket.create_connection(("127.0.0.1", int(sys.argv[2])), timeout=1)
    connection.close()
if mode == "mutate-self":
    with open(__file__, "a") as handle:
        handle.write("# mutation")

if request["phase"] == "inspect":
    snapshot = request["snapshot"]
    result = {
        "schema_version": "1",
        "adapter_identity": "fixture.stdio",
        "provider_identity": "fixture.graph",
        "provider_version": "2.0.0",
        "repository_identity": snapshot["repository_identity"],
        "worktree_identity": snapshot["worktree_identity"],
        "committed_head": snapshot["head_sha"],
        "dirty_overlay_fingerprint": snapshot["dirty_fingerprint"],
        "index_identity": "sha256:" + "d" * 64,
        "readiness": "ready",
        "capabilities": ["repository-map", "search-symbols"],
        "path_coverage": 1.0,
        "language_coverage": 1.0,
        "storage_bytes": 4096,
        "reason_codes": [],
        "warnings": [],
    }
else:
    item = request["request"]
    result = {
        "schema_version": "1",
        "request_identity": item["request_identity"],
        "operation": item["operation"],
        "status": "ready",
        "provider_identity": item["provider_identity"],
        "provider_version": "2.0.0",
        "index_identity": item["index_identity"],
        "repository_identity": item["repository_identity"],
        "worktree_identity": item["worktree_identity"],
        "committed_head": item["committed_head"],
        "dirty_overlay_fingerprint": item["dirty_overlay_fingerprint"],
        "freshness": "exact",
        "parser_versions": {"fixture": "1.0.0"},
        "coverage": {
            "path_coverage": 1.0,
            "language_coverage": 1.0,
            "indexed_path_count": 1,
            "excluded_path_count": 0,
            "unsupported_language_count": 0,
            "parse_failure_count": 0,
            "exclusion_reason_counts": {},
        },
        "findings": [],
        "returned_count": 0,
        "omitted_count": 0,
        "truncated": False,
        "output_characters": 0,
        "warnings": [],
        "next_safe_action": "use-cited-evidence",
    }

    if mode == "wrong-request":
        result["request_identity"] = "wrong-request"
    if mode == "wrong-repository":
        result["repository_identity"] = "sha256:" + "e" * 64
    if mode == "wrong-worktree":
        result["worktree_identity"] = "sha256:" + "e" * 64
    if mode == "wrong-index":
        result["index_identity"] = "sha256:" + "e" * 64
    if mode == "budget-overflow":
        result["output_characters"] = 8000
    if mode in {
        "citation-range", "citation-absolute", "citation-traversal",
        "too-many-results",
    }:
        count = 11 if mode == "too-many-results" else 1
        findings = []
        for rank in range(1, count + 1):
            path = "README.md"
            if mode == "citation-absolute":
                path = "/etc/passwd"
            elif mode == "citation-traversal":
                path = "../README.md"
            findings.append(
                {
                    "rank": rank,
                    "result_identity": "sha256:" + format(rank, "064x"),
                    "path": path,
                    "start_line": 1,
                    "end_line": 999 if mode == "citation-range" else 1,
                    "language": "Markdown",
                    "record_kind": "heading",
                    "source_type": "document",
                    "qualified_name": "Fixture",
                    "extraction_method": "fixture",
                    "evidence_class": "verified",
                    "preview": "# Fixture",
                }
            )
        result["findings"] = findings
        result["returned_count"] = count
        result["output_characters"] = 200

wire = json.dumps(result, sort_keys=True, separators=(",", ":"))
if mode == "wrong-identity":
    wire = wire.replace("fixture.graph", "wrong.provider")
if mode == "duplicate":
    wire = wire.replace("{", '{"schema_version":"1",', 1)
if mode == "nonfinite":
    wire = wire.replace('"path_coverage":1.0', '"path_coverage":NaN')
if mode == "oversized-stderr":
    sys.stderr.write("x" * 70000)
if mode == "secret-stderr":
    sys.stderr.write("token=must-not-escape")
if mode == "oversized-stdout":
    sys.stdout.write("x" * 270000 + "\n")
elif mode == "invalid-utf8":
    sys.stdout.buffer.write(b"\xff\n")
if mode == "multiple":
    sys.stdout.write(wire + "\n" + wire + "\n")
elif mode not in {"oversized-stdout", "invalid-utf8"}:
    sys.stdout.write(wire + "\n")
