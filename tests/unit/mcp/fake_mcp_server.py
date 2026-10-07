#!/usr/bin/env python3
"""A stand-in MCP server over stdio for tests: one read-only tool, `search_gmail_messages`.

It answers from FAKE_MCP_MESSAGES (a JSON list of {"id", "body"}), so a test decides what the inbox holds.
Standard library only: it runs as its own process under the test's Python.
"""

import json
import os
import sys

TOOLS = [
    {
        "name": "search_gmail_messages",
        "description": "Search the inbox. Returns message ids.",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "get_gmail_message_content",
        "description": "Read one message by id.",
        "inputSchema": {
            "type": "object",
            "properties": {"message_id": {"type": "string"}},
            "required": ["message_id"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
]


def _answer(request: dict) -> dict:
    method = request.get("method")
    if method == "initialize":
        return {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        messages = json.loads(os.environ.get("FAKE_MCP_MESSAGES", "[]"))
        params = request.get("params", {})
        if params.get("name") == "get_gmail_message_content":
            wanted = params.get("arguments", {}).get("message_id")
            text = next((m["body"] for m in messages if m["id"] == wanted), f"No message {wanted}.")
        else:
            text = "\n\n".join(f"Message ID: {m['id']}\n{m['body']}" for m in messages) or "No messages."
        return {"content": [{"type": "text", "text": text}], "isError": False}
    return {}


for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue  # a notification
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": _answer(request)}) + "\n")
    sys.stdout.flush()
