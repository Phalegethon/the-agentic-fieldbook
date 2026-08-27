#!/usr/bin/python3
"""Hostile and valid newline-framed MCP stdio fixture."""

import json
import sys
import time


MODE = sys.argv[1]
TOOL = {
    "name": "query_graph",
    "description": "Query the bound graph",
    "inputSchema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    },
    "outputSchema": {
        "type": "object",
        "properties": {"nodes": {"type": "array"}},
        "required": ["nodes"],
    },
}


def read() -> dict[str, object]:
    return json.loads(sys.stdin.buffer.readline())


def send(value: object) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


request = read()
if MODE == "timeout":
    time.sleep(10)
if MODE == "early-exit":
    raise SystemExit(0)
if MODE == "invalid-utf8":
    sys.stdout.buffer.write(b"\xff\n")
    sys.stdout.buffer.flush()
    raise SystemExit(0)
if MODE == "invalid-json":
    sys.stdout.write("{\n")
    sys.stdout.flush()
    raise SystemExit(0)
if MODE == "partial-frame":
    sys.stdout.write('{"jsonrpc":"2.0"')
    sys.stdout.flush()
    raise SystemExit(0)
if MODE == "stdout-overflow":
    sys.stdout.write("x" * 8192)
    sys.stdout.flush()
    time.sleep(10)
if MODE == "stderr-overflow":
    sys.stderr.write("x" * 8192)
    sys.stderr.flush()
if MODE == "server-request":
    send({"jsonrpc": "2.0", "id": 90, "method": "roots/list"})
if MODE == "notification-flood":
    for number in range(9):
        send({"jsonrpc": "2.0", "method": "notifications/message", "params": {"n": number}})
if MODE == "wrong-jsonrpc":
    send({"jsonrpc": "1.0", "id": request["id"], "result": {}})
    raise SystemExit(0)
if MODE == "wrong-id":
    send({"jsonrpc": "2.0", "id": 999, "result": {}})
    raise SystemExit(0)
if MODE == "nonfinite":
    sys.stdout.write('{"jsonrpc":"2.0","id":1,"result":{"value":NaN}}\n')
    sys.stdout.flush()
    raise SystemExit(0)
if MODE == "duplicate-key":
    sys.stdout.write('{"jsonrpc":"2.0","id":1,"id":1,"result":{}}\n')
    sys.stdout.flush()
    raise SystemExit(0)

send({
    "jsonrpc": "2.0",
    "id": request["id"],
    "result": {
        "protocolVersion": request["params"]["protocolVersion"],
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "fixture", "version": "1.0.0"},
    },
})

initialized = read()
if initialized.get("method") != "notifications/initialized":
    raise SystemExit(2)

request = read()
tool = dict(TOOL)
if MODE == "schema-drift":
    tool["inputSchema"] = {"type": "array"}
if MODE == "unknown-tool":
    tool["name"] = "other"
tools = [tool, tool] if MODE == "duplicate-tool" else [tool]
listed = {"tools": tools}
if MODE == "pagination":
    listed["nextCursor"] = "more"
send({"jsonrpc": "2.0", "id": request["id"], "result": listed})

request = read()
if MODE == "tool-rpc-error":
    send({
        "jsonrpc": "2.0",
        "id": request["id"],
        "error": {"code": -32000, "message": "fixture failure"},
    })
elif MODE == "tool-is-error":
    send({
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": {"content": [{"type": "text", "text": "failed"}], "isError": True},
    })
elif MODE == "text-overflow":
    send({
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": {"content": [{"type": "text", "text": "x" * 8192}], "isError": False},
    })
elif MODE == "resource-content":
    send({
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": {"content": [{"type": "resource", "resource": {"uri": "file:///escape"}}]},
    })
else:
    send({
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": {
            "content": [{"type": "text", "text": '{"nodes":[]}'}],
            "structuredContent": {"nodes": []},
            "isError": False,
        },
    })

sys.stdin.buffer.read()
