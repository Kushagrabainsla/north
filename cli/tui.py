"""North TUI - Textual-based chat UI.

A Textual App that owns the full render cycle. This is the same approach used
by Bubbletea-based tools like gh copilot: the framework explicitly re-positions
the cursor inside the Input widget after every frame, so live streaming output
and the input box coexist without cursor conflicts.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import httpx
from rich.console import Group
from rich.markdown import Markdown as RichMarkdown
from rich.padding import Padding as RichPadding
from rich.syntax import Syntax as RichSyntax
from rich.text import Text as RichText
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.suggester import Suggester
from textual.widgets import Button, Input, Label, ListItem, ListView, RichLog, Static

from cli.constants import (
    _REASONING_PREVIEW_CHARS,
    _SLASH_COMMANDS,
    _SSE_BACKOFF_BASE,
    _SSE_BACKOFF_MAX,
)
from cli.formatting import (
    _compute_suggestion,
    _copy_to_clipboard,
    _fill_bar,
    _fmt_elapsed,
    _fmt_params,
    _fmt_tokens,
    _format_help_table,
    _format_jobs_table,
    _format_plan_table,
    _format_turn_details,
    _format_turn_summary,
    _reconstruct_task_output,
    _short_model,
    _strip_markup,
    summarize_diff,
)


class ToolInspectorModal(ModalScreen[None]):
    """Interactive modal to inspect recent tool calls, parameters, stdout, and diffs."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close", priority=True),
        Binding("q", "dismiss", "Close", show=False),
    ]

    CSS = """
    ToolInspectorModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.7);
    }

    #tool-modal-box {
        width: 90%;
        height: 85%;
        background: #0d1117;
        border: thick #58a6ff;
        layout: vertical;
        padding: 1 2;
    }

    #tool-modal-title {
        width: 100%;
        height: 2;
        color: #58a6ff;
        text-style: bold;
        border-bottom: solid #30363d;
    }

    #tool-modal-body {
        width: 100%;
        height: 1fr;
        layout: horizontal;
    }

    #tool-modal-list {
        width: 32%;
        height: 100%;
        border-right: solid #30363d;
        scrollbar-size: 1 1;
    }

    #tool-modal-detail {
        width: 68%;
        height: 100%;
        padding: 0 1;
        scrollbar-size: 1 1;
    }

    #tool-modal-footer {
        width: 100%;
        height: 2;
        color: #8b949e;
        border-top: solid #30363d;
    }
    """

    def __init__(self, tool_history: list[dict]) -> None:
        super().__init__()
        self._history = tool_history

    def compose(self) -> ComposeResult:
        with Vertical(id="tool-modal-box"):
            yield Static(
                "  [bold #58a6ff]🔍 Tool & Diff Inspector[/bold #58a6ff] [bright_black](Esc/q to close)[/bright_black]",
                id="tool-modal-title",
            )
            with Horizontal(id="tool-modal-body"):
                yield ListView(id="tool-modal-list")
                with VerticalScroll(id="tool-modal-detail"):
                    yield Static(
                        "Select a tool call from the left to inspect its parameters, output, and diffs.",
                        id="tool-detail-content",
                    )
            yield Static("  [dim]↑/↓ Navigate list · Enter to select · Esc to close[/dim]", id="tool-modal-footer")

    def on_mount(self) -> None:
        list_view = self.query_one("#tool-modal-list", ListView)
        if not self._history:
            list_view.append(ListItem(Label("  No tool calls recorded yet.")))
            return
        for i, item in enumerate(reversed(self._history)):
            tool = item.get("tool", "unknown")
            success = item.get("success", True)
            icon = "[green]✓[/green]" if success else "[red]✗[/red]"
            dur = item.get("duration")
            dur_str = f" ({dur:.2f}s)" if dur is not None else ""
            label_text = f" {icon} [bold white]{tool}[/bold white][bright_black]{dur_str}[/bright_black]"
            list_view.append(ListItem(Label(label_text), name=str(len(self._history) - 1 - i)))
        if self._history:
            self._render_tool_detail(self._history[-1])

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.item and event.item.name:
            try:
                idx = int(event.item.name)
                if 0 <= idx < len(self._history):
                    self._render_tool_detail(self._history[idx])
            except ValueError:
                pass

    def _render_tool_detail(self, item: dict) -> None:
        detail = self.query_one("#tool-detail-content", Static)
        tool = item.get("tool", "unknown")
        success = item.get("success", True)
        params = item.get("params", {})
        result = item.get("result") or item.get("formatted") or item.get("output") or ""
        error = item.get("error") or ""
        dur = item.get("duration")
        dur_str = f"{dur:.3f}s" if dur is not None else "n/a"

        header_color = "#3fb950" if success else "#f85149"
        status_text = "SUCCESS" if success else "FAILED"

        lines = [
            f"[bold {header_color}]▶ Tool: {tool}[/bold {header_color}]  "
            f"[bright_black]({status_text} · {dur_str})[/bright_black]",
            "",
            "[bold white]Parameters:[/bold white]",
        ]
        formatted_params = json.dumps(params, indent=2, ensure_ascii=False) if params else "{}"
        lines.append(f"[bright_black]{formatted_params}[/bright_black]")
        lines.append("")

        old_string = params.get("old_string")
        new_string = params.get("new_string")
        if old_string is not None and new_string is not None:
            lines.append("[bold cyan]Planned Replacement / Diff:[/bold cyan]")
            lines.append(f"[red]- {old_string}[/red]")
            lines.append(f"[green]+ {new_string}[/green]")
            lines.append("")
        elif new_string and "<<<<<<< SEARCH" in new_string:
            lines.append("[bold cyan]SEARCH / REPLACE Blocks:[/bold cyan]")
            lines.append(f"[bright_black]{new_string}[/bright_black]")
            lines.append("")

        if error:
            lines.append("[bold red]Error / Traceback:[/bold red]")
            lines.append(f"[red]{error}[/red]")
            lines.append("")

        if result:
            lines.append("[bold white]Output / Result:[/bold white]")
            lines.append(f"[bright_black]{result}[/bright_black]")

        detail.update("\n".join(lines))


