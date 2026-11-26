"""A hand-written 2024-11-05 server that, like many small servers, never answers a method it does not know.

It is the server in north's own tests (tests/unit/mcp/test_mcp_client_connection.py), with `echo` and `big`,
and with the `serverInfo.version` the spec requires (the SDK client refuses a server without it).
With `--answers` it answers an unknown method with JSON-RPC's "method not found", as the spec says it must.
"""

import json
import sys

ANSWERS = "--answers" in sys.argv


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    req = json.loads(line)
    method, rid = req.get("method"), req.get("id")
    if method == "initialize":
        send(
            {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "x", "version": "1"},
                },
            }
        )
    elif method == "tools/list":
        tools = [{"name": n, "inputSchema": {"type": "object"}} for n in ("echo", "big")]
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": tools}})
    elif method == "tools/call":
        name, args = req["params"]["name"], req["params"]["arguments"]
        text = "x" * (args["kilobytes"] * 1024) if name == "big" else args["text"]
        send({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": text}], "isError": False}})
    elif rid is not None and ANSWERS:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown method {method}"}})
