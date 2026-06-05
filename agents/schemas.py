"""Tool JSON schemas injected into every AgenticLLMAgent call."""
from __future__ import annotations

DELEGATE_TASK_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "delegate_task",
        "description": (
            "Delegate a sub-task to a specialist agent. "
            "Use when a sub-problem clearly belongs to a different domain specialist "
            "(e.g. code, finance, health). The specialist runs its own ReAct loop and "
            "returns a result. Only use when the sub-task genuinely requires domain expertise "
            "you don't have — don't delegate work you can do yourself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": (
                        "Name of the specialist agent "
                        "(e.g. 'researcher', 'architect', 'coder', 'tester', "
                        "'finance', 'health', 'university', 'job', 'home', 'general')."
                    ),
                },
                "task": {
                    "type": "string",
                    "description": "The full sub-task prompt for the specialist. Be specific.",
                },
            },
            "required": ["agent", "task"],
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
