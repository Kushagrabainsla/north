"""Tool JSON schemas injected into every AgenticLLMAgent call."""

from __future__ import annotations

from collections.abc import Sequence


def delegate_task_schema(agent_names: Sequence[str] = ()) -> dict:
    """The `delegate_task` definition, naming only agents that exist.

    The names come from the live `AgentRegistry`, never from a list written
    here. A hardcoded list drifted: this schema advertised `finance`, `health`,
    `university` and `job`, none of which had an agent, so a model that did what
    the schema told it raised `AgentNotFoundError` - a failure north caused with
    its own instructions, and one that reads like a model mistake.

    With no names given the schema states the constraint without examples, which
    is the honest thing to send when the caller cannot say what exists.
    """
    if agent_names:
        listed = ", ".join(f"'{name}'" for name in agent_names)
        agent_description = f"Name of the specialist agent. Must be one of: {listed}."
    else:
        agent_description = "Name of the specialist agent. Only agents registered in this install are valid."
    return {
        "type": "function",
        "function": {
            "name": "delegate_task",
            "description": (
                "Delegate a sub-task to a specialist agent. "
                "Use when a sub-problem clearly belongs to a different domain specialist. "
                "The specialist runs its own ReAct loop and "
                "returns a result. Only use when the sub-task genuinely requires domain expertise "
                "you don't have - don't delegate work you can do yourself. "
                "Context is automatically carried forward; you only need to pass "
                "task description and optional metadata."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent": {
                        "type": "string",
                        "description": agent_description,
                    },
                    "task": {
                        "type": "string",
                        "description": "The full sub-task prompt for the specialist. Be specific.",
                    },
                    "context": {
                        "type": "object",
                        "description": (
                            "Optional metadata to pass to the specialist. "
                            "Include failed_attempts, known_failures, relevant_files, etc. "
                            "Helps specialist avoid redundant work."
                        ),
                        "additionalProperties": True,
                    },
                },
                "required": ["agent", "task"],
            },
        },
    }


ASK_USER_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "ask_user",
        "description": (
            "Ask the user a clarifying question and wait for their answer. "
            "Use this whenever a requirement, preference, or detail you need is not "
            "stated in the task or context - NEVER assume or invent it. The user's "
            "typed answer is returned as the tool result so you can continue with it. "
            "This is for gathering information, not for approving an action "
            "(use request_approval for that)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The specific question to ask. One clear question at a time.",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional suggested answers shown as choices. Omit for an "
                        "open-ended question - the user can always type a free-form answer."
                    ),
                },
            },
            "required": ["question"],
        },
    },
}

REQUEST_APPROVAL_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "request_approval",
        "description": (
            "Request explicit user approval before taking an irreversible action "
            "(send email, submit form, delete data, etc.)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Describe exactly what you plan to do and why.",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Choices shown to the user (default ['Approve','Reject']).",
                },
            },
            "required": ["message"],
        },
    },
}
