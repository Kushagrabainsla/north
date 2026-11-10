"""Tool JSON schemas injected into every AgenticLLMAgent call."""

from __future__ import annotations

FIND_TOOLS_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "find_tools",
        "description": (
            "Search north's global tool catalog and load the most relevant tools into the next turn. "
            "Use this when the currently visible tools cannot perform the task; every agent may discover any tool."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Capability needed, or an exact tool name if known.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": "Maximum tools to load. Default: 5.",
                },
            },
            "required": ["query"],
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
            "(use request_approval for that). When several choices are open, ask them together "
            "in `questions`: the user answers them on one card, in one go. "
            "For a real-world handoff such as login, MFA, "
            "or browser takeover, set requires_user_action=true and use the same mode policy "
            "to obtain an answer. Send that handoff as its own tool call, then verify "
            "the resulting state before continuing the original task."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": (
                        "The specific question to ask. With `questions`, an optional one-line lead-in instead."
                    ),
                },
                "questions": {
                    "type": "array",
                    "maxItems": 8,
                    "description": (
                        "Several questions asked on one card and answered together, instead of one call "
                        "each. The result lists every answer."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string"},
                            "options": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Suggested answers, each complete as it stands: ask for a detail such as a "
                                    "path as its own question, not as an option that says 'tell me'. The user "
                                    "can always type their own."
                                ),
                            },
                        },
                        "required": ["question"],
                    },
                },
                "requires_user_action": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "True when the person must do something outside the chat (login, MFA, unlock, "
                        "or browser takeover). Uses the same approval policy and current mode as "
                        "every question. An answer does not prove completion; verify the actual state."
                    ),
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
        },
    },
}

REQUEST_APPROVAL_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "request_approval",
        "description": (
            "Ask the user once about one thing you are about to do that takes several tool calls (fill a form "
            "and submit it), before making any of them. Name in `calls` every call, exactly as you will make "
            "it: north asks once, or not at all if the user's approval mode allows them, and those exact calls "
            "then run without another card; a call that differs asks again. A single call needs no "
            "request_approval: make it, and north asks when the mode requires it. Never raise a card for an "
            "action no tool of yours will carry out: tell the user you cannot do it. With wait false it "
            "instead leaves finished work for the user to look at (a drafted application) and carries on: "
            "give `fields` and `item_keys` then, not `calls`."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "calls": {
                    "type": "array",
                    "description": "Each tool call this approval is for, in order, exactly as you will make it.",
                    "items": {
                        "type": "object",
                        "properties": {"tool": {"type": "string"}, "params": {"type": "object"}},
                        "required": ["tool", "params"],
                    },
                },
                "message": {
                    "type": "string",
                    "description": "Describe exactly what you plan to do and why.",
                },
                "title": {
                    "type": "string",
                    "description": "Short title naming the exact item and action under review.",
                },
                "fields": {
                    "type": "array",
                    "description": (
                        "With wait false: the finished work the user should look at. Each field has name, label, type "
                        "(text, textarea, number, boolean, select, or link), value, editable, and optional options."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "label": {"type": "string"},
                            "type": {
                                "type": "string",
                                "enum": ["text", "textarea", "number", "boolean", "select", "link"],
                            },
                            "value": {},
                            "editable": {"type": "boolean"},
                            "options": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["name", "value"],
                    },
                },
                "context": {
                    "type": "string",
                    "description": "With wait false: read-only source material the user needs to judge the fields.",
                },
                "item_keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "With wait false: what the item is, e.g. its address and 'company | role'. North offers "
                        "an item once; if any key was offered before, no card is made and you are told so."
                    ),
                },
                "wait": {
                    "type": "boolean",
                    "description": (
                        "Default true: stop until the user decides. false: the work is finished and only "
                        "needs the user's eye (a drafted application, a prepared reply), so leave it for them "
                        "and carry on at once; nothing here waits on the decision."
                    ),
                },
            },
            "required": ["message"],
        },
    },
}
