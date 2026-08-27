#!/usr/bin/python3
"""Adapter/child fixture for exact provider child isolation tests."""

import json
import os
import socket
import subprocess
import sys


role = sys.argv[1]
mode = sys.argv[2]
envelope = json.loads(sys.stdin.buffer.readline())

if role == "adapter":
    command = envelope["provider_command"]
    executable = command["executable"]
    arguments = command["arguments"]
    if mode == "wrong-child":
        executable, arguments = "/bin/echo", ["escape"]
    completed = subprocess.run(
        [executable, *arguments],
        input=(json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(command["environment"]),
        check=False,
    )
    sys.stderr.buffer.write(completed.stderr)
    sys.stdout.buffer.write(completed.stdout)
    raise SystemExit(completed.returncode)

if mode == "write-repository":
    with open(os.path.join(envelope["repository_root"], "escape.txt"), "w") as handle:
        handle.write("escape")
if mode == "write-state":
    with open(os.path.join(envelope["provider_command"]["state_roots"][0], "escape.txt"), "w") as handle:
        handle.write("escape")
if mode == "network":
    connection = socket.create_connection(("127.0.0.1", int(sys.argv[3])), timeout=1)
    connection.close()
if mode == "grandchild":
    subprocess.run(["/bin/echo", "escape"], check=True)

snapshot = envelope["snapshot"]
result = {
    "schema_version": "1",
    "adapter_identity": "fixture.command-json",
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
sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
