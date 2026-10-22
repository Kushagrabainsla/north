"""How every provider sends a conversation, so the start a provider has cached never changes."""

from __future__ import annotations

from typing import Any

_SYSTEM_ROLES = frozenset({"system", "developer"})


def message_text(content: Any) -> str:
    """Flatten OpenAI-shaped message content (string or a list of parts) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict) and part.get("type") == "text"
        )
    return "" if content is None else str(content)


def system_notes_in_place(messages: list[dict]) -> list[dict]:
    """*messages*, with a system message that arrives mid-conversation sent where it arrived, as a user note.

    Only the opening system messages are the system prompt. Providers cache a
    prompt by its unchanged start, and the Codex provider merged a later system
    message (the agent loop's soft-budget nudge) into its instructions, so the
    start changed and the whole history after it went uncached: the next call
    reused 2,816 of about 7,200 tokens. Sent in place, the note only appends, and
    the call reused 5,888 and 6,912. A user note is accepted mid-conversation by
    every provider; a system or developer role is not.
    """
    out: list[dict] = []
    conversation_started = False
    for message in messages:
        is_system = message.get("role") in _SYSTEM_ROLES
        if is_system and conversation_started:
            out.append({"role": "user", "content": f"[System note] {message_text(message.get('content'))}"})
            continue
        conversation_started = conversation_started or not is_system
        out.append(message)
    return out
