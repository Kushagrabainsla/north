"""Live view of one task's SSE stream: events in, rendered steps and answer out.

The CLI submits a task and then watches `/orchestrator/stream/{task_id}`. Every
event either adds a step to the live table, extends the streamed answer, or ends
the stream. `TaskStepFeed` owns that fold so `cli.main` only submits the task and
prints what comes back.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from cli._client import _api, _headers
from cli.constants import _BASE_URL, _STEP_ICONS, _STEP_LABELS
from cli.formatting import _build_steps_table

_AFFIRMATIVE = frozenset({"approve", "approved", "yes", "y", "apply", "run", "ok"})
_NEGATIVE = frozenset({"reject", "rejected", "no", "n", "cancel", "deny"})

_FINAL_EVENTS = frozenset({"task_completed", "task_failed", "task_cancelled"})

_MAX_PARAM_PREVIEW_CHARS = 28
_MAX_RESULT_PREVIEW_CHARS = 35
_RUN_ID_CHARS = 8

# One rendered line of the live table: its icon, its text, and whether it is the
# step currently running (which is what the table renders as active).
_Step = tuple[str, str, bool]


@dataclass(frozen=True)
class TaskStream:
    """Where one task's events come from and how the user answers them."""

    task_id: str
    console: Console
    yolo: bool = False

    @property
    def url(self) -> str:
        return f"{_BASE_URL}/orchestrator/stream/{self.task_id}"


def _approval_decision(chosen: str, *, yolo: bool) -> str:
    """Map the user's pick on an approval card to a decision the server accepts.

    This is only ever reached for ``approval_required``; questions arrive under
    their own event. So under --yolo the first option is a yes whatever it is
    called, and anything unrecognised stays ``answered`` rather than being
    guessed into consent.
    """
    if yolo:
        return "approved"
    picked = chosen.strip().lower()
    if picked in _AFFIRMATIVE:
        return "approved"
    if picked in _NEGATIVE:
        return "rejected"
    return "answered"


