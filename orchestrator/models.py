"""Models and data structures for the Orchestrator.

See docs/CODING_STYLE.md Section 9.7.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from ledger.models import LedgerSource
from utils.edit_scope import EditAuthorizer


def _not_blank(prompt: str) -> str:
    if not prompt.strip():
        raise ValueError("a prompt needs words, not only spaces")
    return prompt


# What a task's prompt may be, for every way one arrives. A prompt of only spaces
# passed the length check, and north ran (39k input tokens) to
# say "The task came through empty" (issue #76, M21).
Prompt = Annotated[str, Field(min_length=1, max_length=32_768), AfterValidator(_not_blank)]


class TaskRequest(BaseModel):
    """Input payload to trigger a new task execution."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    prompt: Prompt
    source: LedgerSource = LedgerSource.PROMPT
    workspace: str = ""  # optional root directory for filesystem/shell tools
    context: str = ""  # optional pre-loaded context summary to inject into the agent's prompt
    # Optional client-supplied key to dedupe re-deliveries (e.g. a webhook id).
    # When omitted, the orchestrator derives one from source+prompt.
    idempotency_key: str | None = None
    # Server-owned edit scope for this task. When set, the orchestrator threads it
    # onto every AgentPayload it creates, and mutating file tools enforce it before
    # writing. None (the default) leaves edits unrestricted, preserving the behavior
    # of all current public callers. This is never populated from model output.
    edit_scope: EditAuthorizer | None = Field(default=None, exclude=True)


class TaskResponse(BaseModel):
    """Response returned upon successfully registering a task."""

    task_id: str
    status: str
    created_at: str