class PlanCockpitModal(ModalScreen[None]):
    """Interactive modal to inspect the task execution plan, subtasks, and DoD criteria."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close", priority=True),
        Binding("q", "dismiss", "Close", show=False),
    ]

    CSS = """
    PlanCockpitModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.7);
    }

    #plan-modal-box {
        width: 85%;
        height: 80%;
        background: #0d1117;
        border: thick #3fb950;
        layout: vertical;
        padding: 1 2;
    }

    #plan-modal-title {
        width: 100%;
        height: 2;
        color: #3fb950;
        text-style: bold;
        border-bottom: solid #30363d;
    }

    #plan-modal-body {
        width: 100%;
        height: 1fr;
        scrollbar-size: 1 1;
        padding: 1 0;
    }

    #plan-modal-footer {
        width: 100%;
        height: 2;
        color: #8b949e;
        border-top: solid #30363d;
    }
    """

    def __init__(self, plan_steps: list[dict], dod_results: list[dict], active_phase: str = "") -> None:
        super().__init__()
        self._plan_steps = plan_steps
        self._dod_results = dod_results
        self._active_phase = active_phase

    def compose(self) -> ComposeResult:
        with Vertical(id="plan-modal-box"):
            yield Static(
                "  [bold #3fb950]📋 Execution Plan & DoD Cockpit[/bold #3fb950] "
                "[bright_black](Esc/q to close)[/bright_black]",
                id="plan-modal-title",
            )
            with VerticalScroll(id="plan-modal-body"):
                yield Static(id="plan-modal-content")
            yield Static("  [dim]Press Esc or q to return to chat[/dim]", id="plan-modal-footer")

    def on_mount(self) -> None:
        content = self.query_one("#plan-modal-content", Static)
        table = _format_plan_table(self._plan_steps, self._dod_results)
        content.update(table)


class ActionMenuModal(ModalScreen[str | None]):
    """Modal menu to steer, inspect, or control an active task."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close", priority=True),
        Binding("q", "dismiss", "Close", show=False),
        Binding("1", "select_1", "Steer", show=False),
        Binding("2", "select_2", "Tools", show=False),
        Binding("3", "select_3", "Thoughts", show=False),
        Binding("4", "select_4", "Plan", show=False),
        Binding("5", "select_5", "Cancel", show=False),
    ]

    CSS = """
    ActionMenuModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.7);
    }

    #action-menu-box {
        width: 60;
        height: auto;
        background: #0d1117;
        border: thick #d29922;
        layout: vertical;
        padding: 1 2;
    }

    #action-menu-title {
        width: 100%;
        height: 2;
        color: #d29922;
        text-style: bold;
        border-bottom: solid #30363d;
    }

    #action-menu-options {
        width: 100%;
        height: auto;
        padding: 1 0;
    }

    #action-menu-footer {
        width: 100%;
        height: 1;
        color: #8b949e;
        border-top: solid #30363d;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="action-menu-box"):
            yield Static("  [bold #d29922]Task actions[/bold #d29922]", id="action-menu-title")
            with Vertical(id="action-menu-options"):
                yield Button(" [1] Steer agent (/steer)", id="btn-steer", variant="primary")
                yield Button(" [2] Inspect tools and diffs (Ctrl+I)", id="btn-tools")
                yield Button(" [3] View thoughts for this message (Ctrl+T)", id="btn-thoughts")
                yield Button(" [4] View plan and checks (Ctrl+P)", id="btn-plan")
                yield Button(" [5] Cancel task (/cancel)", id="btn-cancel", variant="error")
            yield Static("  [dim]Press [1-5] or click · Esc to return[/dim]", id="action-menu-footer")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-steer":
            self.dismiss("steer")
        elif bid == "btn-tools":
            self.dismiss("inspect_tools")
        elif bid == "btn-thoughts":
            self.dismiss("view_thoughts")
        elif bid == "btn-plan":
            self.dismiss("view_plan")
        elif bid == "btn-cancel":
            self.dismiss("cancel")

    def action_select_1(self) -> None:
        self.dismiss("steer")

    def action_select_2(self) -> None:
        self.dismiss("inspect_tools")

    def action_select_3(self) -> None:
        self.dismiss("view_thoughts")

    def action_select_4(self) -> None:
        self.dismiss("view_plan")

    def action_select_5(self) -> None:
        self.dismiss("cancel")


class SteerModal(ModalScreen[str | None]):
    """Modal prompt to input an in-flight steering directive."""

    BINDINGS = [
        Binding("escape", "dismiss", "Cancel", priority=True),
    ]

    CSS = """
    SteerModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.7);
    }

    #steer-box {
        width: 70;
        height: auto;
        background: #0d1117;
        border: thick #a371f7;
        layout: vertical;
        padding: 1 2;
    }

    #steer-title {
        width: 100%;
        height: 2;
        color: #a371f7;
        text-style: bold;
        border-bottom: solid #30363d;
    }

    #steer-input {
        width: 100%;
        margin: 1 0;
        border: round #a371f7;
    }

    #steer-footer {
        width: 100%;
        height: 1;
        color: #8b949e;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="steer-box"):
            yield Static("  [bold #a371f7]🎯 In-Flight Steering Guidance[/bold #a371f7]", id="steer-title")
            yield Input(placeholder="e.g. use asyncpg instead of psycopg2, or focus on tests...", id="steer-input")
            yield Static("  [dim]Enter to submit directive · Esc to cancel[/dim]", id="steer-footer")

    def on_mount(self) -> None:
        self.query_one("#steer-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        val = event.value.strip()
        self.dismiss(val if val else None)


class _NorthSuggester(Suggester):
    """Drives the Input's dim ghost-text using slash commands + input history."""

    def __init__(self, history_getter) -> None:
        super().__init__(use_cache=False, case_sensitive=True)
        self._history_getter = history_getter

    async def get_suggestion(self, value: str) -> str | None:
        return _compute_suggestion(value, self._history_getter())


def _read_power(settings_path: Path) -> str:
    """Read the current power dial from the north settings file.

    Falls back to 'cruise' if the file is absent or unreadable so the info bar
    always shows something meaningful without crashing the TUI. Reads the new
    'power' key, falling back to the legacy 'strategy' key for older files.
    """
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        return str(data.get("power", data.get("strategy", "cruise")))
    except Exception:
        return "cruise"


class NorthApp(App[None]):
    """Textual chat UI for north."""

    # Layout (top → bottom):
    #   #log          - completed turns and system notices       (1fr)
    #   #active-turns - mutable, task-scoped live conversation   (≤65%)
    #   #statusbar    - global model/context/cost information     (1 row)
    #   #input-row    - ╭ >  [                       ] ╮ box       (3 rows)
    #   #hint         - dim shortcuts                              (1 row)

    CSS = """
    Screen {
        layout: vertical;
        background: #090d13;
    }

    /* ── top cockpit header bar ───────────────────────────── */

    #header-bar {
        width: 100%;
        height: 1;
        background: #161b22;
        color: #8b949e;
        border-bottom: solid #30363d;
    }

    #header-brand {
        width: auto;
        padding: 0 1;
    }

    #header-model {
        width: 1fr;
        text-align: center;
    }

    #header-meta {
        width: auto;
        padding: 0 1;
        text-align: right;
    }

    /* ── chat log ─────────────────────────────────────────── */

    #log {
        width: 100%;
        height: 1fr;
        border: none;
        padding: 0;
        background: #090d13;

        /* Keep the scrollbar invisible in every state (default / hover / active)
           so hovering or clicking the chat never flashes an accent-coloured
           scrollbar - the only focus difference should be the input box border. */
        scrollbar-size: 1 1;
        scrollbar-background: $background;
        scrollbar-background-hover: $background;
        scrollbar-background-active: $background;
        scrollbar-color: $background;
        scrollbar-color-hover: $background;
        scrollbar-color-active: $background;
        scrollbar-corner-color: $background;
    }

    /* ── active turns ─────────────────────────────────────── */

    #active-turns {
        width: 100%;
        height: auto;
        max-height: 65%;
        display: none;
        overflow-y: auto;
        overflow-x: hidden;
        background: #090d13;
        border-top: solid #30363d;
        padding: 1 2 0 2;
        scrollbar-size: 1 1;
        scrollbar-background: #090d13;
        scrollbar-color: #30363d;
    }

    #active-turn-content {
        width: 100%;
        height: auto;
        background: #090d13;
    }

    /* persistent live status bar, just above the input box */
    #statusbar {
        width: 100%;
        height: 1;
        background: $background;
        color: $text-muted;
        padding: 0 1 0 2;
    }

    /* rounded input box (╭─╮ │ ╰─╯), accent border when focused */
    #input-row {
        width: 100%;
        height: 3;
        background: $background;
        border: round #444444;
        padding: 0 1;
    }

    #input-row:focus-within {
        border: round #6cb6ff;
    }

    #prompt-prefix {
        width: auto;
        height: 1;
        padding: 0 1 0 0;
        background: $background;
        color: $text-muted;
    }

    #prompt {
        width: 1fr;
        height: 1;
        border: none;
        padding: 0;
        background: $background;
        color: $text;
    }

    /* dim shortcut hint, just below the input box */
    #hint {
        width: 100%;
        height: 1;
        background: $background;
        color: $text-muted;
        padding: 0 0 0 2;
    }

    Input {
        border: none;
        background: $background;
        padding: 0;
    }

    /* ── keep widgets flat on focus / hover; the input box keeps its    */
    /* rounded border (accent on focus-within, handled above).          */

    Screen:focus-within,
    #log:focus,
    #log:focus-within,
    #log:hover,
    #active-turns:focus,
    #active-turns:focus-within,
    #active-turns:hover,
    #hint:focus,
    #hint:hover,
    #prompt-prefix:focus,
    #prompt-prefix:hover,
    #prompt:focus,
    #prompt:hover,
    Input:focus,
    Input:hover,
    Input.-invalid,
    Input.-invalid:focus {
        border: none;
        background: $background;
    }

    """

    BINDINGS = [
        Binding("ctrl+c", "interrupt", "Interrupt", priority=True),
        Binding("ctrl+o", "toggle_activity_details", "Details", priority=True),
        Binding("ctrl+t", "toggle_reasoning", "Thoughts", priority=True),
        Binding("ctrl+i", "inspect_tools", "Tools", priority=True),
        Binding("ctrl+p", "inspect_plan", "Plan", priority=True),
        Binding("escape", "action_menu", "Action Menu", show=False),
        Binding("ctrl+d", "toggle_dictation", "Dictate", priority=True),
        Binding("ctrl+y", "copy_last_response", "Copy", priority=True),
        Binding("ctrl+g", "edit_in_editor", "Editor", show=False),
        Binding("up", "history_prev", "Previous", show=False),
        Binding("down", "history_next", "Next", show=False),
    ]

    def __init__(
        self,
        base_url: str,
        headers: dict,
        workspace: str | None = None,
        yolo: bool = False,
    ) -> None:
        super().__init__()
        self.base_url = base_url
        self.headers = headers
        self.workspace = workspace
        self.yolo = yolo

        # One HTTP client reused for the app's lifetime (closed on unmount) so
        # each action/stream doesn't pay connection setup. Lazily created.
        self._client: httpx.AsyncClient | None = None

        self._token_buffer: dict[str, str] = {}
        self._reasoning_buffer: dict[str, str] = {}  # model's private chain-of-thought, shown in drawer
        self._reasoning_visible: bool = False
        self._details_expanded: bool = False
        self._turns: list[dict] = []
        self._current_turn_activity: dict[str, dict] = {}
        self._reasoning_start_times: dict[str, float] = {}
        self._recent_thoughts: deque[dict] = deque(maxlen=20)
        self._tool_history: deque[dict] = deque(maxlen=50)
        self._plan_steps: list[dict] = []
        self._dod_results: list[dict] = []
        self._active_phase: str = ""
        self._streaming_active: set[str] = set()
        self._last_assistant_response: str = ""
        self._stream_start_times: dict[str, float] = {}
        self._turn_start_times: dict[str, float] = {}
        self._agent_start_times: dict[str, float] = {}
        self._stream_token_counts: dict[str, int] = {}
        self._streaming_tok_per_sec: float = 0.0
        self._approval_pending: dict | None = None
        # Guards against a duplicate prompt for the same card. If two SSE
        # clients are connected (e.g. TUI + a `north "prompt"` shell, or a
        # stale reconnect), the server fans the event out to both, which would
        # otherwise make the user answer twice. Tracking the card_id collapses
        # repeats of the same pending card into a single prompt.
        self._pending_card_id: str | None = None
        self._user_task_ids: set[str] = set()
        self._last_submitted_prompt: str = ""
        self._conversation_history: deque[dict] = deque(maxlen=5)
        self._pending_user_messages: dict[str, str] = {}
        self._task_tool_activity: dict[str, list[dict]] = {}
        self._last_logged_markup: str = ""

        self._input_history: list[str] = []
        self._history_index: int = -1
        self._current_input: str = ""
        self._spin_frame: int = 0
        self._status_text: str = ""
        self._strategy: str = "cruise"
        self._model: str = ""
        self._settings_path = Path.home() / ".north" / "settings.json"

        # SSE event name → handler. Adding an event = adding one _on_* method
        # and one entry here; no change to the dispatch path itself.
        self._event_handlers: dict[str, Callable[[str, dict], Awaitable[None]]] = {
            "classifying": self._on_classifying,
            "classified": self._on_classified,
            "routed": self._on_routed,
            "north_star_checking": self._on_north_star_checking,
            "north_star_aligned": self._on_north_star_noop,
            "north_star_check_skipped": self._on_north_star_noop,
            "north_star_conflict": self._on_north_star_conflict,
            "model": self._on_model,
            "compaction": self._on_compaction,
            "executing": self._on_agent_started,
            "agent_started": self._on_agent_started,
            "agent_completed": self._on_agent_completed,
            "agent_failed": self._on_agent_failed,
            "tool_called": self._on_tool_called,
            "tool_result": self._on_tool_result,
            "token": self._on_token,
            "reasoning": self._on_reasoning,
            "task_synthesis": self._on_task_synthesis,
            "task_completed": self._on_task_completed,
            "task_failed": self._on_task_failed,
            "task_cancelled": self._on_task_cancelled,
            "task_skipped": self._on_task_skipped,
            "task_rejected": self._on_task_rejected,
            "task_paused": self._on_task_paused,
            "approval_required": self._on_approval_required,
            "question_required": self._on_question_required,
            "waiting_for_model": self._on_waiting_for_model,
            "task_queued": self._on_task_queued,
            "task_resumed": self._on_task_resumed,
            "task_steered": self._on_task_steered,
            "design_phase": self._on_design_phase,
            "plan_seeded": self._on_plan_seeded,
            "plan_updated": self._on_plan_updated,
            "conductor_fix_round": self._on_conductor_fix_round,
            "auto_verify_started": self._on_auto_verify_started,
            "auto_verify": self._on_auto_verify,
            "dod_evaluated": self._on_dod_evaluated,
            "stream_reset": self._on_stream_reset,
            # Integrity signals. north computes these and records them to the ledger;
            # without handlers they were emitted to nobody, so the checks ran invisibly.
            "claims_unverified": self._on_claims_unverified,
            "self_repair_started": self._on_self_repair_started,
            "self_repair_done": self._on_self_repair_done,
            "critic_flagged": self._on_critic_flagged,
            "agent_skipped": self._on_agent_skipped,
            "conductor_review_missing_verdict": self._on_review_missing_verdict,
            "conductor_review_unresolved": self._on_review_unresolved,
            "north_star_check_failed": self._on_north_star_check_failed,
            "task_stuck": self._on_task_stuck,
            "conductor_review_skipped_model_unavailable": self._on_review_skipped,
            "handoff_artifact_missing": self._on_handoff_artifact_missing,
            "spec_critique": self._on_spec_critique,
            "best_of_n": self._on_best_of_n,
            "worktree_integrated": self._on_worktree_integrated,
            "skill_selected": self._on_skill_selected,
            "approval_responded": self._on_approval_responded,
        }

        # ── session metrics (drive the live status bar) ──────────────────────
        self._session_tokens: int = 0  # cumulative estimate (chars/4)
        self._session_cost: float = 0.0  # summed task_completed.cost_usd
        self._compactions: int = 0  # count of 'compaction' SSE events
        self._start_time: float = time.monotonic()
        # Double-Ctrl+C-to-exit: monotonic timestamp of the last single Ctrl+C.
        self._last_interrupt: float = 0.0
        # Paste-preview: large pastes are stashed here and shown as a placeholder
        # until the user presses Enter, keeping the scrollback clean. The exact
        # placeholder text is tracked so that if the user edits the line (types,
        # history, editor) the stale paste is discarded rather than sent.
        self._pending_paste: str | None = None
        self._paste_placeholder: str | None = None

        # ── dictation state ───────────────────────────────────────────────────
        self._recording: bool = False
        self._audio_frames: list = []
        self._audio_stream: object | None = None  # sounddevice.InputStream
        self._sample_rate: int = 16000

    @contextlib.asynccontextmanager
    async def _http(self) -> AsyncIterator[httpx.AsyncClient]:
        """Yield the shared HTTP client, created lazily and closed on unmount."""
        if self._client is None:
            self._client = httpx.AsyncClient()
        yield self._client

    async def on_unmount(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    def compose(self) -> ComposeResult:
        with Horizontal(id="header-bar"):
            yield Static(
                " [bold #a371f7]NORTH[/bold #a371f7] [dim]· active[/dim]",
                id="header-brand",
            )
            yield Static("", id="header-model")
            yield Static("", id="header-meta")
        yield RichLog(id="log", highlight=False, markup=True, wrap=True)
        with VerticalScroll(id="active-turns"):
            yield Static("", id="active-turn-content")
        yield Static("", id="statusbar")
        with Horizontal(id="input-row"):
            yield Static(">", id="prompt-prefix")
            yield Input(
                id="prompt",
                suggester=_NorthSuggester(lambda: self._input_history),
            )
        yield Static("", id="hint")

    def on_mount(self) -> None:
        history_file = Path.home() / ".north" / "tui_history"
        if history_file.exists():
            with contextlib.suppress(Exception):
                self._input_history = [line for line in history_file.read_text().splitlines() if line.strip()]

        self._strategy = _read_power(self._settings_path)
        self._refresh_hint()
        self._render_header_bar()
        self._render_status_bar()
        self._set_status("")


        self.set_interval(0.08, self._tick)
        self.run_worker(self._listen(), exclusive=False)
        self.query_one("#prompt", Input).focus()
        # The completed log and active-turn pane are read-only: keep them out of the
        # focus chain so Tab (or a click) can never move focus off the input and
        # strand the user with keystrokes that go nowhere. Mouse-wheel scrolling
        # still works without focus.
        self.query_one("#log", RichLog).can_focus = False
        self.query_one("#active-turns", VerticalScroll).can_focus = False
        # Defer so the log's width is known before drawing the banner rule.
        self.call_after_refresh(self._draw_banner)

    def _draw_banner(self) -> None:
        # Top-anchored: the banner is the first thing in the log; chat flows
        # downward beneath it and the input stays pinned at the bottom. Agent
        # discovery is async, so the banner is composed in a worker.
        self.run_worker(self._draw_banner_async(), exclusive=False)

    def _write_banner_lines(self, log: RichLog, toolsets: list[str] | None = None) -> None:
        backend = f"textual · {os.environ.get('TERM', 'unknown')}"
        cwd = self.workspace or os.getcwd()
        home = str(Path.home())
        if cwd.startswith(home):
            cwd = "~" + cwd[len(home) :]

        tools = toolsets if toolsets is not None else getattr(self, "_cached_agent_toolsets", None)

        log.write("")
        log.write("  [bold white]north[/bold white]  [bright_black]personal operating system[/bright_black]")
        log.write("")
        log.write(f"  [bright_black]model[/bright_black]     {_short_model(self._model) if self._model else 'auto'}")
        log.write(f"  [bright_black]backend[/bright_black]   {backend}")
        log.write(f"  [bright_black]cwd[/bright_black]       {cwd}")
        log.write(f"  [bright_black]strategy[/bright_black]  {self._strategy}")
        if tools:
            shown = ", ".join(tools[:10]) + ("…" if len(tools) > 10 else "")
            log.write(f"  [bright_black]toolsets[/bright_black]  {shown}")
        if self.yolo:
            log.write("  [#f85149]⚠ YOLO[/#f85149]     [bright_black]auto-approve enabled[/bright_black]")
        log.write("")
        self._write_rule()

    async def _draw_banner_async(self) -> None:
        log = self.query_one("#log", RichLog)
        self._cached_agent_toolsets = await self._fetch_agents()
        self._write_banner_lines(log, self._cached_agent_toolsets)

    async def _fetch_agents(self) -> list[str]:
        """Best-effort list of registered agent names, shown as 'toolsets' in the
        banner. Returns an empty list if the server is unreachable."""
        try:
            async with self._http() as c:
                r = await c.get(
                    f"{self.base_url}/orchestrator/agents",
                    headers=self.headers,
                    timeout=5.0,
                )
                data = r.json()
                names = [a.get("name", "") for a in data if a.get("name")]
                return sorted(n for n in names if n)
        except Exception:
            return []

    async def _set_dial(self, url: str, key: str, value: str | None, ok: str = "") -> None:
        """Set a live dial (power/autonomy) via the settings endpoint.

        Called from slash commands. With no value, just shows the current value
        by re-reading the settings file. Accepts the legacy names too.
        """
        full_url = f"{self.base_url}{url}"
        try:
            async with self._http() as c:
                if value:
                    body = {key: value}
                    r = await c.post(
                        full_url,
                        headers=self.headers,
                        json=body,
                        timeout=5.0,
                    )
                    if r.status_code >= 400:
                        self._log(f"  [red]failed: {r.status_code} {r.text[:120]}[/red]")
                        return
                    data = r.json()
                    shown = data.get("power") if key in ("power", "strategy") else data.get("autonomy")
                    self._log(f"{ok}`{shown}`")
                else:
                    r = await c.get(full_url, headers=self.headers, timeout=5.0)
                    data = r.json()
                    shown = data.get("power") if key in ("power", "strategy") else data.get("autonomy")
                    self._log(f"{ok}`{shown}` (current)")
                self._strategy = _read_power(self._settings_path)
                self._render_status_bar()
        except Exception as exc:
            self._log(f"  [red]error setting {key}: {exc}[/red]")

    # ── rendering helpers ────────────────────────────────────────────────────

    def _refresh_hint(self) -> None:
        turn = self._focused_turn()
        thoughts_open = bool(turn and turn.get("thoughts_expanded"))
        details_open = bool(turn and turn.get("details_expanded"))
        thoughts_label = "ctrl+t hide thoughts" if thoughts_open else "ctrl+t thoughts"
        details_label = "ctrl+o collapse" if details_open else "ctrl+o details"
        hint = (
            f"  {self._strategy}  ·  {details_label}  ·  {thoughts_label}  ·  ctrl+i tools  ·  ctrl+p plan"
            "  ·  esc menu  ·  /help"
        )
        self.query_one("#hint", Static).update(f"[bright_black]{hint}[/bright_black]")

    def _render_header_bar(self) -> None:
        """Compose the sticky top cockpit header bar with active model and session counters."""
        with contextlib.suppress(Exception):
            model = _short_model(self._model) if self._model else "ready"
            self.query_one("#header-model", Static).update(f"[bold #58a6ff]{model}[/bold #58a6ff]")
            strat = self._strategy.upper()
            cost = f"${self._session_cost:.3f}"
            self.query_one("#header-meta", Static).update(f"[dim]{strat}  ·  {cost} [/dim]")

    def _render_status_bar(self) -> None:

        """Compose the live status bar, dropping low-priority segments as the
        terminal narrows so the bar never wraps or truncates mid-segment."""
        from agents.context_compaction import context_window_for

        width = self.size.width or 80

        ctx_max = context_window_for(self._model) if self._model else 0
        fraction = (self._session_tokens / ctx_max) if ctx_max else 0.0

        model = _short_model(self._model) if self._model else " - "
        tokens = f"{_fmt_tokens(self._session_tokens)}/{_fmt_tokens(ctx_max)}" if ctx_max else ""
        bar = _fill_bar(fraction) if ctx_max else ""
        speed = f"⚡ {int(self._streaming_tok_per_sec)} t/s" if self._streaming_tok_per_sec >= 1.0 else ""
        cost = f"${self._session_cost:.4f}"
        compactions = f"⊕{self._compactions}" if self._compactions else ""
        active = sum(1 for _ in self._user_task_ids)
        tasks = f"⚙{active}" if active else ""
        elapsed = _fmt_elapsed(time.monotonic() - self._start_time)
        yolo = "[#f85149]⚠ YOLO[/#f85149]" if self.yolo else ""

        # (text, priority) - higher priority survives longer as width shrinks.
        segments: list[tuple[str, int]] = [
            (f"[#6cb6ff]{model}[/#6cb6ff]", 5),
            (f"{tokens} {bar}".strip(), 4),
            (f"[cyan]{speed}[/cyan]" if speed else "", 4),
            (cost, 3),
            (tasks, 3),
            (compactions, 1),
            (elapsed, 1),
            (yolo, 5),
        ]
        segments = [(t, p) for t, p in segments if t]

        sep = "  ·  "
        chosen = list(segments)
        # Drop the lowest-priority segments until the bar fits the terminal width.
        while chosen:
            plain = sep.join(_strip_markup(t) for t, _ in chosen)
            if len(plain) + 4 <= width:
                break
            lowest = min(p for _, p in chosen)
            idx = next(i for i, (_, p) in enumerate(chosen) if p == lowest)
            chosen.pop(idx)

        line = sep.join(t for t, _ in chosen)
        self.query_one("#statusbar", Static).update(f"[bright_black]{line}[/bright_black]")

    def _write_rule(self) -> None:
        log = self.query_one("#log", RichLog)
        width = log.scrollable_content_region.width or (self.size.width - 1) or 80
        log.write("[bright_black]" + "─" * width + "[/bright_black]")
        log.scroll_end(animate=False)

    def _tick(self) -> None:
        self._spin_frame += 1
        # Refresh the bar roughly once a second so elapsed time ticks live
        # without redrawing on every 80ms animation frame.
        if self._spin_frame % 12 == 0:
            self._render_status_bar()

    def _set_status(self, text: str) -> None:
        """Record transient status for compatibility; task status renders inline."""
        self._status_text = text

    def _log(self, markup: str) -> None:
        if markup == self._last_logged_markup and ("◎" in markup or "→" in markup):
            return
        self._last_logged_markup = markup
        log = self.query_one("#log", RichLog)
        log.write(markup)
        log.scroll_end(animate=False)

    def _log_rich(self, renderable: object) -> None:
        log = self.query_one("#log", RichLog)
        log.write(renderable)  # type: ignore[arg-type]
        log.scroll_end(animate=False)

    # ── task-scoped turn rendering ───────────────────────────────────────────

    def _focused_turn(self) -> dict | None:
        """Return the newest active turn, or the newest completed turn."""
        if self._current_turn_activity:
            return next(reversed(self._current_turn_activity.values()))
        return self._turns[-1] if self._turns else None

    @staticmethod
    def _plan_progress(turn: dict) -> tuple[int, int]:
        steps = turn.get("plan_steps") or []
        done = sum(1 for step in steps if step.get("status") in {"done", "completed"})
        return done, len(steps)

    def _turn_renderables(self, turn: dict, *, active: bool) -> list[object]:
        """Build one self-contained user/activity/answer block."""
        items: list[object] = [RichText("› " + str(turn.get("prompt", "")), style="bold white")]
        expanded = bool(turn.get("details_expanded"))
        phase = str(turn.get("phase") or ("working" if active else turn.get("status", "completed")))

        if expanded:
            for line in _format_turn_details(turn):
                items.append(RichText.from_markup(line))
        else:
            summary = _format_turn_summary(turn).replace("Ctrl+O to expand", "Ctrl+O")
            items.append(RichText.from_markup(summary))

        if active and phase:
            items.append(RichText(f"  ◉ {phase}", style="dim cyan"))

        thoughts = str(turn.get("thoughts") or "")
        if thoughts:
            tokens = int(turn.get("thought_tokens") or max(1, len(thoughts) // 4))
            state = "hide" if turn.get("thoughts_expanded") else "show"
            items.append(RichText.from_markup(
                f"  [cyan]Thinking[/cyan] [bright_black]· {tokens} tokens · Ctrl+T to {state}[/bright_black]"
            ))
            if turn.get("thoughts_expanded"):
                items.append(RichPadding(RichText(thoughts, style="dim"), (0, 0, 0, 4)))

        done, total = self._plan_progress(turn)
        if total:
            items.append(RichText.from_markup(
                f"  [cyan]Plan[/cyan] [bright_black]· {done}/{total} complete · Ctrl+P to inspect[/bright_black]"
            ))

        interaction = turn.get("interaction")
        if interaction:
            kind = interaction.get("kind", "input")
            color = "yellow" if kind == "approval" else "cyan"
            items.append(RichText.from_markup(f"  [{color}]{kind} required[/{color}]"))
            if interaction.get("message"):
                items.append(RichPadding(RichText(str(interaction["message"])), (0, 0, 0, 4)))
            for index, option in enumerate(interaction.get("options") or [], 1):
                items.append(RichText(f"    [{index}] {option}", style="dim"))

        for resolved in turn.get("interactions") or []:
            decision = str(resolved.get("decision") or "answered")
            chosen = str(resolved.get("chosen") or "")
            color = "green" if decision == "approved" else "red" if decision == "rejected" else "cyan"
            items.append(RichText.from_markup(f"  [{color}]✓ {decision}[/{color}] [dim]· {chosen}[/dim]"))

        output = str(turn.get("output") or self._token_buffer.get(str(turn.get("task_id", "")), ""))
        if output:
            label = "North · responding" if active else "North"
            items.append(RichText(label, style="bold #a371f7"))
            items.append(RichPadding(RichMarkdown(output), (0, 0, 0, 2)))
        elif turn.get("status") in {"failed", "skipped", "rejected", "cancelled"}:
            style = "red" if turn.get("status") == "failed" else "yellow"
            items.append(RichText(str(turn.get("error") or turn["status"]), style=style))

        return items

    def _render_active_turns(self) -> None:
        """Update all in-flight turns in place, keeping each task together."""
        with contextlib.suppress(NoMatches):
            wrap = self.query_one("#active-turns", VerticalScroll)
            content = self.query_one("#active-turn-content", Static)
            turns = list(self._current_turn_activity.values())
            wrap.display = bool(turns)
            if not turns:
                content.update("")
                return
            renderables: list[object] = []
            for index, turn in enumerate(turns):
                if index:
                    renderables.append(RichText("─" * max(20, self.size.width - 6), style="bright_black"))
                renderables.extend(self._turn_renderables(turn, active=True))
            content.update(Group(*renderables))
            wrap.scroll_end(animate=False)

    def _append_completed_turn(self, turn: dict) -> None:
        """Append a finalized, grouped turn to immutable scrollback."""
        log = self.query_one("#log", RichLog)
        for renderable in self._turn_renderables(turn, active=False):
            log.write(renderable)  # type: ignore[arg-type]
        self._write_rule()

    def _update_turn_phase(self, task_id: str, phase: str) -> None:
        turn = self._current_turn_activity.get(task_id)
        if turn is not None:
            turn["phase"] = phase
            self._render_active_turns()

    def _update_streaming(self, task_id: str) -> None:
        # The growing buffer uses the same Markdown renderer as the finalized
        # answer, inside the same turn, so neither placement nor chrome jumps.
        self._render_active_turns()

    # ── SSE event handler ────────────────────────────────────────────────────

    async def _handle_event(self, event: str, data: dict) -> None:
        task_id = data.get("task_id", "")
        # Filter out background daemons/cron tasks from hijacking the interactive chat view
        if task_id and task_id not in self._user_task_ids:
            return
        handler = self._event_handlers.get(event)
        if handler is not None:
            await handler(task_id, data)

    async def _on_classifying(self, task_id: str, data: dict) -> None:
        self._set_status("classifying…")
        self._update_turn_phase(task_id, "classifying")

    async def _on_classified(self, task_id: str, data: dict) -> None:
        domain = data.get("domain", "")
        is_consequential = bool(data.get("is_consequential"))
        if task_id in self._current_turn_activity:
            self._current_turn_activity[task_id]["domain"] = domain
            self._current_turn_activity[task_id]["is_consequential"] = is_consequential
        self._set_status(f"routing → {domain}…")
        self._update_turn_phase(task_id, f"routing · {domain}" if domain else "routing")

    async def _on_routed(self, task_id: str, data: dict) -> None:
        agents = data.get("agents") or []
        if task_id in self._current_turn_activity:
            self._current_turn_activity[task_id]["agents"] = list(agents)
        self._set_status(f"running {', '.join(agents) or 'general'}…")
        self._update_turn_phase(task_id, f"running · {', '.join(agents) or 'general'}")

    async def _on_north_star_checking(self, task_id: str, data: dict) -> None:
        self._set_status("checking goals…")
        self._update_turn_phase(task_id, "checking goals")

    async def _on_north_star_noop(self, task_id: str, data: dict) -> None:
        """north_star_aligned / north_star_check_skipped - no UI change."""

    async def _on_north_star_conflict(self, task_id: str, data: dict) -> None:
        tension = (data.get("tension") or "")[:200]
        self._set_status("")
        turn = self._current_turn_activity.get(task_id)
        if turn is not None:
            turn["phase"] = "goal conflict"
            turn["interaction"] = {"kind": "goal conflict", "message": tension, "options": []}
            self._render_active_turns()

    async def _on_model(self, task_id: str, data: dict) -> None:
        self._model = data.get("model", "")
        if task_id in self._current_turn_activity:
            self._current_turn_activity[task_id]["model"] = self._model
        self._refresh_hint()
        self._render_header_bar()
        self._render_status_bar()

    async def _on_compaction(self, task_id: str, data: dict) -> None:
        self._compactions += 1
        self._render_status_bar()

    async def _on_agent_started(self, task_id: str, data: dict) -> None:
        if task_id:
            self._agent_start_times.setdefault(task_id, time.monotonic())
        agent = data.get("agent", "")
        agents = data.get("agents") or []
        label = ", ".join(agents) if agents else agent or "general"
        if task_id in self._current_turn_activity:
            if agents:
                self._current_turn_activity[task_id]["agents"] = list(agents)
            elif agent and agent not in self._current_turn_activity[task_id]["agents"]:
                self._current_turn_activity[task_id]["agents"].append(agent)
            if self._model:
                self._current_turn_activity[task_id]["model"] = self._model
        self._set_status(f"running {label}…")
        self._update_turn_phase(task_id, f"running · {label}")

    async def _on_agent_completed(self, task_id: str, data: dict) -> None:
        """Close one visible agent-tree node without completing the whole task."""
        agent = data.get("agent", "agent")
        summary = str(data.get("summary") or "")[:120]
        turn = self._current_turn_activity.get(task_id)
        if turn is not None:
            turn.setdefault("agent_events", []).append({"agent": agent, "status": "completed", "summary": summary})
            turn["phase"] = f"{agent} completed"
            self._render_active_turns()

    async def _on_agent_failed(self, task_id: str, data: dict) -> None:
        agent = data.get("agent", "agent")
        error = str(data.get("error") or "failed")[:120]
        turn = self._current_turn_activity.get(task_id)
        if turn is not None:
            turn.setdefault("agent_events", []).append({"agent": agent, "status": "failed", "summary": error})
            turn["phase"] = f"{agent} failed"
            self._render_active_turns()

    async def _on_tool_called(self, task_id: str, data: dict) -> None:
        # Whatever streamed before a tool call was narration on the way to it
        # ("I'll check the repo for..."), not the answer. Keeping it glued the
        # two together with no separator; the answer is what streams after the
        # last tool call.
        self._token_buffer[task_id] = ""
        tool = data.get("tool", "")
        params = data.get("params") or {}
        params_str = _fmt_params(params)
        self._set_status(f"{tool}…")
        entry = {
            "task_id": task_id,
            "run_id": data.get("run_id"),
            "tool": tool,
            "params": params,
            "params_str": params_str,
            "result": None,
            "formatted": None,
            "success": True,
            "error": None,
            "start_time": time.monotonic(),
            "duration": None,
        }
        self._tool_history.append(entry)
        if task_id:
            self._task_tool_activity.setdefault(task_id, []).append(
                {"tool": tool, "params": params_str, "result": None}
            )
            if task_id in self._current_turn_activity:
                self._current_turn_activity[task_id]["tools"].append(entry)
                self._current_turn_activity[task_id]["phase"] = tool
                self._render_active_turns()

    async def _on_tool_result(self, task_id: str, data: dict) -> None:
        tool = data.get("tool", "")
        success = data.get("success", True)
        formatted = data.get("formatted", "")
        error = data.get("error", "")
        result = (
            formatted[:200].replace("\n", " ")
            if formatted
            else (error[:200].replace("\n", " ") if error else ("ok" if success else "failed"))
        )
        history_entry = next(
            (
                entry
                for entry in reversed(self._tool_history)
                if entry.get("task_id") == task_id and entry.get("tool") == tool and entry.get("result") is None
            ),
            None,
        )
        if history_entry is not None:
            history_entry["result"] = result
            history_entry["formatted"] = formatted
            history_entry["success"] = success
            history_entry["error"] = error
            st = history_entry.get("start_time")
            if st:
                history_entry["duration"] = max(0.01, time.monotonic() - st)


        if task_id and task_id in self._task_tool_activity:
            tools = self._task_tool_activity[task_id]
            if tools:
                tools[-1]["result"] = result

        if task_id and task_id in self._current_turn_activity:
            turn_tools = self._current_turn_activity[task_id].get("tools", [])
            turn_entry = next(
                (entry for entry in reversed(turn_tools) if entry.get("tool") == tool and entry.get("result") is None),
                None,
            )
            if turn_entry is not None:
                turn_entry["result"] = result
                turn_entry["success"] = success
                turn_entry["error"] = error
                st = turn_entry.get("start_time")
                if st:
                    turn_entry["duration"] = max(0.01, time.monotonic() - st)
            self._render_active_turns()

    async def _on_token(self, task_id: str, data: dict) -> None:
        text = data.get("text", "")
        if not text:
            return
        now = time.monotonic()
        if task_id not in self._stream_start_times:
            self._stream_start_times[task_id] = now
            self._stream_token_counts[task_id] = 0
        self._stream_token_counts[task_id] = self._stream_token_counts.get(task_id, 0) + 1
        elapsed = max(0.001, now - self._stream_start_times[task_id])
        self._streaming_tok_per_sec = self._stream_token_counts[task_id] / elapsed

        buf = self._token_buffer.get(task_id, "") + text
        self._token_buffer[task_id] = buf
        self._session_tokens += max(1, len(text) // 4)
        if task_id not in self._streaming_active:
            self._streaming_active.add(task_id)
            thoughts = self._reasoning_buffer.get(task_id, "")
            if thoughts:
                toks = max(1, len(thoughts) // 4)
                start_t = (
                    self._reasoning_start_times.get(task_id)
                    or self._agent_start_times.get(task_id)
                    or self._turn_start_times.get(task_id, now)
                )
                dur = max(0.01, now - start_t)
                if task_id in self._current_turn_activity:
                    self._current_turn_activity[task_id]["thought_duration"] = dur
                    self._current_turn_activity[task_id]["thought_tokens"] = toks
                    self._current_turn_activity[task_id]["thoughts"] = thoughts
                self._recent_thoughts.append({
                    "task_id": task_id,
                    "thoughts": thoughts,
                    "tokens": toks,
                    "duration": dur,
                })
            self._set_status("")
        self._update_streaming(task_id)
        self._render_status_bar()

    async def _on_reasoning(self, task_id: str, data: dict) -> None:
        text = data.get("text", "")
        if not text or task_id in self._streaming_active:
            return
        if task_id not in self._reasoning_start_times:
            self._reasoning_start_times[task_id] = time.monotonic()
        buf = self._reasoning_buffer.get(task_id, "") + text
        self._reasoning_buffer[task_id] = buf
        turn = self._current_turn_activity.get(task_id)
        if turn is not None:
            turn["thoughts"] = buf
            turn["thought_tokens"] = max(1, len(buf) // 4)
            turn["phase"] = "thinking"
            self._render_active_turns()
        preview = " ".join(buf.split())[-_REASONING_PREVIEW_CHARS:]
        self._set_status(f"thinking… {preview}")

        elapsed = time.monotonic() - self._reasoning_start_times[task_id]
        if turn is not None:
            turn["thought_duration"] = elapsed

    async def _on_task_synthesis(self, task_id: str, data: dict) -> None:
        self._set_status("synthesising…")
        self._update_turn_phase(task_id, "synthesising")

    async def _on_task_completed(self, task_id: str, data: dict) -> None:
        sys.stdout.write("\a")
        sys.stdout.flush()
        now = time.monotonic()
        self._session_cost += float(data.get("cost_usd", 0.0) or 0.0)
        output = self._token_buffer.pop(task_id, "")
        self._reasoning_buffer.pop(task_id, None)
        self._stream_start_times.pop(task_id, None)
        start_t = self._turn_start_times.pop(task_id, None) or self._agent_start_times.pop(task_id, None)
        self._agent_start_times.pop(task_id, None)
        self._reasoning_start_times.pop(task_id, None)
        self._stream_token_counts.pop(task_id, None)
        self._streaming_tok_per_sec = 0.0
        self._streaming_active.discard(task_id)

        if not output:
            output = await self._fetch_ledger_output(task_id)

        turn = self._current_turn_activity.pop(task_id, None)
        if turn is not None:
            turn["output"] = output
            turn["status"] = "completed"
            turn["phase"] = "completed"
            if start_t:
                turn["turn_duration"] = max(0.01, now - start_t)
            self._turns.append(turn)

        if turn is not None:
            self._append_completed_turn(turn)
        self._render_active_turns()

        if output:
            self._last_assistant_response = output

        # Refresh strategy in case the user issued a strategy command.
        self._strategy = _read_power(self._settings_path)
        self._refresh_hint()
        self._set_status("")
        self._user_task_ids.discard(task_id)
        user_msg = self._pending_user_messages.pop(task_id, "")
        tools_used = self._task_tool_activity.pop(task_id, [])
        if user_msg and output:
            short = output[:600] + ("…" if len(output) > 600 else "")
            self._conversation_history.append({"user": user_msg, "tools": tools_used, "north": short})
        self._render_status_bar()

    async def _fetch_ledger_output(self, task_id: str) -> str:
        """Reconstruct a completed task's answer from the ledger when no tokens streamed."""
        try:
            async with self._http() as c:
                r = await c.get(
                    f"{self.base_url}/orchestrator/ledger",
                    params={"task_id": task_id, "limit": 20},
                    headers=self.headers,
                    timeout=5.0,
                )
                entries = r.json()
                return _reconstruct_task_output(entries)
        except Exception:
            return ""

    async def _on_task_failed(self, task_id: str, data: dict) -> None:
        sys.stdout.write("\a")
        sys.stdout.flush()
        self._streaming_active.discard(task_id)
        partial_output = self._token_buffer.pop(task_id, "")
        self._reasoning_buffer.pop(task_id, None)
        self._stream_start_times.pop(task_id, None)
        self._turn_start_times.pop(task_id, None)
        self._agent_start_times.pop(task_id, None)
        self._reasoning_start_times.pop(task_id, None)
        self._stream_token_counts.pop(task_id, None)
        self._streaming_tok_per_sec = 0.0
        self._task_tool_activity.pop(task_id, None)
        error = data.get("error", "Task failed.")
        turn = self._current_turn_activity.pop(task_id, None)
        if turn is not None:
            turn["status"] = "failed"
            turn["error"] = error
            turn["phase"] = "failed"
            turn["output"] = partial_output
            self._turns.append(turn)
        self._set_status("")
        self._user_task_ids.discard(task_id)
        if turn is not None:
            self._append_completed_turn(turn)
        self._render_active_turns()
        self._render_status_bar()

    async def _on_task_cancelled(self, task_id: str, data: dict) -> None:
        self._streaming_active.discard(task_id)
        partial_output = self._token_buffer.pop(task_id, "")
        self._reasoning_buffer.pop(task_id, None)
        self._stream_start_times.pop(task_id, None)
        self._turn_start_times.pop(task_id, None)
        self._agent_start_times.pop(task_id, None)
        self._reasoning_start_times.pop(task_id, None)
        self._stream_token_counts.pop(task_id, None)
        self._streaming_tok_per_sec = 0.0
        self._task_tool_activity.pop(task_id, None)
        turn = self._current_turn_activity.pop(task_id, None)
        if turn is not None:
            turn["status"] = "cancelled"
            turn["error"] = "cancelled"
            turn["phase"] = "cancelled"
            turn["output"] = partial_output
            self._turns.append(turn)
        self._set_status("")
        self._user_task_ids.discard(task_id)
        if turn is not None:
            self._append_completed_turn(turn)
        self._render_active_turns()
        self._render_status_bar()

    async def _on_task_skipped(self, task_id: str, data: dict) -> None:
        self._streaming_active.discard(task_id)
        partial_output = self._token_buffer.pop(task_id, "")
        self._reasoning_buffer.pop(task_id, None)
        self._stream_start_times.pop(task_id, None)
        self._turn_start_times.pop(task_id, None)
        self._agent_start_times.pop(task_id, None)
        self._reasoning_start_times.pop(task_id, None)
        self._stream_token_counts.pop(task_id, None)
        self._streaming_tok_per_sec = 0.0
        self._task_tool_activity.pop(task_id, None)
        reason = data.get("reason", "Task skipped.")
        turn = self._current_turn_activity.pop(task_id, None)
        if turn is not None:
            turn["status"] = "skipped"
            turn["error"] = reason
            turn["phase"] = "skipped"
            turn["output"] = partial_output
            self._turns.append(turn)
        self._set_status("")
        self._user_task_ids.discard(task_id)
        if turn is not None:
            self._append_completed_turn(turn)
        self._render_active_turns()
        self._render_status_bar()

    async def _on_task_rejected(self, task_id: str, data: dict) -> None:
        self._streaming_active.discard(task_id)
        partial_output = self._token_buffer.pop(task_id, "")
        self._reasoning_buffer.pop(task_id, None)
        self._stream_start_times.pop(task_id, None)
        self._stream_token_counts.pop(task_id, None)
        self._streaming_tok_per_sec = 0.0
        self._task_tool_activity.pop(task_id, None)
        reason = data.get("reason", "Task rejected.")
        turn = self._current_turn_activity.pop(task_id, None)
        if turn is not None:
            turn["status"] = "rejected"
            turn["error"] = reason
            turn["phase"] = "rejected"
            turn["output"] = partial_output
            self._turns.append(turn)
        self._set_status("")
        self._user_task_ids.discard(task_id)
        if turn is not None:
            self._append_completed_turn(turn)
        self._render_active_turns()
        self._render_status_bar()

    async def _on_task_paused(self, task_id: str, data: dict) -> None:
        self._set_status("paused")
        self._update_turn_phase(task_id, "paused")
        self._render_status_bar()

    async def _on_waiting_for_model(self, task_id: str, data: dict) -> None:
        wait_secs = int(data.get("wait_seconds", 10))
        reason = data.get("reason", "rate limits")
        self._set_status(f"waiting for model ({reason}, retrying in {wait_secs}s)…")
        self._update_turn_phase(task_id, f"waiting for model · {reason}")

    async def _on_task_queued(self, task_id: str, data: dict) -> None:
        reason = data.get("reason", "waiting for model capacity")
        retry_after = data.get("retry_after")
        time_info = f" (retrying in ~{int(retry_after)}s)" if retry_after else ""
        self._set_status("task queued (waiting for models)…")
        self._update_turn_phase(task_id, f"queued · {reason}{time_info}")

    async def _on_task_resumed(self, task_id: str, data: dict) -> None:
        self._set_status("resuming task from queue…")
        self._update_turn_phase(task_id, "resuming")

    async def _on_approval_required(self, task_id: str, data: dict) -> None:
        card_id = data.get("card_id", "")
        # Ignore a repeat of a card we're already prompting for (see
        # self._pending_card_id). Prevents double-prompting when the event is
        # delivered more than once (multiple SSE clients / reconnects).
        if self._pending_card_id == card_id and self._approval_pending is not None:
            return
        self._approval_pending = data
        self._pending_card_id = card_id
        self._set_status("")
        options = data.get("options") or ["Approve", "Reject"]
        msg = data.get("message", "")
        turn = self._current_turn_activity.get(task_id)
        if turn is not None:
            turn["phase"] = "waiting for approval"
            turn["interaction"] = {"kind": "approval", "message": msg, "options": options}
            self._render_active_turns()
        if self.yolo:
            # Auto-approve mode: take the first option without prompting.
            await self._submit_approval("1")
            return
        if turn is not None:
            return
        # Check if the message contains a unified diff code block
        if "```diff" in msg:
            pre, _, rest = msg.partition("```diff\n")
            diff_body, _, post = rest.partition("```")
            added, deleted, files = summarize_diff(diff_body)
            if files or added or deleted:
                fnames = ", ".join(files[:3]) + ("…" if len(files) > 3 else "")
                self._log(
                    f"    [dim]Diff:[/dim] [cyan]{fnames or 'patch'}[/cyan]  "
                    f"[green]+{added}[/green] [red]-{deleted}[/red]  "
                    f"[bright_black](Ctrl+I to inspect)[/bright_black]"
                )
            if pre.strip():
                self._log_rich(RichText("    " + pre.strip(), style="white"))
            self._log_rich(
                RichPadding(
                    RichSyntax(diff_body.strip(), "diff", background_color="default", line_numbers=True),
                    (0, 0, 0, 4),
                )
            )
            if post.strip():
                self._log_rich(RichText("    " + post.strip(), style="white"))
        else:
            self._log_rich(RichText("    " + msg, style="white"))

        if options == ["Approve", "Reject"]:
            self._log(
                "    [bold green][y][/bold green] [white]Approve (1)[/white]    "
                "[bold red][n][/bold red] [white]Reject (2)[/white]    "
                "[bright_black](or type rejection feedback)[/bright_black]"
            )
        else:
            for i, opt in enumerate(options, 1):
                self._log(f"    [bright_black][{i}][/bright_black]  {opt}")


    async def _on_design_phase(self, task_id: str, data: dict) -> None:
        step = data.get("step", "")
        self._active_phase = f"design phase: {step}"
        self._set_status(f"design phase: {step}…")
        turn = self._current_turn_activity.get(task_id)
        if turn is not None:
            turn["active_phase"] = self._active_phase
        self._update_turn_phase(task_id, self._active_phase)

    async def _on_plan_seeded(self, task_id: str, data: dict) -> None:
        tasks = data.get("tasks", 0)
        default_steps = [
            {"step_id": i + 1, "task": f"Task Step {i+1}", "agent": "coder", "status": "pending"}
            for i in range(tasks)
        ]
        steps = data.get("steps") or default_steps
        self._plan_steps = steps
        turn = self._current_turn_activity.get(task_id)
        if turn is not None:
            turn["plan_steps"] = steps
            self._render_active_turns()

    async def _on_plan_updated(self, task_id: str, data: dict) -> None:
        """Keep the inline plan synchronized with the agent's live checklist."""
        rendered = str(data.get("plan") or "")
        steps: list[dict] = []
        marks = {"[x]": "done", "[~]": "in_progress", "[ ]": "pending"}
        for index, line in enumerate(rendered.splitlines(), 1):
            stripped = line.strip()
            mark = next((candidate for candidate in marks if stripped.startswith(candidate)), None)
            if mark is None:
                continue
            steps.append({
                "step_id": index,
                "agent": "",
                "task": stripped[len(mark):].strip(),
                "status": marks[mark],
            })
        turn = self._current_turn_activity.get(task_id)
        if turn is not None:
            if steps:
                turn["plan_steps"] = steps
            turn["phase"] = f"plan · {int(data.get('done') or 0)}/{int(data.get('total') or len(steps))} complete"
            self._plan_steps = list(turn.get("plan_steps") or [])
            self._render_active_turns()

    async def _on_conductor_fix_round(self, task_id: str, data: dict) -> None:
        round_num = data.get("round", 1)
        self._active_phase = f"reviewer fix round {round_num}"
        self._set_status(f"reviewer fix round {round_num}…")
        turn = self._current_turn_activity.get(task_id)
        if turn is not None:
            turn["active_phase"] = self._active_phase
        self._update_turn_phase(task_id, self._active_phase)

    async def _on_auto_verify_started(self, task_id: str, data: dict) -> None:
        cmd = data.get("command", "")
        self._set_status(f"verifying ({cmd})…")
        self._update_turn_phase(task_id, f"verifying · {cmd}")

    async def _on_auto_verify(self, task_id: str, data: dict) -> None:
        cmd = data.get("command", "")
        passed = data.get("passed", False)
        if task_id in self._current_turn_activity:
            self._current_turn_activity[task_id]["verifications"].append({
                "command": cmd,
                "passed": passed,
            })
            self._current_turn_activity[task_id]["phase"] = "verification passed" if passed else "verification failed"
            self._render_active_turns()

    async def _on_dod_evaluated(self, task_id: str, data: dict) -> None:
        self._dod_results.append(data)
        turn = self._current_turn_activity.get(task_id)
        if turn is not None:
            turn.setdefault("dod_results", []).append(data)
            self._render_active_turns()

    async def _on_task_steered(self, task_id: str, data: dict) -> None:
        instruction = data.get("instruction", "")
        self._update_turn_phase(task_id, f"steered · {instruction}")

    # ── integrity signals ────────────────────────────────────────────────
    # north checks its own work (claims vs tool evidence, self-repair, a critic
    # pass) and records the verdicts. These render them, so a run that did not
    # earn a clean result says so rather than looking identical to one that did.

    async def _on_claims_unverified(self, task_id: str, data: dict) -> None:
        violations = data.get("violations") or []
        agent = data.get("agent", "agent")
        self._log(f"  [yellow]⚠ unverified[/yellow]  [bright_black]{agent}[/bright_black]")
        for violation in violations:
            self._log(f"    [bright_black]· {violation}[/bright_black]")
        self._update_turn_phase(task_id, f"unverified claims · {len(violations)}")

    async def _on_self_repair_started(self, task_id: str, data: dict) -> None:
        agent = data.get("agent", "agent")
        # Drop the answer streamed so far: the correction pass streams its own
        # over the same channel, and keeping both concatenated the two drafts
        # into one reply that contradicted itself halfway through.
        self._token_buffer[task_id] = ""
        self._update_turn_phase(task_id, f"self-repair · {agent}")
        self._set_status("correcting unverified claims…")

    async def _on_self_repair_done(self, task_id: str, data: dict) -> None:
        remaining = data.get("remaining") or []
        agent = data.get("agent", "agent")
        if remaining:
            self._log(f"  [yellow]⚠ self-repair[/yellow]  {agent}: {len(remaining)} claim(s) still unsupported")
        else:
            self._log(f"  [green]✓ self-repair[/green]  [bright_black]{agent} corrected its claims[/bright_black]")

    async def _on_critic_flagged(self, task_id: str, data: dict) -> None:
        gap = data.get("gap", "")
        self._log(f"  [yellow]⚠ reviewer[/yellow]  [bright_black]{gap}[/bright_black]")

    async def _on_agent_skipped(self, task_id: str, data: dict) -> None:
        agent = data.get("agent", "agent")
        failed = ", ".join(data.get("failed_dependencies") or [])
        self._log(f"  [yellow]⊘ skipped[/yellow]  {agent} [bright_black]· depends on {failed}[/bright_black]")

    async def _on_review_missing_verdict(self, task_id: str, data: dict) -> None:
        self._log("  [yellow]⚠ review[/yellow]  [bright_black]no machine-readable verdict - retrying[/bright_black]")

    async def _on_review_unresolved(self, task_id: str, data: dict) -> None:
        must_fix = data.get("must_fix") or []
        self._log(f"  [yellow]⚠ review[/yellow]  {len(must_fix)} item(s) unresolved after the fix rounds")
        for item in must_fix[:5]:
            self._log(f"    [bright_black]· {item}[/bright_black]")

    async def _on_north_star_check_failed(self, task_id: str, data: dict) -> None:
        reason = data.get("reason", "")
        self._log(f"  [yellow]⚠ goals[/yellow]  [bright_black]could not evaluate: {reason}[/bright_black]")

    async def _on_task_stuck(self, task_id: str, data: dict) -> None:
        self._log("  [red]✕ stuck[/red]  [bright_black]no progress - cancelled by the watchdog[/bright_black]")
        self._update_turn_phase(task_id, "stuck")

    async def _on_review_skipped(self, task_id: str, data: dict) -> None:
        reason = data.get("reason", "no model available")
        self._log(f"  [yellow]⚠ review skipped[/yellow]  [bright_black]{reason}[/bright_black]")

    async def _on_handoff_artifact_missing(self, task_id: str, data: dict) -> None:
        agent = data.get("agent", "agent")
        artifact = data.get("artifact", "its handoff artifact")
        self._log(f"  [yellow]⚠ handoff[/yellow]  {agent} finished without writing {artifact}")

    async def _on_spec_critique(self, task_id: str, data: dict) -> None:
        issues = data.get("issues") or []
        independent = data.get("independent", False)
        mark = "" if independent else " [bright_black](same model as the architect)[/bright_black]"
        if not issues:
            self._log(f"  [green]✓ spec review[/green]  [bright_black]no material flaws[/bright_black]{mark}")
            return
        self._log(f"  [yellow]⚠ spec review[/yellow]  {len(issues)} concern(s){mark}")
        for issue in issues:
            self._log(f"    [bright_black]· {issue}[/bright_black]")

    async def _on_best_of_n(self, task_id: str, data: dict) -> None:
        candidates = data.get("candidates", 0)
        winner = data.get("winner")
        viable = data.get("viable", False)
        if not viable:
            self._log(f"  [yellow]⚠ best-of-{candidates}[/yellow]  [bright_black]no viable candidate[/bright_black]")
            return
        self._log(f"  [bright_black]best-of-{candidates} · picked candidate {winner}[/bright_black]")

    async def _on_worktree_integrated(self, task_id: str, data: dict) -> None:
        agent = data.get("agent", "agent")
        if data.get("conflicted"):
            branch = data.get("branch", "")
            self._log(f"  [yellow]⚠ conflict[/yellow]  {agent}'s changes kept on [bright_black]{branch}[/bright_black]")
        elif data.get("changed"):
            self._log(f"  [green]✓ integrated[/green]  [bright_black]{agent}'s isolated changes applied[/bright_black]")

    async def _on_skill_selected(self, task_id: str, data: dict) -> None:
        names = ", ".join(data.get("skills") or [])
        if names:
            self._log(f"  [bright_black]skills · {names}[/bright_black]")

    async def _on_approval_responded(self, task_id: str, data: dict) -> None:
        decision = data.get("chosen_option") or data.get("decision", "")
        self._log(f"  [bright_black]decision · {decision}[/bright_black]")

    async def _on_stream_reset(self, task_id: str, data: dict) -> None:
        # Reset token buffer when tool calling is engaged mid-thought
        self._token_buffer.pop(task_id, None)
        self._reasoning_buffer.pop(task_id, None)
        if task_id in self._streaming_active:
            self._render_active_turns()

    async def _on_question_required(self, task_id: str, data: dict) -> None:
        # Reuses the pending-card input path; a free-form answer is allowed.
        card_id = data.get("card_id", "")
        if self._pending_card_id == card_id and self._approval_pending is not None:
            return
        # The agent is asking a clarifying question and is blocked until we answer.
        self._approval_pending = data
        self._pending_card_id = card_id
        self._set_status("")
        options = data.get("options") or []
        turn = self._current_turn_activity.get(task_id)
        if turn is not None:
            turn["phase"] = "waiting for answer"
            turn["interaction"] = {
                "kind": "question",
                "message": data.get("question", ""),
                "options": options,
            }
            self._render_active_turns()
        if self.yolo:
            ans = options[0] if options else "Use your best judgment."
            await self._submit_approval("1" if options else ans)
            return
        if turn is None:
            self._log("  [cyan]question required[/cyan]")
            self._log_rich(RichText("    " + data.get("question", ""), style="white"))
            for index, option in enumerate(options, 1):
                self._log(f"    [bright_black][{index}][/bright_black]  {option}")

    # ── SSE listener (runs as Textual worker in the same event loop) ─────────

    async def _listen(self) -> None:
        delay = _SSE_BACKOFF_BASE
        while True:
            try:
                async with (
                    self._http() as client,
                    client.stream(
                        "GET",
                        f"{self.base_url}/orchestrator/stream",
                        headers=self.headers,
                        timeout=None,
                    ) as resp,
                ):
                    if resp.status_code != 200:
                        await resp.aread()
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, _SSE_BACKOFF_MAX)
                        continue
                    # Successful connection - reset backoff.
                    delay = _SSE_BACKOFF_BASE
                    current_event = ""
                    async for line in resp.aiter_lines():
                        if line.startswith("event:"):
                            current_event = line[6:].strip()
                        elif line.startswith("data:"):
                            try:
                                payload = json.loads(line[5:].strip())
                            except json.JSONDecodeError:
                                current_event = ""
                                continue
                            ev = current_event or payload.get("event", "")
                            await self._handle_event(ev, payload)
                            current_event = ""
            except asyncio.CancelledError:
                return
            except Exception:
                await asyncio.sleep(delay)
                delay = min(delay * 2, _SSE_BACKOFF_MAX)

    # ── approval ─────────────────────────────────────────────────────────────

    async def _submit_approval(self, raw: str) -> None:
        data = self._approval_pending
        self._approval_pending = None
        self._pending_card_id = None
        if data is None:
            return
        # A question card carries "question" and has no Approve/Reject semantics  -
        # any selection (numbered option or free text) is an "answered" decision.
        is_question = bool(data.get("question"))
        options = data.get("options") or ([] if is_question else ["Approve", "Reject"])
        feedback = ""
        try:
            idx = int(raw) - 1
            chosen = options[idx] if 0 <= idx < len(options) else raw
        except ValueError:
            if is_question:
                chosen = raw  # free-form answer
            else:
                low = raw.lower()
                if low in ("y", "yes", "a", "approve", "approved", "ok"):
                    chosen = options[0]
                elif low in ("n", "no", "r", "reject", "rejected"):
                    chosen = options[1] if len(options) > 1 else options[0]
                elif low.startswith(("n ", "reject ", "no ", "r ")):
                    feedback = raw.split(maxsplit=1)[1].strip()
                    chosen = f"Reject: {feedback}"
                else:
                    chosen = raw or options[0]

        if is_question:
            decision = "answered"
        else:
            decision = (
                "approved"
                if chosen == options[0]
                else "rejected"
                if (len(options) > 1 and chosen == options[1]) or feedback or chosen.lower().startswith("reject")
                else "answered"
            )

        task_id = str(data.get("task_id") or "")
        turn = self._current_turn_activity.get(task_id)
        if turn is not None:
            pending = turn.get("interaction") or {}
            turn.setdefault("interactions", []).append({
                "kind": pending.get("kind", "question" if is_question else "approval"),
                "message": pending.get("message", ""),
                "chosen": chosen,
                "decision": decision,
            })
            turn["interaction"] = None
            turn["phase"] = f"{decision} · {chosen}"
            self._render_active_turns()
        else:
            style = "green" if decision == "approved" else "red" if decision == "rejected" else "cyan"
            self._log(f"    [dim {style}]✓  {decision}: {chosen}[/dim {style}]")

        try:
            async with self._http() as c:
                await c.post(
                    f"{self.base_url}/orchestrator/approval/respond",
                    headers=self.headers,
                    json={
                        "card_id": data.get("card_id", ""),
                        "task_id": data.get("task_id", ""),
                        "agent": data.get("agent", ""),
                        "decision": decision,
                        "chosen_option": chosen,
                    },
                    timeout=10.0,
                )
        except Exception:
            pass


    # ── input ─────────────────────────────────────────────────────────────────

    def on_key(self, event) -> None:
        """Tab accepts the ghost-text suggestion when the prompt is focused and a
        completion is available; otherwise Tab falls through to focus movement."""
        if event.key != "tab":
            return
        prompt = self.query_one("#prompt", Input)
        if not prompt.has_focus or not prompt.value:
            return
        suggestion = _compute_suggestion(prompt.value, self._input_history)
        if suggestion:
            prompt.value = suggestion
            prompt.cursor_position = len(suggestion)
            event.prevent_default()
            event.stop()

    def on_paste(self, event) -> None:
        """Large multi-line pastes are previewed as a compact placeholder instead
        of flooding the input line; the real text is sent on Enter."""
        text = getattr(event, "text", "")
        n_lines = text.count("\n") + 1
        if n_lines < 3 and len(text) <= 200:
            return  # small paste - let the Input insert it normally
        self._pending_paste = text
        prompt = self.query_one("#prompt", Input)
        prompt.value = f"[pasted: {n_lines} lines, {len(text)} chars - press Enter to send]"
        self._paste_placeholder = prompt.value
        prompt.cursor_position = len(prompt.value)
        event.prevent_default()
        event.stop()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.clear()
        # A pending paste is sent only if the placeholder is still intact; if the
        # user edited the line (typed over it, used history, or the editor), send
        # what they see and drop the stale paste so input and action never diverge.
        if self._pending_paste is not None:
            if event.value == self._paste_placeholder:
                text = self._pending_paste.strip()
            self._pending_paste = None
            self._paste_placeholder = None
        if not text:
            return

        if text.startswith("north ") and not self._approval_pending:
            text = "/" + text[6:].strip()

        if text.startswith("/") and not self._approval_pending:
            await self._handle_slash(text)
            return

        if text.lower() in ("exit", "quit", "bye"):
            self._log("  [dim]goodbye[/dim]")
            self.exit()
            return

        if self._approval_pending:
            await self._submit_approval(text)
            return

        if not self._input_history or self._input_history[-1] != text:
            self._input_history.append(text)
        self._history_index = -1
        self._current_input = ""
        try:
            history_file = Path.home() / ".north" / "tui_history"
            history_file.parent.mkdir(parents=True, exist_ok=True)
            history_file.write_text("\n".join(self._input_history[-1000:]))
        except Exception:
            pass

        body: dict = {"prompt": text}
        if self.workspace:
            body["workspace"] = self.workspace
        if self._conversation_history:
            turns: list[str] = []
            for turn in self._conversation_history:
                parts = [f"User: {turn['user']}"]
                if turn.get("tools"):
                    summaries = [
                        f"{e['tool']}({e['params']}) → {e['result']}"
                        if e.get("params")
                        else f"{e['tool']} → {e['result']}"
                        for e in turn["tools"]
                        if e.get("result")
                    ]
                    if summaries:
                        parts.append("[actions: " + "; ".join(summaries) + "]")
                parts.append(f"north: {turn['north']}")
                turns.append("\n".join(parts))
            body["context"] = "## Recent conversation\n" + "\n\n".join(turns)

        self._set_status("…")
        self._last_submitted_prompt = text
        try:
            async with self._http() as c:
                resp = await c.post(
                    f"{self.base_url}/orchestrator/task",
                    headers=self.headers,
                    json=body,
                    timeout=30.0,
                )
                resp.raise_for_status()
                task_id = resp.json().get("task_id", "")
                if task_id:
                    self._user_task_ids.add(task_id)
                    self._turn_start_times[task_id] = time.monotonic()
                    self._pending_user_messages[task_id] = text
                    self._current_turn_activity[task_id] = {
                        "task_id": task_id,
                        "prompt": text,
                        "domain": "",
                        "is_consequential": False,
                        "agents": [],
                        "model": self._model,
                        "thought_duration": 0.0,
                        "thought_tokens": 0,
                        "tools": [],
                        "verifications": [],
                        "output": "",
                        "status": "running",
                        "error": "",
                        "phase": "submitted",
                        "thoughts": "",
                        "details_expanded": self._details_expanded,
                        "thoughts_expanded": self._reasoning_visible,
                        "plan_steps": [],
                        "dod_results": [],
                        "active_phase": "",
                        "interaction": None,
                    }
                    self._session_tokens += max(1, len(text) // 4)
                    self._render_active_turns()
                    self._render_status_bar()
        except httpx.ConnectError:
            self._set_status("")
            self._log("  [red]◆[/red]  [red]cannot reach north server[/red]")
        except Exception as exc:
            self._set_status("")
            self._log(f"  [red]◆[/red]  [red]error: {exc}[/red]")

    # ── slash commands ────────────────────────────────────────────────────────

    async def _handle_slash(self, text: str) -> None:
        cmd = text.split()[0].lower()
        if cmd in ("/quit", "/exit"):
            self._log("  [dim]goodbye[/dim]")
            self.exit()
        elif cmd == "/clear":
            self.query_one("#log", RichLog).clear()
            self._draw_banner()
        elif cmd == "/cost":
            self._log(
                f"  [bright_black]tokens[/bright_black] {_fmt_tokens(self._session_tokens)}  ·  "
                f"[bright_black]cost[/bright_black] ${self._session_cost:.4f}  ·  "
                f"[bright_black]compactions[/bright_black] {self._compactions}"
            )
        elif cmd == "/power":
            parts = text.split()
            val = parts[1] if len(parts) > 1 else None
            await self._set_dial("/orchestrator/settings", "power", val, ok="  [bright_black]power[/bright_black] ")
        elif cmd == "/autonomy":
            parts = text.split()
            val = parts[1] if len(parts) > 1 else None
            await self._set_dial(
                "/orchestrator/settings", "autonomy", val, ok="  [bright_black]autonomy[/bright_black] "
            )
        elif cmd == "/agents":
            agents = await self._fetch_agents()
            self._log("  [bright_black]agents[/bright_black]  " + (", ".join(agents) or "none"))
        elif cmd == "/queue":
            try:
                async with self._http() as c:
                    r_tasks = await c.get(f"{self.base_url}/orchestrator/tasks", headers=self.headers, timeout=5.0)
                    r_jobs = await c.get(
                        f"{self.base_url}/orchestrator/jobs",
                        params={"status": "pending"},
                        headers=self.headers,
                        timeout=5.0,
                    )
                    tasks = r_tasks.json() if r_tasks.status_code == 200 else []
                    jobs = r_jobs.json() if r_jobs.status_code == 200 else []
                    self._log("  [cyan]◆[/cyan]  [white]active tasks & queued jobs[/white]")
                    if not tasks and not jobs:
                        self._log("    [bright_black]No active tasks or queued jobs in flight.[/bright_black]")
                    else:
                        for t in tasks:
                            tid = t.get("task_id", "")
                            p = t.get("prompt", "")[:60]
                            self._log(f"    [green]active task[/green]  [bright_black]{tid}[/bright_black]  {p}")
                        for j in jobs:
                            jid = j.get("job_id", "")
                            p = j.get("task", "")[:60]
                            self._log(f"    [yellow]queued job[/yellow]   [bright_black]{jid}[/bright_black]  {p}")
                        self._log(
                            "    [bright_black]Type '/cancel <id>' to cancel a task/job, "
                            "or '/cancel all' to cancel everything.[/bright_black]"
                        )
            except Exception as exc:
                self._log(f"  [red]error fetching queue: {exc}[/red]")
        elif cmd.startswith("/cancel"):
            parts = text.strip().split()
            target = parts[1] if len(parts) > 1 else ""
            if not target or target.lower() in ("all", "--all"):
                try:
                    async with self._http() as c:
                        r = await c.post(
                            f"{self.base_url}/orchestrator/cancel-all", headers=self.headers, timeout=10.0
                        )
                        if r.status_code == 200:
                            data = r.json()
                            c_tasks = data.get("tasks_cancelled", 0)
                            c_jobs = data.get("jobs_cancelled", 0)
                            self._log(
                                f"  [yellow]✓[/yellow]  [white]cancelled {c_tasks} active task(s) "
                                f"and {c_jobs} queued job(s)[/white]"
                            )
                        else:
                            self._log(f"  [red]error cancelling tasks (HTTP {r.status_code})[/red]")
                except Exception as exc:
                    self._log(f"  [red]error cancelling tasks: {exc}[/red]")
            else:
                try:
                    async with self._http() as c:
                        r = await c.post(
                            f"{self.base_url}/orchestrator/cancel/{target}", headers=self.headers, timeout=10.0
                        )
                        if r.status_code == 200:
                            data = r.json()
                            c_type = data.get("cancelled", "task")
                            c_id = data.get("id", target)
                            self._log(f"  [yellow]✓[/yellow]  [white]cancelled {c_type} {c_id}[/white]")
                        else:
                            self._log(f"  [red]task or job '{target}' not found or already completed[/red]")
                except Exception as exc:
                    self._log(f"  [red]error cancelling {target}: {exc}[/red]")
        elif cmd == "/jobs":
            try:
                async with self._http() as c:
                    r = await c.get(f"{self.base_url}/orchestrator/jobs", headers=self.headers, timeout=5.0)
                    jobs = r.json() if r.status_code == 200 else []
                    self._log_rich(_format_jobs_table(jobs))
            except Exception as exc:
                self._log(f"  [red]error fetching jobs: {exc}[/red]")
        elif cmd == "/context":
            parts = text.split()
            target_doc = None
            if len(parts) == 2 and parts[1] not in ("show", "edit"):
                target_doc = parts[1].removesuffix(".md")
            elif len(parts) >= 3 and parts[1] == "show":
                target_doc = parts[2].removesuffix(".md")

            if target_doc:
                try:
                    async with self._http() as c:
                        r = await c.get(
                            f"{self.base_url}/orchestrator/context/{target_doc}",
                            headers=self.headers,
                            timeout=5.0,
                        )
                        if r.status_code == 200:
                            content = r.text.strip()
                            self._log(f"  [cyan]◆[/cyan]  [white]{target_doc}.md[/white]")
                            if content:
                                self._log_rich(RichPadding(RichMarkdown(content), (0, 0, 0, 4)))
                            else:
                                self._log("    [dim](empty document)[/dim]")
                        else:
                            self._log(f"  [yellow]context document '{target_doc}' not found[/yellow]")
                except Exception as exc:
                    self._log(f"  [red]error fetching context '{target_doc}': {exc}[/red]")
            else:
                try:
                    async with self._http() as c:
                        docs = ["user", "judgement_rules", "north_stars", "soul"]
                        self._log("  [cyan]◆[/cyan]  [white]context documents[/white]")
                        for doc in docs:
                            r = await c.get(
                                f"{self.base_url}/orchestrator/context/{doc}",
                                headers=self.headers,
                                timeout=5.0,
                            )
                            if r.status_code == 200 and r.text.strip():
                                self._log(
                                    f"    [white]{doc}.md[/white] [bright_black]({len(r.text)} chars)[/bright_black]"
                                )
                        self._log(
                            "    [bright_black]Type '/context <doc>' or '/context show <doc>' to inspect[/bright_black]"
                        )
                except Exception as exc:
                    self._log(f"  [red]error fetching context: {exc}[/red]")
        elif cmd == "/models":
            try:
                async with self._http() as c:
                    r = await c.get(
                        f"{self.base_url}/orchestrator/inference/models",
                        headers=self.headers,
                        timeout=5.0,
                    )
                    pools = r.json() if r.status_code == 200 else {}
                    self._log("  [cyan]◆[/cyan]  [white]discovered models per pool[/white]")
                    for pool_name, pool_data in pools.items():
                        models = pool_data.get("models", [])
                        if models:
                            sample = ", ".join(m["id"] for m in models[:4])
                            more = f" (+{len(models) - 4} more)" if len(models) > 4 else ""
                            self._log(
                                f"    [white]{pool_name}[/white] "
                                f"[bright_black]({len(models)}): {sample}{more}[/bright_black]"
                            )
            except Exception as exc:
                self._log(f"  [red]error fetching models: {exc}[/red]")
        elif cmd == "/limits":
            from config.settings import settings
            from inference.rate_limit_status import format_status_table

            self._log_rich(format_status_table(settings.north_home / "rate_limit_status.json"))
        elif cmd == "/details":
            self.action_toggle_activity_details()
            state = "expanded" if self._details_expanded else "collapsed"
            self._log(f"  [bright_black]Execution traces {state} (Ctrl+O)[/bright_black]")
        elif cmd == "/thoughts":
            self.action_toggle_reasoning()
            state = "expanded" if self._reasoning_visible else "collapsed"
            self._log(f"  [bright_black]Thoughts for the current message {state} (Ctrl+T)[/bright_black]")
        elif cmd in ("/tools", "/inspect"):
            self.action_inspect_tools()
        elif cmd == "/plan":
            self.action_inspect_plan()
        elif cmd.startswith("/steer"):
            feedback = text.partition(" ")[2].strip()
            if feedback:
                await self._send_steer(feedback)
            else:
                self.push_screen(SteerModal(), self._handle_steer_submit)
        elif cmd == "/help":
            self._log_rich(_format_help_table(_SLASH_COMMANDS))
        else:
            self._log(f"  [bright_black]unknown command: {cmd} - try /help[/bright_black]")

    # ── interactive cockpit actions ──────────────────────────────────────────

    def action_toggle_activity_details(self) -> None:
        """Toggle activity only for the active/latest message."""
        turn = self._focused_turn()
        if turn is None:
            return
        turn["details_expanded"] = not bool(turn.get("details_expanded"))
        self._details_expanded = bool(turn["details_expanded"])  # compatibility for integrations
        self._refresh_hint()
        if turn in self._current_turn_activity.values():
            self._render_active_turns()
        else:
            self._redraw_chat_history()

    def _redraw_chat_history(self) -> None:
        """Redraw conversation history switching between compact and detailed traces."""
        with contextlib.suppress(Exception):
            log = self.query_one("#log", RichLog)
            log.clear()
            self._write_banner_lines(log)
            for turn in self._turns:
                for renderable in self._turn_renderables(turn, active=False):
                    log.write(renderable)  # type: ignore[arg-type]
                self._write_rule()

            # Preserve any in-flight active turn currently running
            log.scroll_end(animate=False)
            self._render_active_turns()



    def action_toggle_reasoning(self) -> None:
        """Toggle thoughts inside the active/latest message."""
        turn = self._focused_turn()
        if turn is None:
            self._reasoning_visible = not self._reasoning_visible
            self._refresh_hint()
            return
        turn["thoughts_expanded"] = not bool(turn.get("thoughts_expanded"))
        self._reasoning_visible = bool(turn["thoughts_expanded"])  # compatibility for integrations
        self._refresh_hint()
        if turn in self._current_turn_activity.values():
            self._render_active_turns()
        else:
            self._redraw_chat_history()

    def action_inspect_tools(self) -> None:
        """Open tools belonging to the active/latest message."""
        turn = self._focused_turn()
        tools = list(turn.get("tools") or []) if turn is not None else []
        self.push_screen(ToolInspectorModal(tools))

    def action_inspect_plan(self) -> None:
        """Open the plan belonging to the active/latest message."""
        turn = self._focused_turn()
        steps = list(turn.get("plan_steps") or []) if turn is not None else list(self._plan_steps)
        dod = list(turn.get("dod_results") or []) if turn is not None else list(self._dod_results)
        phase = str(turn.get("active_phase") or "") if turn is not None else self._active_phase
        self.push_screen(PlanCockpitModal(steps, dod, phase))

    def action_action_menu(self) -> None:
        """Open in-flight action and steer menu when tasks are active."""
        if len(self.screen_stack) > 1:
            return
        if not self._user_task_ids and not self._streaming_active:
            return
        self.push_screen(ActionMenuModal(), self._handle_action_menu_result)

    def _handle_action_menu_result(self, result: str | None) -> None:
        if not result:
            return
        if result == "steer":
            self.push_screen(SteerModal(), self._handle_steer_submit)
        elif result == "inspect_tools":
            self.action_inspect_tools()
        elif result == "view_thoughts":
            self.action_toggle_reasoning()
        elif result == "view_plan":
            self.action_inspect_plan()
        elif result == "cancel":
            self.run_worker(self._cancel_active_tasks())

    def _handle_steer_submit(self, feedback: str | None) -> None:
        if feedback:
            self.run_worker(self._send_steer(feedback))

    async def _send_steer(self, feedback: str) -> None:
        try:
            async with self._http() as c:
                r = await c.post(
                    f"{self.base_url}/orchestrator/steer",
                    headers=self.headers,
                    json={"instruction": feedback},
                    timeout=5.0,
                )
                if r.status_code == 200:
                    self._log(f"  [magenta]🎯 steering sent:[/magenta] [white]{feedback}[/white]")
                else:
                    self._log(f"  [red]failed to send steer directive (HTTP {r.status_code})[/red]")
        except Exception as exc:
            self._log(f"  [red]error sending steer directive: {exc}[/red]")

    async def _cancel_active_tasks(self) -> None:
        try:
            async with self._http() as c:
                await c.post(f"{self.base_url}/orchestrator/cancel-all", headers=self.headers, timeout=10.0)
                self._log("  [yellow]✓ cancelled in-flight tasks[/yellow]")
        except Exception as exc:
            self._log(f"  [red]error cancelling tasks: {exc}[/red]")

    # ── clipboard copy (Ctrl+Y) ──────────────────────────────────────────────

    def action_copy_last_response(self) -> None:
        """Copy the last assistant response to the clipboard."""
        if not self._last_assistant_response:
            self._log("  [dim]no previous response to copy[/dim]")
            return
        ok = _copy_to_clipboard(self._last_assistant_response)
        if ok:
            self._log("  [bright_black]✓ copied response to clipboard[/bright_black]")
        else:
            self._log("  [dim]failed to copy to clipboard[/dim]")

    # ── history navigation ────────────────────────────────────────────────────

    def action_history_prev(self) -> None:
        if not self._input_history:
            return
        prompt = self.query_one("#prompt", Input)
        if self._history_index == -1:
            self._current_input = prompt.value
            self._history_index = len(self._input_history) - 1
        elif self._history_index > 0:
            self._history_index -= 1
        prompt.value = self._input_history[self._history_index]
        prompt.cursor_position = len(prompt.value)

    def action_history_next(self) -> None:
        if self._history_index == -1:
            return
        prompt = self.query_one("#prompt", Input)
        if self._history_index < len(self._input_history) - 1:
            self._history_index += 1
            prompt.value = self._input_history[self._history_index]
        else:
            self._history_index = -1
            prompt.value = self._current_input
        prompt.cursor_position = len(prompt.value)

    # ── interrupt / exit ──────────────────────────────────────────────────────

    def action_interrupt(self) -> None:
        """Single Ctrl+C cancels in-flight work (and lets the user redirect);
        a second Ctrl+C within 2s force-exits."""
        now = time.monotonic()
        if now - self._last_interrupt < 2.0:
            self.exit()
            return
        self._last_interrupt = now
        if self._user_task_ids:
            self.run_worker(self._cancel_active(), exclusive=False)
            self._log("  [dim]interrupted - press ctrl+c again to exit[/dim]")
        else:
            self._log("  [dim]press ctrl+c again to exit[/dim]")

    async def _cancel_active(self) -> None:
        """Ask the server to cancel every task this session started."""
        for task_id in list(self._user_task_ids):
            try:
                async with self._http() as c:
                    await c.delete(
                        f"{self.base_url}/orchestrator/task/{task_id}",
                        headers=self.headers,
                        timeout=10.0,
                    )
            except Exception:
                pass

    # ── external editor (Ctrl+G) ──────────────────────────────────────────────

    def action_edit_in_editor(self) -> None:
        """Open the current prompt buffer in $EDITOR; the saved text replaces it."""
        prompt = self.query_one("#prompt", Input)
        edited = self._run_external_editor(prompt.value)
        if edited is not None:
            prompt.value = edited.replace("\n", " ").strip()
            prompt.cursor_position = len(prompt.value)

    def _run_external_editor(self, initial: str) -> str | None:
        import subprocess
        import tempfile

        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".md", prefix="north-", delete=False, encoding="utf-8"
            ) as tf:
                tf.write(initial)
                path = tf.name
            with self.suspend():
                subprocess.run([*editor.split(), path], check=False)
            text = Path(path).read_text(encoding="utf-8")
            Path(path).unlink(missing_ok=True)
            return text
        except Exception:
            return None

    # ── push-to-talk dictation (Ctrl+D) ────────────────────────────────────

    def action_toggle_dictation(self) -> None:
        """Toggle push-to-talk recording. First press starts; second stops and transcribes."""
        if self._recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self) -> None:
        """Begin capturing audio from the microphone."""
        try:
            import numpy as np  # noqa: F401
            import sounddevice as sd
        except ImportError:
            self._log("  [red]◆[/red]  [red]Missing dependency: sounddevice. Install with: uv add sounddevice[/red]")
            return

        self._audio_frames = []
        self._recording = True

        def _callback(indata, frame_count, time_info, status):  # type: ignore[no-untyped-def]
            if self._recording:
                self._audio_frames.append(indata.copy())

        try:
            self._audio_stream = sd.InputStream(
                samplerate=self._sample_rate,
                channels=1,
                dtype="int16",
                callback=_callback,
            )
            self._audio_stream.start()  # type: ignore[union-attr]
        except Exception as exc:
            self._recording = False
            self._log(f"  [red]◆[/red]  [red]Microphone error: {exc}[/red]")
            return

        self._log("  [red]● Recording…[/red]  [bright_black](press ctrl+d to stop)[/bright_black]")
        prompt = self.query_one("#prompt", Input)
        prompt.value = ""
        prompt.placeholder = "🎙 Recording… press Ctrl+D to stop"

    def _stop_recording(self) -> None:
        """Stop recording, transcribe, and submit the result as a prompt."""
        self._recording = False
        if self._audio_stream is not None:
            try:
                self._audio_stream.stop()  # type: ignore[union-attr]
                self._audio_stream.close()  # type: ignore[union-attr]
            except Exception:
                pass
            self._audio_stream = None

        prompt = self.query_one("#prompt", Input)
        prompt.placeholder = ""

        if not self._audio_frames:
            self._log("  [dim](nothing recorded)[/dim]")
            return

        self._log("  [yellow]■ Transcribing…[/yellow]")
        self.run_worker(self._transcribe_and_submit(), exclusive=False)

    async def _transcribe_and_submit(self) -> None:
        """Encode captured audio to WAV, send to the server for transcription,
        and submit the resulting text as a chat prompt."""
        import io
        import wave

        import numpy as np

        frames = self._audio_frames
        self._audio_frames = []

        audio_np = np.concatenate(frames, axis=0)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(self._sample_rate)
            wf.writeframes(audio_np.tobytes())
        wav_bytes = buf.getvalue()

        # Transcribe via the server endpoint
        try:
            async with self._http() as c:
                resp = await c.post(
                    f"{self.base_url}/orchestrator/transcribe",
                    content=wav_bytes,
                    headers={**self.headers, "Content-Type": "audio/wav"},
                    timeout=60.0,
                )
                resp.raise_for_status()
                text = resp.json().get("text", "").strip()
        except Exception as exc:
            self._log(f"  [red]◆[/red]  [red]Transcription error: {exc}[/red]")
            return

        if not text:
            self._log("  [dim](empty transcript)[/dim]")
            return

        self._log(f"  [cyan]✎ {text}[/cyan]")

        # Submit as a prompt (reuse the same submission logic as typed input)
        if not self._input_history or self._input_history[-1] != text:
            self._input_history.append(text)
        self._history_index = -1
        self._current_input = ""

        self._log(f"  [bright_black]>[/bright_black]  {text}")

        body: dict = {"prompt": text}
        if self.workspace:
            body["workspace"] = self.workspace
        if self._conversation_history:
            turns: list[str] = []
            for turn in self._conversation_history:
                parts = [f"User: {turn['user']}"]
                if turn.get("tools"):
                    summaries = [
                        f"{e['tool']}({e['params']}) → {e['result']}"
                        if e.get("params")
                        else f"{e['tool']} → {e['result']}"
                        for e in turn["tools"]
                        if e.get("result")
                    ]
                    if summaries:
                        parts.append("[actions: " + "; ".join(summaries) + "]")
                parts.append(f"north: {turn['north']}")
                turns.append("\n".join(parts))
            body["context"] = "## Recent conversation\n" + "\n\n".join(turns)

        self._set_status("…")
        try:
            async with self._http() as c:
                resp = await c.post(
                    f"{self.base_url}/orchestrator/task",
                    headers=self.headers,
                    json=body,
                    timeout=30.0,
                )
                resp.raise_for_status()
                task_id = resp.json().get("task_id", "")
                if task_id:
                    self._user_task_ids.add(task_id)
                    self._pending_user_messages[task_id] = text
                    self._session_tokens += max(1, len(text) // 4)
                    self._render_status_bar()
        except httpx.ConnectError:
            self._set_status("")
            self._log("  [red]◆[/red]  [red]cannot reach north server[/red]")
        except Exception as exc:
            self._set_status("")
            self._log(f"  [red]◆[/red]  [red]error: {exc}[/red]")


async def run(
    base_url: str,
    headers: dict,
    workspace: str | None = None,
    yolo: bool = False,
) -> None:
    """Launch the TUI. Blocks until the user exits."""
    app = NorthApp(base_url=base_url, headers=headers, workspace=workspace, yolo=yolo)
    await app.run_async()
