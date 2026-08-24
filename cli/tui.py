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
from rich.markdown import Markdown as RichMarkdown
from rich.padding import Padding as RichPadding
from rich.syntax import Syntax as RichSyntax
from rich.text import Text as RichText
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.suggester import Suggester
from textual.widgets import Button, Input, Label, ListItem, ListView, RichLog, Static

from cli.constants import (
    _REASONING_PREVIEW_CHARS,
    _SLASH_COMMANDS,
    _SPIN,
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
    _reconstruct_task_output,
    _short_model,
    _strip_markup,
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
            yield Static("  [bold #58a6ff]🔍 Tool & Diff Inspector[/bold #58a6ff] [bright_black](Esc/q to close)[/bright_black]", id="tool-modal-title")
            with Horizontal(id="tool-modal-body"):
                yield ListView(id="tool-modal-list")
                with VerticalScroll(id="tool-modal-detail"):
                    yield Static("Select a tool call from the left to inspect its parameters, output, and diffs.", id="tool-detail-content")
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
            list_view.append(ListItem(Label(f" {icon} [bold white]{tool}[/bold white][bright_black]{dur_str}[/bright_black]"), name=str(len(self._history) - 1 - i)))
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
            f"[bold {header_color}]▶ Tool: {tool}[/bold {header_color}]  [bright_black]({status_text} · {dur_str})[/bright_black]",
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
            yield Static("  [bold #3fb950]📋 Execution Plan & DoD Cockpit[/bold #3fb950] [bright_black](Esc/q to close)[/bright_black]", id="plan-modal-title")
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
            yield Static("  [bold #d29922]⚡ Task Action & Steering Menu[/bold #d29922]", id="action-menu-title")
            with Vertical(id="action-menu-options"):
                yield Button(" [1] 🎯 Steer Agent (/steer)", id="btn-steer", variant="primary")
                yield Button(" [2] 🔍 Inspect Tool Calls & Diffs (Ctrl+I)", id="btn-tools")
                yield Button(" [3] 🧠 View Full Chain of Thought (Ctrl+T)", id="btn-thoughts")
                yield Button(" [4] 📋 View Plan & DoD Status (Ctrl+P)", id="btn-plan")
                yield Button(" [5] 🛑 Cancel Task (/cancel)", id="btn-cancel", variant="error")
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
    #   #log           - scrollable chat history (top-anchored)  (1fr)
    #   #streaming-wrap - scrollable live stream area (≤50%, hidden when idle)
    #     #streaming    - live markdown during token stream
    #   #status        - working spinner (empty when idle)        (1 row)
    #   #input-row     - ╭ >  [                       ] ╮ box     (3 rows)
    #   #hint          - dim shortcuts (strategy · history · …)   (1 row)

    CSS = """
    Screen {
        layout: vertical;
        background: $background;
    }

    /* ── chat log ─────────────────────────────────────────── */

    #log {
        width: 100%;
        height: 1fr;
        border: none;
        padding: 0;
        background: $background;
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

    /* ── live reasoning drawer (Ctrl+T) ───────────────────── */

    #reasoning-wrap {
        width: 100%;
        height: auto;
        max-height: 40%;
        display: none;
        overflow-y: auto;
        overflow-x: hidden;
        border-top: solid #1f6feb;
        border-bottom: solid #30363d;
        background: #0d1117;
        padding: 0 1;
        scrollbar-size: 1 1;
        scrollbar-background: #0d1117;
        scrollbar-color: #30363d;
    }

    #reasoning-header {
        width: 100%;
        height: 1;
        color: #58a6ff;
        background: #0d1117;
        text-style: bold;
        padding: 0;
    }

    #reasoning {
        width: 100%;
        height: auto;
        padding: 0 0 0 2;
        background: #0d1117;
        color: #8b949e;
    }

    /* ── live streaming area ──────────────────────────────── */

    /* Scrollable wrapper: caps the live area at half the screen and lets the
       user scroll through output longer than that while it streams. The inner
       Static sizes to its content; this container provides the scrollbar. */
    #streaming-wrap {
        width: 100%;
        height: auto;
        max-height: 50%;
        display: none;
        overflow-y: auto;
        overflow-x: hidden;
        background: $background;
        scrollbar-size: 1 1;
        scrollbar-background: $background;
        scrollbar-background-hover: $background;
        scrollbar-background-active: $background;
        scrollbar-color: $background;
        scrollbar-color-hover: $background;
        scrollbar-color-active: $background;
        scrollbar-corner-color: $background;
    }

    /* The live stream renders the growing buffer through the *same* rich
       Markdown engine used for the finalized message in #log, so the view does
       not re-flow or change chrome when the stream ends (see _finish_streaming). */
    #streaming {
        width: 100%;
        height: auto;
        padding: 0 0 0 4;
        background: $background;
        color: $text;
    }

    /* ── footer: status · top-sep · input · bot-sep · pad ── */

    /* working-spinner line, just above the input box (empty when idle) */
    #status {
        width: 100%;
        height: 1;
        background: $background;
        color: $text-muted;
        padding: 0 0 0 2;
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
    #reasoning-wrap:focus,
    #reasoning-wrap:focus-within,
    #reasoning-wrap:hover,
    #streaming-wrap:focus,
    #streaming-wrap:focus-within,
    #streaming-wrap:hover,
    #streaming:focus,
    #streaming:focus-within,
    #status:focus,
    #status:hover,
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
        self._reasoning_start_times: dict[str, float] = {}
        self._recent_thoughts: deque[dict] = deque(maxlen=20)
        self._tool_history: deque[dict] = deque(maxlen=50)
        self._plan_steps: list[dict] = []
        self._dod_results: list[dict] = []
        self._active_phase: str = ""
        self._streaming_active: set[str] = set()
        self._last_assistant_response: str = ""
        self._stream_start_times: dict[str, float] = {}
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
        self._conversation_history: deque[dict] = deque(maxlen=5)
        self._pending_user_messages: dict[str, str] = {}
        self._task_tool_activity: dict[str, list[dict]] = {}

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
            "conductor_fix_round": self._on_conductor_fix_round,
            "auto_verify_started": self._on_auto_verify_started,
            "auto_verify": self._on_auto_verify,
            "dod_evaluated": self._on_dod_evaluated,
            "stream_reset": self._on_stream_reset,
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
        yield RichLog(id="log", highlight=False, markup=True, wrap=True)
        with VerticalScroll(id="reasoning-wrap"):
            yield Static("", id="reasoning-header")
            yield Static("", id="reasoning")
        with VerticalScroll(id="streaming-wrap"):
            yield Static("", id="streaming")
        yield Static("", id="status")
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
        self._render_status_bar()
        self._set_status("")

        self.set_interval(0.08, self._tick)
        self.run_worker(self._listen(), exclusive=False)
        self.query_one("#prompt", Input).focus()
        # The chat log and live-stream panes are read-only: keep them out of the
        # focus chain so Tab (or a click) can never move focus off the input and
        # strand the user with keystrokes that go nowhere. Mouse-wheel scrolling
        # still works without focus.
        self.query_one("#log", RichLog).can_focus = False
        self.query_one("#reasoning-wrap", VerticalScroll).can_focus = False
        self.query_one("#streaming-wrap", VerticalScroll).can_focus = False
        # Defer so the log's width is known before drawing the banner rule.
        self.call_after_refresh(self._draw_banner)

    def _draw_banner(self) -> None:
        # Top-anchored: the banner is the first thing in the log; chat flows
        # downward beneath it and the input stays pinned at the bottom. Agent
        # discovery is async, so the banner is composed in a worker.
        self.run_worker(self._draw_banner_async(), exclusive=False)

    async def _draw_banner_async(self) -> None:
        log = self.query_one("#log", RichLog)
        backend = f"textual · {os.environ.get('TERM', 'unknown')}"
        cwd = self.workspace or os.getcwd()
        home = str(Path.home())
        if cwd.startswith(home):
            cwd = "~" + cwd[len(home) :]

        toolsets = await self._fetch_agents()

        log.write("")
        log.write("  [bold white]north[/bold white]  [bright_black]personal operating system[/bright_black]")
        log.write("")
        log.write(f"  [bright_black]model[/bright_black]     {_short_model(self._model) if self._model else 'auto'}")
        log.write(f"  [bright_black]backend[/bright_black]   {backend}")
        log.write(f"  [bright_black]cwd[/bright_black]       {cwd}")
        log.write(f"  [bright_black]strategy[/bright_black]  {self._strategy}")
        if toolsets:
            shown = ", ".join(toolsets[:10]) + ("…" if len(toolsets) > 10 else "")
            log.write(f"  [bright_black]toolsets[/bright_black]  {shown}")
        if self.yolo:
            log.write("  [#f85149]⚠ YOLO[/#f85149]     [bright_black]auto-approve enabled[/bright_black]")
        log.write("")
        self._write_rule()

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
        thoughts_label = "ctrl+t hide thoughts" if self._reasoning_visible else "ctrl+t thoughts"
        hint = (
            f"  {self._strategy}  ·  {thoughts_label}  ·  ctrl+i tools  ·  ctrl+p plan"
            "  ·  esc menu  ·  /commands  ·  ctrl+y copy  ·  ctrl+d dictate"
        )
        self.query_one("#hint", Static).update(f"[bright_black]{hint}[/bright_black]")

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
        if self._status_text:
            f = _SPIN[self._spin_frame % len(_SPIN)]
            with contextlib.suppress(NoMatches):
                self.query_one("#status", Static).update(f"[bright_black]  {f}  {self._status_text}[/bright_black]")
        # Refresh the bar roughly once a second so elapsed time ticks live
        # without redrawing on every 80ms animation frame.
        if self._spin_frame % 12 == 0:
            self._render_status_bar()

    def _set_status(self, text: str) -> None:
        self._status_text = text
        if not text:
            self.query_one("#status", Static).update("")
        else:
            f = _SPIN[self._spin_frame % len(_SPIN)]
            self.query_one("#status", Static).update(f"[bright_black]  {f}  {text}[/bright_black]")

    def _log(self, markup: str) -> None:
        log = self.query_one("#log", RichLog)
        log.write(markup)
        log.scroll_end(animate=False)

    def _log_rich(self, renderable: object) -> None:
        log = self.query_one("#log", RichLog)
        log.write(renderable)  # type: ignore[arg-type]
        log.scroll_end(animate=False)

    # ── streaming widget ─────────────────────────────────────────────────────

    def _start_streaming(self) -> None:
        self.query_one("#streaming-wrap", VerticalScroll).display = True
        self.query_one("#streaming", Static).update("")

    def _update_streaming(self, task_id: str) -> None:
        # Render the growing buffer through the same rich Markdown engine used for
        # the finalized message, so the view does not change when the stream ends.
        # (The #streaming CSS supplies the left indent, matching the RichPadding
        # applied to the final message in _finish_streaming.)
        buffer = self._token_buffer.get(task_id, "")
        self.query_one("#streaming", Static).update(RichMarkdown(buffer) if buffer else "")
        # Keep the newest tokens in view as they stream; the user can still
        # scroll up through the live area (it's a VerticalScroll now).
        self.query_one("#streaming-wrap", VerticalScroll).scroll_end(animate=False)

    def _finish_streaming(self, task_id: str, final_output: str) -> None:
        self.query_one("#streaming-wrap", VerticalScroll).display = False
        self.query_one("#streaming", Static).update("")
        if final_output:
            # Identical renderer to the live stream (rich Markdown), so the message
            # simply moves from the capped live area into the scrollback with no
            # re-flow or chrome change - tables, lists, and inline styling included.
            # (Do NOT flatten with _to_prose here: it has no table support and would
            # fork rendering from the streaming path, the original "table un-renders
            # when the stream finishes" bug.)
            self._log_rich(RichPadding(RichMarkdown(final_output), (0, 0, 0, 4)))

    # ── SSE event handler ────────────────────────────────────────────────────

    async def _handle_event(self, event: str, data: dict) -> None:
        task_id = data.get("task_id", "")
        if task_id and task_id not in self._user_task_ids:
            return
        handler = self._event_handlers.get(event)
        if handler is not None:
            await handler(task_id, data)

    async def _on_classifying(self, task_id: str, data: dict) -> None:
        self._set_status("classifying…")
        self._log("  [bright_black]→[/bright_black]  [dim]classifying…[/dim]")

    async def _on_classified(self, task_id: str, data: dict) -> None:
        domain = data.get("domain", "")
        flag = " [dim](complex)[/dim]" if data.get("is_consequential") else " [dim](direct)[/dim]"
        self._set_status(f"routing → {domain}…")
        label = f"classified: [cyan]{domain}[/cyan]{flag}" if domain else "classified"
        self._log(f"  [dim green]✓[/dim green]  {label}")

    async def _on_routed(self, task_id: str, data: dict) -> None:
        agents = data.get("agents") or []
        self._set_status(f"running {', '.join(agents) or 'general'}…")
        if agents:
            self._log(f"  [dim green]✓[/dim green]  plan ready: [cyan]{', '.join(agents)}[/cyan]")

    async def _on_north_star_checking(self, task_id: str, data: dict) -> None:
        self._set_status("checking goals…")

    async def _on_north_star_noop(self, task_id: str, data: dict) -> None:
        """north_star_aligned / north_star_check_skipped - no UI change."""

    async def _on_north_star_conflict(self, task_id: str, data: dict) -> None:
        tension = (data.get("tension") or "")[:200]
        self._set_status("")
        self._log("  [yellow]◆[/yellow]  [yellow]goal conflict[/yellow]")
        self._log_rich(RichText("    " + tension, style="white"))

    async def _on_model(self, task_id: str, data: dict) -> None:
        self._model = data.get("model", "")
        self._refresh_hint()
        self._render_status_bar()

    async def _on_compaction(self, task_id: str, data: dict) -> None:
        self._compactions += 1
        self._render_status_bar()

    async def _on_agent_started(self, task_id: str, data: dict) -> None:
        agent = data.get("agent", "")
        agents = data.get("agents") or []
        label = ", ".join(agents) if agents else agent or "general"
        self._set_status(f"running {label}…")
        model_str = f" [dim]on [cyan]{self._model}[/cyan][/dim]" if self._model else ""
        self._log(f"  [bright_black]◎[/bright_black]  [cyan]{label}[/cyan] agent running{model_str}…")

    async def _on_tool_called(self, task_id: str, data: dict) -> None:
        tool = data.get("tool", "")
        params = data.get("params") or {}
        params_str = _fmt_params(params)
        suffix = f"[bright_black]({params_str})[/bright_black]" if params_str else ""
        self._log(f"    [bright_black]→[/bright_black]  [cyan]{tool}[/cyan]{suffix}")
        self._set_status(f"{tool}…")
        entry = {
            "task_id": task_id,
            "tool": tool,
            "params": params,
            "params_str": params_str,
            "result": None,
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

    async def _on_tool_result(self, task_id: str, data: dict) -> None:
        tool = data.get("tool", "")
        success = data.get("success", True)
        self._log(f"    [dim green]✓  {tool}[/dim green]" if success else f"    [dim red]✗  {tool}[/dim red]")
        formatted = data.get("formatted", "")
        error = data.get("error", "")
        result = (
            formatted[:200].replace("\n", " ")
            if formatted
            else f"failed: {error[:100]}"
            if error
            else ("ok" if success else "failed")
        )
        now = time.monotonic()
        for item in reversed(self._tool_history):
            if item.get("tool") == tool and item.get("result") is None:
                item["result"] = result
                item["formatted"] = formatted
                item["error"] = error
                item["success"] = success
                if item.get("start_time"):
                    item["duration"] = now - item["start_time"]
                break
        if task_id:
            for entry in self._task_tool_activity.get(task_id, []):
                if entry["tool"] == tool and entry["result"] is None:
                    entry["result"] = result
                    break
        self._set_status("thinking…")

    async def _on_token(self, task_id: str, data: dict) -> None:
        text = data.get("text", "")
        if not text:
            return
        now = time.monotonic()
        if task_id not in self._stream_start_times:
            self._stream_start_times[task_id] = now
            self._stream_token_counts[task_id] = 0
        self._stream_token_counts[task_id] += max(1, len(text) // 4)
        elapsed = now - self._stream_start_times[task_id]
        if elapsed > 0.2:
            self._streaming_tok_per_sec = self._stream_token_counts[task_id] / elapsed

        self._token_buffer[task_id] = self._token_buffer.get(task_id, "") + text
        # Rough running token estimate (≈4 chars/token) for the status bar.
        self._session_tokens += max(1, len(text) // 4)
        if task_id not in self._streaming_active:
            self._streaming_active.add(task_id)
            thoughts = self._reasoning_buffer.get(task_id, "")
            if thoughts:
                toks = max(1, len(thoughts) // 4)
                dur = now - self._reasoning_start_times.get(task_id, now)
                self._recent_thoughts.append({
                    "task_id": task_id,
                    "thoughts": thoughts,
                    "tokens": toks,
                    "duration": dur,
                })
                self._log(f"  [dim cyan]🧠 Thought for {dur:.1f}s ({toks} tokens · Ctrl+T to view)[/dim cyan]")
            self._set_status("")
            self._log("  [cyan]◆[/cyan]  [white]north[/white]")
            self._start_streaming()
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
        preview = " ".join(buf.split())[-_REASONING_PREVIEW_CHARS:]
        self._set_status(f"thinking… {preview}")

        toks = max(1, len(buf) // 4)
        elapsed = time.monotonic() - self._reasoning_start_times[task_id]
        with contextlib.suppress(Exception):
            self.query_one("#reasoning-header", Static).update(
                f"  [bold #58a6ff]🧠 Thinking…[/bold #58a6ff] [bright_black]({toks} tokens · {elapsed:.1f}s · Ctrl+T to toggle)[/bright_black]"
            )
            self.query_one("#reasoning", Static).update(buf)

    async def _on_task_synthesis(self, task_id: str, data: dict) -> None:
        self._set_status("synthesising…")

    async def _on_task_completed(self, task_id: str, data: dict) -> None:
        sys.stdout.write("\a")
        sys.stdout.flush()
        self._session_cost += float(data.get("cost_usd", 0.0) or 0.0)
        output = self._token_buffer.pop(task_id, "")
        self._reasoning_buffer.pop(task_id, None)
        self._stream_start_times.pop(task_id, None)
        self._stream_token_counts.pop(task_id, None)
        self._streaming_tok_per_sec = 0.0
        was_streaming = task_id in self._streaming_active
        self._streaming_active.discard(task_id)

        if not output:
            output = await self._fetch_ledger_output(task_id)

        if was_streaming:
            self._finish_streaming(task_id, output)
        elif output:
            self._log("  [cyan]◆[/cyan]  [white]north[/white]")
            # Same markdown path as _finish_streaming - keep the non-streamed
            # branch (output fetched whole from the ledger) rendering tables
            # and lists identically rather than flattening to prose.
            self._log_rich(RichPadding(RichMarkdown(output), (0, 0, 0, 4)))

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
        self._write_rule()

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
        if task_id in self._streaming_active:
            self._finish_streaming(task_id, "")
        self._streaming_active.discard(task_id)
        self._token_buffer.pop(task_id, None)
        self._reasoning_buffer.pop(task_id, None)
        self._stream_start_times.pop(task_id, None)
        self._stream_token_counts.pop(task_id, None)
        self._streaming_tok_per_sec = 0.0
        self._task_tool_activity.pop(task_id, None)
        error = data.get("error", "Task failed.")
        self._set_status("")
        self._user_task_ids.discard(task_id)
        self._log("  [red]◆[/red]  [red]error[/red]")
        self._log_rich(RichText("    " + error, style="red"))
        self._render_status_bar()
        self._write_rule()

    async def _on_task_cancelled(self, task_id: str, data: dict) -> None:
        if task_id in self._streaming_active:
            self._finish_streaming(task_id, "")
        self._streaming_active.discard(task_id)
        self._token_buffer.pop(task_id, None)
        self._reasoning_buffer.pop(task_id, None)
        self._stream_start_times.pop(task_id, None)
        self._stream_token_counts.pop(task_id, None)
        self._streaming_tok_per_sec = 0.0
        self._task_tool_activity.pop(task_id, None)
        self._set_status("")
        self._user_task_ids.discard(task_id)
        self._log("  [dim]cancelled[/dim]")
        self._render_status_bar()
        self._write_rule()

    async def _on_task_skipped(self, task_id: str, data: dict) -> None:
        if task_id in self._streaming_active:
            self._finish_streaming(task_id, "")
        self._streaming_active.discard(task_id)
        self._token_buffer.pop(task_id, None)
        self._reasoning_buffer.pop(task_id, None)
        self._stream_start_times.pop(task_id, None)
        self._stream_token_counts.pop(task_id, None)
        self._streaming_tok_per_sec = 0.0
        self._task_tool_activity.pop(task_id, None)
        reason = data.get("reason", "Task skipped.")
        self._set_status("")
        self._user_task_ids.discard(task_id)
        self._log("  [yellow]◆[/yellow]  [yellow]task skipped[/yellow]")
        self._log_rich(RichText("    " + reason, style="yellow"))
        self._render_status_bar()
        self._write_rule()

    async def _on_task_rejected(self, task_id: str, data: dict) -> None:
        if task_id in self._streaming_active:
            self._finish_streaming(task_id, "")
        self._streaming_active.discard(task_id)
        self._token_buffer.pop(task_id, None)
        self._reasoning_buffer.pop(task_id, None)
        self._stream_start_times.pop(task_id, None)
        self._stream_token_counts.pop(task_id, None)
        self._streaming_tok_per_sec = 0.0
        self._task_tool_activity.pop(task_id, None)
        reason = data.get("reason", "Task rejected.")
        self._set_status("")
        self._user_task_ids.discard(task_id)
        self._log("  [yellow]◆[/yellow]  [yellow]task rejected[/yellow]")
        self._log_rich(RichText("    " + reason, style="yellow"))
        self._render_status_bar()
        self._write_rule()

    async def _on_task_paused(self, task_id: str, data: dict) -> None:
        self._set_status("paused")
        self._log("  [dim]task paused[/dim]")
        self._render_status_bar()

    async def _on_waiting_for_model(self, task_id: str, data: dict) -> None:
        wait_secs = int(data.get("wait_seconds", 10))
        reason = data.get("reason", "rate limits")
        self._set_status(f"waiting for model ({reason}, retrying in {wait_secs}s)…")

    async def _on_task_queued(self, task_id: str, data: dict) -> None:
        reason = data.get("reason", "waiting for model capacity")
        retry_after = data.get("retry_after")
        time_info = f" (retrying in ~{int(retry_after)}s)" if retry_after else ""
        self._set_status("task queued (waiting for models)…")
        self._log(f"  [yellow]⏳[/yellow]  [white]task queued[/white] [bright_black]({reason}{time_info})[/bright_black]")
        if task_id:
            self._log(f"    [bright_black]Task ID: {task_id} — type '/cancel {task_id}' to cancel[/bright_black]")

    async def _on_task_resumed(self, task_id: str, data: dict) -> None:
        self._set_status("resuming task from queue…")
        self._log(f"  [cyan]↻[/cyan]  [white]resuming task[/white] [bright_black]{task_id}[/bright_black]")

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
        if self.yolo:
            # Auto-approve mode: take the first option without prompting.
            self._log(f"  [#f85149]⚠[/#f85149]  [bright_black]auto-approved: {options[0]}[/bright_black]")
            await self._submit_approval("1")
            return
        self._log("  [yellow]◆[/yellow]  [yellow]approval required[/yellow]")
        msg = data.get("message", "")
        # Check if the message contains a unified diff code block
        if "```diff" in msg:
            pre, _, rest = msg.partition("```diff\n")
            diff_body, _, post = rest.partition("```")
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

        for i, opt in enumerate(options, 1):
            self._log(f"    [bright_black][{i}][/bright_black]  {opt}")

    async def _on_design_phase(self, task_id: str, data: dict) -> None:
        step = data.get("step", "")
        self._active_phase = f"design phase: {step}"
        self._set_status(f"design phase: {step}…")
        self._log(f"  [cyan]◆[/cyan]  [white]design phase[/white]  [bright_black]{step}[/bright_black]")

    async def _on_plan_seeded(self, task_id: str, data: dict) -> None:
        tasks = data.get("tasks", 0)
        steps = data.get("steps") or [{"step_id": i + 1, "task": f"Task Step {i+1}", "agent": "coder", "status": "pending"} for i in range(tasks)]
        self._plan_steps = steps
        self._log(f"  [cyan]◆[/cyan]  [white]plan seeded[/white]  [bright_black]{tasks} task steps · Ctrl+P to inspect[/bright_black]")

    async def _on_conductor_fix_round(self, task_id: str, data: dict) -> None:
        round_num = data.get("round", 1)
        self._active_phase = f"reviewer fix round {round_num}"
        self._set_status(f"reviewer fix round {round_num}…")
        self._log(f"  [yellow]◆[/yellow]  [yellow]reviewer fix round {round_num}[/yellow]")

    async def _on_auto_verify_started(self, task_id: str, data: dict) -> None:
        cmd = data.get("command", "")
        self._set_status(f"verifying ({cmd})…")
        self._log(f"    [bright_black]→[/bright_black]  [cyan]auto-verify[/cyan] [bright_black]({cmd})[/bright_black]")

    async def _on_auto_verify(self, task_id: str, data: dict) -> None:
        cmd = data.get("command", "")
        passed = data.get("passed", False)
        if passed:
            self._log(f"    [dim green]✓  auto-verify passed ({cmd})[/dim green]")
        else:
            self._log(f"    [dim red]✗  auto-verify failed ({cmd})[/dim red]")

    async def _on_dod_evaluated(self, task_id: str, data: dict) -> None:
        passed = data.get("passed", False)
        reasons = data.get("reasons") or []
        self._dod_results.append(data)
        summary = ", ".join(reasons) if reasons else ("met" if passed else "unmet")
        style = "dim green" if passed else "dim yellow"
        icon = "✓" if passed else "!"
        self._log(f"    [{style}]{icon}  Definition of Done: {summary}[/{style}]")

    async def _on_task_steered(self, task_id: str, data: dict) -> None:
        instruction = data.get("instruction", "")
        self._log(f"  [magenta]🎯 steered[/magenta]  [bright_black]{instruction}[/bright_black]")

    async def _on_stream_reset(self, task_id: str, data: dict) -> None:
        # Reset token buffer when tool calling is engaged mid-thought
        self._token_buffer.pop(task_id, None)
        self._reasoning_buffer.pop(task_id, None)
        if task_id in self._streaming_active:
            self.query_one("#streaming", Static).update("")

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
        if self.yolo:
            ans = options[0] if options else "Use your best judgment."
            self._log(f"  [#f85149]⚠[/#f85149]  [bright_black]auto-answered: {ans}[/bright_black]")
            await self._submit_approval("1" if options else ans)
            return
        self._log("  [cyan]◆[/cyan]  [cyan]north asks[/cyan]")
        self._log_rich(RichText("    " + data.get("question", ""), style="white"))
        for i, opt in enumerate(options, 1):
            self._log(f"    [bright_black][{i}][/bright_black]  {opt}")
        hint = "type a number or your own answer" if options else "type your answer"
        self._log(f"    [bright_black]{hint}[/bright_black]")

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
        try:
            idx = int(raw) - 1
            chosen = options[idx] if 0 <= idx < len(options) else raw
        except ValueError:
            if is_question:
                chosen = raw  # free-form answer
            else:
                low = raw.lower()
                if low in ("a", "approve", "approved", "yes"):
                    chosen = options[0]
                elif low in ("r", "reject", "rejected", "no"):
                    chosen = options[1] if len(options) > 1 else options[0]
                else:
                    chosen = raw or options[0]
        if is_question:
            decision = "answered"
        else:
            decision = (
                "approved"
                if chosen == options[0]
                else "rejected"
                if len(options) > 1 and chosen == options[1]
                else "answered"
            )
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
                        self._log("    [bright_black]Type '/cancel <id>' to cancel a task/job, or '/cancel all' to cancel everything.[/bright_black]")
            except Exception as exc:
                self._log(f"  [red]error fetching queue: {exc}[/red]")
        elif cmd.startswith("/cancel"):
            parts = text.strip().split()
            target = parts[1] if len(parts) > 1 else ""
            if not target or target.lower() in ("all", "--all"):
                try:
                    async with self._http() as c:
                        r = await c.post(f"{self.base_url}/orchestrator/cancel-all", headers=self.headers, timeout=10.0)
                        if r.status_code == 200:
                            data = r.json()
                            self._log(
                                f"  [yellow]✓[/yellow]  [white]cancelled {data.get('tasks_cancelled', 0)} active task(s) and {data.get('jobs_cancelled', 0)} queued job(s)[/white]"
                            )
                        else:
                            self._log(f"  [red]error cancelling tasks (HTTP {r.status_code})[/red]")
                except Exception as exc:
                    self._log(f"  [red]error cancelling tasks: {exc}[/red]")
            else:
                try:
                    async with self._http() as c:
                        r = await c.post(f"{self.base_url}/orchestrator/cancel/{target}", headers=self.headers, timeout=10.0)
                        if r.status_code == 200:
                            data = r.json()
                            self._log(f"  [yellow]✓[/yellow]  [white]cancelled {data.get('cancelled', 'task')} {data.get('id', target)}[/white]")
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
        elif cmd == "/thoughts":
            self.action_toggle_reasoning()
            state = "expanded" if self._reasoning_visible else "collapsed"
            self._log(f"  [bright_black]Live reasoning drawer {state} (Ctrl+T)[/bright_black]")
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

    def action_toggle_reasoning(self) -> None:
        """Toggle the live Chain-of-Thought reasoning drawer."""
        self._reasoning_visible = not self._reasoning_visible
        with contextlib.suppress(Exception):
            wrap = self.query_one("#reasoning-wrap", VerticalScroll)
            wrap.styles.display = "block" if self._reasoning_visible else "none"
        self._refresh_hint()

    def action_inspect_tools(self) -> None:
        """Open the interactive Tool & Diff Inspector modal."""
        self.push_screen(ToolInspectorModal(list(self._tool_history)))

    def action_inspect_plan(self) -> None:
        """Open the execution plan cockpit modal."""
        self.push_screen(PlanCockpitModal(list(self._plan_steps), list(self._dod_results), self._active_phase))

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
