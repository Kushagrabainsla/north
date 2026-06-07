"""Generic LLM-backed agent. The four v1 domain agents are thin subclasses."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from agents.base import Agent
from agents.exceptions import AgentConfigError, AgentOutputParseError
from agents.models import AgentPayload
from inference.models import (
    POOL_TO_PRIORITY,
    CompletionRequest,
    PoolPriority,
)
from tools.base import Tool


class LLMAgent(Agent):
    """An agent that calls the Inference Router with a markdown system prompt.

    Loads `prompts/system.md` from its own folder and uses it as the system
    message. The user message is the formatted task: prompt + context summary
    + tool list. The model is expected to return JSON matching `AgentResult`.

    Override `_build_messages()` or `_parse_response()` for domain-specific
    serialization. Override `_execute()` directly for radical custom logic.
    """

    def __init__(self, config: Any, deps: Any) -> None:
        super().__init__(config, deps)
        # Load the system prompt at construction time (sync, startup context) so
        # the first call to _execute() never blocks the running event loop.
        path = self._prompts_dir() / "system.md"
        if not path.exists():
            raise AgentConfigError(
                f"Missing system prompt at {path}. Every LLMAgent needs one."
            )
        self._system_prompt_cache: str = path.read_text(encoding="utf-8") + _TOOL_CREATION_POLICY

    def _prompts_dir(self) -> Path:
        """Resolve the agent's `prompts/` folder relative to its module file."""
        module = sys.modules[self.__class__.__module__]
        if module.__file__ is None:
            raise AgentConfigError(
                f"Cannot resolve prompts dir for {self.__class__.__name__}: "
                "module has no __file__"
            )
        return Path(module.__file__).parent / "prompts"

    def _load_system_prompt(self) -> str:
        return self._system_prompt_cache

    def _build_user_message(
        self,
        payload: AgentPayload,
        context: str,
        scored_tools: list[tuple[Tool, float]],
    ) -> str:
        from datetime import datetime, timezone
        tool_lines = "\n".join(
            f"- {t.name} (reliability {score:.0%}): {t.description}"
            for t, score in scored_tools
        )
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        system_lines = [f"- current date/time: {now}"]
        if payload.workspace:
            system_lines += [
                f"- workspace: {payload.workspace}",
                "- Before creating or interacting with directories (like Desktop, Downloads, Documents, etc.),"
                " always list the workspace contents (using `list_dir` or standard commands) to inspect"
                " the system, locate the actual target directories, and check if they are already"
                " present—exactly like a human engineer would. Never guess paths or run creation"
                " commands blindly.",
                "- When calling filesystem/shell tools, always use absolute paths derived from your"
                " workspace inspection above. Never use generic placeholders like '/home/user',"
                " unexpanded '~', or relative paths.",
            ]
        system_context = "## System Context\n" + "\n".join(system_lines) + "\n\n"
        return (
            f"{system_context}"
            f"## Task\n{payload.prompt}\n\n"
            f"## Context\n{context or '(none)'}\n\n"
            f"## Tools available\n{tool_lines or '(none)'}\n"
        )

    def _resolve_priority(self) -> PoolPriority:
        pool_name = self._config.model_pool
        if pool_name not in POOL_TO_PRIORITY:
            raise AgentConfigError(
                f"Unknown model_pool '{pool_name}' in {self.name} config. "
                f"Expected one of {sorted(POOL_TO_PRIORITY.keys())}."
            )
        return POOL_TO_PRIORITY[pool_name]

    async def _execute(
        self,
        payload: AgentPayload,
        context: str,
        scored_tools: list[tuple[Tool, float]],
    ) -> dict[str, Any]:
        system_prompt = self._load_system_prompt()
        user_message = self._build_user_message(payload, context, scored_tools)
        full_prompt = f"{system_prompt}\n\n---\n\n{user_message}"

        response = await self._deps.inference_router.complete(
            CompletionRequest(
                prompt=full_prompt,
                priority=self._resolve_priority(),
                component=self.name,
                task_id=payload.task_id,
            )
        )
        return self._parse_response(response.text)

    def _parse_response(self, text: str) -> dict[str, Any]:
        """Parse the model's JSON output. The system prompt instructs JSON.

        Strips a fenced code block if the model wraps the JSON in ```json ... ```.
        """
        cleaned = text.strip()
        if cleaned.startswith("```"):
            # Drop the opening fence (with optional `json` tag) and the closing fence.
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
            if cleaned.endswith("```"):
                cleaned = cleaned.rsplit("```", 1)[0]
            cleaned = cleaned.strip()

        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as e:
            raise AgentOutputParseError(
                f"{self.name} returned non-JSON output: {text[:200]}"
            ) from e

        if not isinstance(parsed, dict):
            raise AgentOutputParseError(
                f"{self.name} returned non-object JSON: {type(parsed).__name__}"
            )
        return parsed


_TOOL_CREATION_POLICY = """

## Tool creation policy

You have a `create_tool` tool that can extend the system with new capabilities.
Follow this strict priority order — only escalate when the step above cannot solve the problem:

1. **Use an existing tool.** Check your available tools first. If one fits, use it.
2. **Extend an existing tool.** Call `create_tool(action=list)` to see all tools. If a similar tool exists,
   call `create_tool(action=read, name=<tool>)` to inspect it, then `create_tool(action=update, ...)`
   to add the new capability while keeping all existing behaviour intact.
3. **Create a new tool.** Only if no existing tool is close enough. Call `create_tool(action=create, ...)`
   with a complete working implementation in the `content` parameter so the tool is immediately usable.

Never create or update a tool for something an existing tool already handles.
Never create a tool when `write_file` can do the job directly, or when composing existing available tools is sufficient.
After creating or updating a tool, you can use it immediately in the next step of this task."""