def iter_sse_events(lines: Iterable[str]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield (event name, payload) for each complete SSE frame, skipping bad JSON."""
    current_event = ""
    for line in lines:
        if line.startswith("event:"):
            current_event = line[6:].strip()
        elif line.startswith("data:"):
            try:
                data = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            yield current_event or data.get("event", ""), data
            current_event = ""


def _truncated(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "…"


def _params_preview(params: Any) -> str:
    if not isinstance(params, dict) or not params:
        return ""
    shown = {key: value for key, value in params.items() if not key.startswith("_")}
    if not shown:
        return ""
    pairs = [
        f"{key}={_truncated(value if isinstance(value, str) else json.dumps(value), _MAX_PARAM_PREVIEW_CHARS)}"
        for key, value in shown.items()
    ]
    return f" [dim]({', '.join(pairs)})[/dim]"


def _result_preview(data: dict[str, Any]) -> str:
    formatted = data.get("formatted") or data.get("summary") or data.get("error") or ""
    if not formatted:
        return ""
    return f" [dim]→ {_truncated(str(formatted).strip().replace('\n', ' '), _MAX_RESULT_PREVIEW_CHARS)}[/dim]"


def _run_id_suffix(data: dict[str, Any]) -> str:
    run_id = str(data.get("run_id") or "")[:_RUN_ID_CHARS]
    return f" [dim]#{run_id}[/dim]" if run_id else ""


def _delegation_branch(data: dict[str, Any], connector: str) -> str:
    return f"  {connector} " if data.get("parent_run_id") else ""


@dataclass
class TaskStepFeed:
    """Folds a task's events into the live step table and the streamed answer."""

    stream: TaskStream
    live: Live
    steps: list[_Step] = field(default_factory=list)
    failure: str = ""
    _tokens: str = ""
    # The auditor can run the agent a second time to correct unverified claims,
    # and that draft streams down the same channel as the first. Without this,
    # both answers were concatenated - which is how one reply ended
    # "...format_status_table()I could not complete the verification:".
    _repairing: bool = False
    _repaired: bool = False

    @property
    def answer(self) -> str:
        """The streamed answer, or "" when nothing usable was streamed.

        A repair that started but was never adopted leaves a rejected draft in
        the buffer; the ledger holds the answer that was actually kept.
        """
        if self._repairing and not self._repaired:
            return ""
        return self._tokens

    def apply(self, event: str, data: dict[str, Any]) -> bool:
        """Fold one event in. Returns False once the task has reached a final state."""
        self._settle_last_step()
        if event == "token":
            self._tokens += data.get("text", "")
            return True

        handler = _EVENT_HANDLERS.get(event)
        if handler is not None:
            handler(self, data)
        elif event in _STEP_LABELS:
            self.steps.append((_STEP_ICONS.get(event, "·"), _STEP_LABELS[event], True))

        if event == "task_failed":
            self.failure = data.get("error", "Task failed.")
            self._mark_last_step_failed()
        self.refresh()
        if event == "task_cancelled":
            self.stream.console.print("[dim]Task cancelled.[/dim]")
        return event not in _FINAL_EVENTS

    def renderable(self) -> Panel:
        return Panel(
            _build_steps_table(self.steps) if self.steps else Text("starting…", style="dim"),
            border_style="bright_black",
            padding=(0, 1),
        )

    def refresh(self) -> None:
        self.live.update(self.renderable())

    def _settle_last_step(self) -> None:
        if self.steps:
            icon, label, _ = self.steps[-1]
            self.steps[-1] = (icon, label, False)

    def _mark_last_step_failed(self) -> None:
        if self.steps:
            _, label, _ = self.steps[-1]
            self.steps[-1] = ("✗", label, False)

    def _on_agent_started(self, data: dict[str, Any]) -> None:
        agent = data.get("agent", "agent")
        label = f"{_delegation_branch(data, '├─')}{agent} agent running{_run_id_suffix(data)}…"
        self.steps.append(("◎", label, True))

    def _on_model(self, data: dict[str, Any]) -> None:
        """Name the model on the running agent's step rather than adding a step."""
        model = data.get("model", "")
        if not model:
            return
        for index in reversed(range(len(self.steps))):
            icon, label, active = self.steps[index]
            if icon == "◎" and active:
                agent_name = label.split()[0]
                self.steps[index] = ("◎", f"{agent_name} running on [cyan]{model}[/cyan]…", True)
                return

    def _on_failover(self, data: dict[str, Any]) -> None:
        from_model = data.get("from", "model")
        to_model = data.get("to", "")
        reason = data.get("reason", "")
        destination = f" → [cyan]{to_model}[/cyan]" if to_model else ""
        why = f" ({reason})" if reason else ""
        self.steps.append(("↻", f"failover: [dim]{from_model}[/dim]{destination}{why}", False))

    def _on_agent_completed(self, data: dict[str, Any]) -> None:
        agent = data.get("agent", "agent")
        summary = data.get("summary", "")
        duration = data.get("duration_ms")
        elapsed = f" [dim]({duration}ms)[/dim]" if duration else ""
        branch = _delegation_branch(data, "└─")
        suffix = f"{elapsed}{_run_id_suffix(data)}"
        label = f"{branch}{agent}: {summary}{suffix}" if summary else f"{branch}{agent} agent done{suffix}"
        self.steps.append(("✓", label, True))

    def _on_tool_called(self, data: dict[str, Any]) -> None:
        # Whatever streamed before a tool call was narration on the way to it
        # ("I'll check the repo for..."), not the answer - keeping it glued the
        # two together with no separator. The answer is what streams after the
        # last tool call.
        self._tokens = ""
        tool = data.get("tool", "tool")
        params = data.get("params") or data.get("args") or {}
        self.steps.append(("→", f"  {tool}{_params_preview(params)}…", True))

    def _on_tool_result(self, data: dict[str, Any]) -> None:
        tool = data.get("tool", "tool")
        icon = "✓" if data.get("success", True) else "✗"
        self.steps.append((icon, f"  {tool}{_result_preview(data)}", True))

    def _on_classified(self, data: dict[str, Any]) -> None:
        domain = data.get("domain", "")
        shape = "complex" if data.get("is_consequential", False) else "direct"
        label = f"classified: [cyan]{domain}[/cyan] [dim]({shape})[/dim]" if domain else "classified"
        self.steps.append(("✓", label, True))

    def _on_routed(self, data: dict[str, Any]) -> None:
        agents = ", ".join(data.get("agents", []))
        self.steps.append(("✓", f"plan ready: [cyan]{agents}[/cyan]" if agents else "plan ready", True))

    def _on_self_repair_started(self, data: dict[str, Any]) -> None:
        # The answer streamed so far is about to be replaced.
        self._tokens = ""
        self._repairing, self._repaired = True, False

    def _on_self_repair_done(self, data: dict[str, Any]) -> None:
        self._repaired = True

    def _on_approval_required(self, data: dict[str, Any]) -> None:
        """Stop the live table, ask the user, and send their decision back."""
        self.steps.append(("?", "Approval required", False))
        self.refresh()
        self.live.stop()

        chosen = self._ask_for_approval(data)
        decision = _approval_decision(chosen, yolo=self.stream.yolo)
        self._send_approval(data, decision=decision, chosen=chosen)
        self.steps[-1] = ("✓" if decision != "rejected" else "✗", f"Approval: {chosen}", False)
        # Do NOT restart Live - the cursor is now past the approval panel. Later
        # refresh() calls on a stopped Live are no-ops; a final event ends the loop.

    def _ask_for_approval(self, data: dict[str, Any]) -> str:
        console = self.stream.console
        console.print()
        console.print(
            Panel(
                Text(data.get("message", ""), style="white"),
                title="[yellow]approval required[/yellow]",
                border_style="yellow",
                padding=(1, 2),
            )
        )
        options = data.get("options", ["Approve", "Reject"])
        for number, option in enumerate(options, 1):
            console.print(f"  [bright_black][{number}][/bright_black]  {option}")
        console.print()

        if self.stream.yolo:
            console.print("  [yellow]⚠ YOLO[/yellow]  auto-approved")
            raw_choice = "1"
        else:
            raw_choice = input("  ❯ ").strip()
        return _chosen_option(raw_choice, options)

    def _send_approval(self, data: dict[str, Any], *, decision: str, chosen: str) -> None:
        with contextlib.suppress(SystemExit):
            _api(
                "POST",
                "/orchestrator/approval/respond",
                json={
                    "card_id": data.get("card_id", ""),
                    "task_id": self.stream.task_id,
                    "agent": data.get("agent", ""),
                    "decision": decision,
                    "chosen_option": chosen,
                },
            )


def _chosen_option(raw_choice: str, options: list[str]) -> str:
    """The option the user meant: by number when they typed one, else verbatim."""
    try:
        index = int(raw_choice) - 1
    except ValueError:
        return raw_choice or options[0]
    return options[index] if 0 <= index < len(options) else raw_choice


# Events that shape the live view, each folded in by one small handler. Events
# without an entry fall back to the fixed labels in _STEP_LABELS.
_EVENT_HANDLERS: dict[str, Callable[[TaskStepFeed, dict[str, Any]], None]] = {
    "agent_started": TaskStepFeed._on_agent_started,
    "model": TaskStepFeed._on_model,
    "failover": TaskStepFeed._on_failover,
    "agent_completed": TaskStepFeed._on_agent_completed,
    "tool_called": TaskStepFeed._on_tool_called,
    "tool_result": TaskStepFeed._on_tool_result,
    "classified": TaskStepFeed._on_classified,
    "routed": TaskStepFeed._on_routed,
    "approval_required": TaskStepFeed._on_approval_required,
    "self_repair_started": TaskStepFeed._on_self_repair_started,
    "self_repair_done": TaskStepFeed._on_self_repair_done,
}


def follow_task(stream: TaskStream) -> TaskStepFeed:
    """Watch a task to completion, rendering its steps live. Raises KeyboardInterrupt."""
    with (
        Live(Text("starting…", style="dim"), console=stream.console, refresh_per_second=8) as live,
        httpx.stream("GET", stream.url, headers=_headers(), timeout=None) as response,
    ):
        feed = TaskStepFeed(stream=stream, live=live)
        feed.refresh()
        for event, data in iter_sse_events(response.iter_lines()):
            if not feed.apply(event, data):
                break
    return feed
