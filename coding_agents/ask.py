"""Ask north: the one door a coding agent has back to north.

A coding agent that needs a decision, a preference, or a fact about the user that the repository does not
hold calls the `ask_north` tool instead of guessing. The tool is served by north over MCP on loopback, to one
run at a time (the same per-run token as the gate). What happens to the question is north's business and not
this module's: the approval layer shows it to the user, or in autonomous mode answers it from memory
(`Asker`). Nothing else of north is reachable through this door.

This module is the protocol and nothing more, so it is pure: one MCP message in, one response out.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from coding_agents.gate import GateSession

# Claude Code names an MCP tool `mcp__<server>__<tool>`; Codex shows the server and the tool apart.
SERVER = "north"
TOOL = "ask_north"
CLAUDE_TOOL = f"mcp__{SERVER}__{TOOL}"

MAX_QUESTION_CHARS = 2_000
MAX_OPTIONS = 8
MAX_OPTION_CHARS = 200
SUPPORTED_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26")

TOOL_DESCRIPTION = (
    "Ask north, the user's assistant, one specific question you cannot settle from the repository: a decision "
    "between options, a preference or convention of the user's, or a fact about the user or their setup. "
    "The answer comes from the user, or from north answering for them from what it knows. Prefer asking to "
    "guessing, but ask one question at a time, include what you already know, and never ask for passwords, "
    "keys or tokens. Treat the answer as the user's own, and say in your final report what you asked and what "
    "you were told."
)
INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": "The one question, in full, with the context needed to answer it.",
        },
        "options": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The choices, when the answer is one of a few. Leave out for an open question.",
        },
    },
    "required": ["question"],
}


# Asking changes nothing in the repository or on the machine, which is what a read-only hint means. A vendor's plan
# mode lets only read-only tools through, and a run that cannot ask north in plan mode cannot ask at all.
_TOOL: dict[str, Any] = {
    "name": TOOL,
    "title": "Ask north",
    "description": TOOL_DESCRIPTION,
    "inputSchema": INPUT_SCHEMA,
    "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
}


@dataclass(frozen=True)
class Question:
    text: str
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class Reply:
    """What goes back to the agent. *by* says who answered, so the agent can weigh it."""

    text: str
    by: str = ""
    reason: str = ""
    answered: bool = True


class Asker(Protocol):
    """Gets a question answered: by the user, or by north for the user. The approval layer implements it."""

    async def ask(self, session: GateSession, question: Question) -> Reply: ...


def render(reply: Reply) -> str:
    """The tool result the agent reads."""
    if not reply.answered:
        return reply.text
    who = f"{reply.by} answered" if reply.by else "Answer"
    detail = f" (why: {reply.reason})" if reply.reason else ""
    return f"{who}: {reply.text}{detail}"


async def handle(message: Any, session: GateSession, asker: Asker) -> dict[str, Any] | list[dict[str, Any]] | None:
    """One JSON-RPC message (or a batch) in, the response out; None when there is nothing to send back."""
    if isinstance(message, list):
        replies = [r for r in [await handle(item, session, asker) for item in message] if isinstance(r, dict)]
        return replies or None
    if not isinstance(message, Mapping):
        return _error(None, -32600, "not a JSON-RPC message")
    method, ident = message.get("method"), message.get("id")
    if ident is None:  # a notification: nothing to answer
        return None
    raw = message.get("params")
    params: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    if method == "initialize":
        asked = str(params.get("protocolVersion") or "")
        version = asked if asked in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
        return _result(
            ident,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER, "version": "1"},
            },
        )
    if method == "ping":
        return _result(ident, {})
    if method == "tools/list":
        return _result(ident, {"tools": [_TOOL]})
    if method == "tools/call":
        return await _call(ident, params, session, asker)
    return _error(ident, -32601, f"north does not handle {method!r}")


async def _call(ident: Any, params: Mapping[str, Any], session: GateSession, asker: Asker) -> dict[str, Any]:
    if params.get("name") != TOOL:
        return _error(ident, -32602, f"there is no tool {params.get('name')!r}")
    question = _question(params.get("arguments"))
    if isinstance(question, str):
        return _result(ident, _content(question, error=True))
    reply = await asker.ask(session, question)
    return _result(ident, _content(render(reply), error=False))


def _question(arguments: Any) -> Question | str:
    """The question in a tool call's arguments, or what is wrong with them."""
    if not isinstance(arguments, Mapping):
        return "ask_north needs a question."
    text = str(arguments.get("question") or "").strip()
    if not text:
        return "ask_north needs a non-empty question."
    if len(text) > MAX_QUESTION_CHARS:
        return f"The question is too long ({len(text)} characters; the limit is {MAX_QUESTION_CHARS}). Make it shorter."
    raw = arguments.get("options") or []
    if not isinstance(raw, list):
        return "options must be a list of strings."
    options = tuple(str(o).strip()[:MAX_OPTION_CHARS] for o in raw if str(o).strip())[:MAX_OPTIONS]
    return Question(text, options)


def _content(text: str, *, error: bool) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": error}


def _result(ident: Any, result: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": ident, "result": dict(result)}


def _error(ident: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}
