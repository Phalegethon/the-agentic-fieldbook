#!/usr/bin/python3
"""Synthetic Graphify v0.9.50 MCP wire fixture."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys


MODE = sys.argv[1]
GRAPH = Path(sys.argv[2]).resolve()

QUERY_GRAPH = {
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


def read() -> dict[str, object]:
    return json.loads(sys.stdin.buffer.readline())


def send(value: object) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


request = read()
send({
    "jsonrpc": "2.0",
    "id": request["id"],
    "result": {
        "protocolVersion": request["params"]["protocolVersion"],
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "graphify", "version": "0.9.50"},
    },
})

initialized = read()
if initialized.get("method") != "notifications/initialized":
    raise SystemExit(2)

request = read()
tool = json.loads(json.dumps(QUERY_GRAPH))
if MODE == "schema-drift":
    tool["inputSchema"]["properties"]["depth"]["default"] = 4
send({"jsonrpc": "2.0", "id": request["id"], "result": {"tools": [tool]}})

request = read()
if request["params"]["name"] != "query_graph":
    raise SystemExit(3)
arguments = request["params"]["arguments"]
if (
    arguments.get("mode") != "bfs"
    or arguments.get("depth") != 1
    or not 128 <= arguments.get("token_budget", 0) <= 4000
):
    raise SystemExit(4)
if os.environ.get("GRAPHIFY_QUERY_LOG_DISABLE") != "1":
    GRAPH.with_name("query.log").write_text("logged", encoding="utf-8")

graph_name = str(GRAPH)
if MODE == "wrong-graph":
    graph_name = str(GRAPH.with_name("other.json"))
header = (
    f"Graph: {graph_name} (3 nodes) | Traversal: BFS depth=1 | "
    "Start: ['Widget'] | 2 nodes found"
)
nodes = [
    "NODE helper [src=src/widget.py loc=L4 community=Core]",
    "NODE Widget [src=src/widget.py loc=L2-L3 community=Core]",
]
edge = "EDGE Widget --calls [EXTRACTED]--> helper at=src/widget.py:L4"

if MODE == "no-match":
    result = "No matching nodes found."
elif MODE == "provider-error":
    result = "Error executing query_graph: corrupt graph"
elif MODE == "absolute-path":
    nodes[0] = "NODE helper [src=/tmp/escape.py loc=L1 community=Core]"
    result = header + "\n\n" + "\n".join(nodes + [edge])
elif MODE == "citation-missing":
    nodes[0] = "NODE helper [src=src/widget.py loc=unknown community=Core]"
    result = header + "\n\n" + "\n".join(nodes + [edge])
elif MODE == "duplicate-citation":
    nodes[1] = "NODE WidgetAlias [src=src/widget.py loc=L4 community=Core]"
    result = header + "\n\n" + "\n".join(nodes + [edge])
elif MODE == "inferred-edge":
    edge = "EDGE Widget --uses [INFERRED]--> helper at=src/widget.py:L4"
    result = header + "\n\n" + "\n".join(nodes + [edge])
elif MODE == "truncated":
    header = header.replace("2 nodes found", "7 nodes found")
    result = (
        "[!] TRUNCATED: showing 2 of 7 nodes (~666-token budget).\n\n"
        + header + "\n\n" + "\n".join(nodes)
        + "\n... (truncated — 5 more nodes cut by ~666-token budget.)"
    )
elif MODE == "excessive-subgraph":
    lines = [
        f"NODE Node{number:02d} [src=src/widget.py loc=L4 community=Core]"
        for number in range(65)
    ]
    result = header.replace("2 nodes found", "65 nodes found") + "\n\n" + "\n".join(lines)
else:
    result = header + "\n\n" + "\n".join(nodes + [edge])

send({
    "jsonrpc": "2.0",
    "id": request["id"],
    "result": {
        "content": [{"type": "text", "text": result}],
        "isError": False,
    },
})
sys.stdin.buffer.read()
