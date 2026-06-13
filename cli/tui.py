"""North TUI — Textual-based chat UI.

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
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
from rich.markdown import Markdown as RichMarkdown
from rich.padding import Padding as RichPadding
from rich.text import Text as RichText
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.suggester import Suggester
from textual.widgets import Input, Markdown, RichLog, Static

from cli.constants import (
    _SLASH_COMMANDS,
    _SPIN,
    _SSE_BACKOFF_BASE,
    _SSE_BACKOFF_MAX,
)
from cli.formatting import (
    _compute_suggestion,
    _fill_bar,
    _fmt_elapsed,
    _fmt_params,
    _fmt_tokens,
    _short_model,
    _strip_markup,
)


class _NorthSuggester(Suggester):
    """Drives the Input's dim ghost-text using slash commands + input history."""

    def __init__(self, history_getter) -> None:
        super().__init__(use_cache=False, case_sensitive=True)
        self._history_getter = history_getter

    async def get_suggestion(self, value: str) -> str | None:
        return _compute_suggestion(value, self._history_getter())


def _read_strategy(settings_path: Path) -> str:
    """Read the current strategy from the north settings file.

    Falls back to 'cruise' if the file is absent or unreadable so the info bar
    always shows something meaningful without crashing the TUI.
    """
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        return str(data.get("strategy", "cruise"))
    except Exception:
        return "cruise"


class NorthApp(App[None]):
    """Textual chat UI for north."""

    # Layout (top → bottom):
    #   #log           — scrollable chat history (top-anchored)  (1fr)
    #   #streaming     — live markdown during token stream       (auto, hidden)
    #   #status        — working spinner (empty when idle)        (1 row)
    #   #input-row     — ╭ >  [                       ] ╮ box     (3 rows)
    #   #hint          — dim shortcuts (strategy · history · …)   (1 row)

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
           scrollbar — the only focus difference should be the input box border. */
        scrollbar-size: 1 1;
        scrollbar-background: $background;
        scrollbar-background-hover: $background;
        scrollbar-background-active: $background;
        scrollbar-color: $background;
        scrollbar-color-hover: $background;
        scrollbar-color-active: $background;
        scrollbar-corner-color: $background;
    }

    /* ── live streaming area ──────────────────────────────── */

    #streaming {
        width: 100%;
        height: auto;
        max-height: 50%;
        display: none;
        padding: 0 0 0 4;
        background: $background;
        color: $text;
        scrollbar-size: 1 1;
        scrollbar-background: $background;
        scrollbar-background-hover: $background;
        scrollbar-background-active: $background;
        scrollbar-color: $background;
        scrollbar-color-hover: $background;
        scrollbar-color-active: $background;
        scrollbar-corner-color: $background;
    }

    /* Markdown renders paragraph / fence / list block sub-widgets with
       $panel by default — flatten them all to $background so there is
       no lighter-shade box inside the streaming area. */
    #streaming MarkdownBlock,
    #streaming MarkdownParagraph,
    #streaming MarkdownH1,
    #streaming MarkdownH2,
    #streaming MarkdownH3,
    #streaming MarkdownH4,
    #streaming MarkdownFence,
    #streaming MarkdownBulletList,
    #streaming MarkdownBulletListItem,
    #streaming MarkdownOrderedList,
    #streaming MarkdownOrderedListItem {
        background: $background;
        border: none;
        padding: 0;
        margin: 0;
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

        self._token_buffer: dict[str, str] = {}
        self._streaming_active: set[str] = set()
        self._approval_pending: dict | None = None
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
            "task_synthesis": self._on_task_synthesis,
            "task_completed": self._on_task_completed,
            "task_failed": self._on_task_failed,
            "task_cancelled": self._on_task_cancelled,
            "approval_required": self._on_approval_required,
        }

        # ── session metrics (drive the live status bar) ──────────────────────
        self._session_tokens: int = 0  # cumulative estimate (chars/4)
        self._session_cost: float = 0.0  # summed task_completed.cost_usd
        self._compactions: int = 0  # count of 'compaction' SSE events
        self._start_time: float = time.monotonic()
        # Double-Ctrl+C-to-exit: monotonic timestamp of the last single Ctrl+C.
        self._last_interrupt: float = 0.0
        # Paste-preview: large pastes are stashed here and shown as a placeholder
        # until the user presses Enter, keeping the scrollback clean.
        self._pending_paste: str | None = None

    def compose(self) -> ComposeResult:
        yield RichLog(id="log", highlight=False, markup=True, wrap=True)
        yield Markdown("", id="streaming")
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

        self._strategy = _read_strategy(self._settings_path)
        self._refresh_hint()
        self._render_status_bar()
        self._set_status("")

        self.set_interval(0.08, self._tick)
        self.run_worker(self._listen(), exclusive=False)
        self.query_one("#prompt", Input).focus()
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
            async with httpx.AsyncClient() as c:
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

    # ── rendering helpers ────────────────────────────────────────────────────

    def _refresh_hint(self) -> None:
        hint = f"  {self._strategy}  ·  ↑↓ history  ·  /commands  ·  ctrl+g editor  ·  ctrl+c interrupt"
        self.query_one("#hint", Static).update(f"[bright_black]{hint}[/bright_black]")

    def _render_status_bar(self) -> None:
        """Compose the live status bar, dropping low-priority segments as the
        terminal narrows so the bar never wraps or truncates mid-segment."""
        from agents.context_compaction import context_window_for

        width = self.size.width or 80

        ctx_max = context_window_for(self._model) if self._model else 0
        fraction = (self._session_tokens / ctx_max) if ctx_max else 0.0

        model = _short_model(self._model) if self._model else "—"
        tokens = f"{_fmt_tokens(self._session_tokens)}/{_fmt_tokens(ctx_max)}" if ctx_max else ""
        bar = _fill_bar(fraction) if ctx_max else ""
        cost = f"${self._session_cost:.4f}"
        compactions = f"⊕{self._compactions}" if self._compactions else ""
        active = sum(1 for _ in self._user_task_ids)
        tasks = f"⚙{active}" if active else ""
        elapsed = _fmt_elapsed(time.monotonic() - self._start_time)
        yolo = "[#f85149]⚠ YOLO[/#f85149]" if self.yolo else ""

        # (text, priority) — higher priority survives longer as width shrinks.
        segments: list[tuple[str, int]] = [
            (f"[#6cb6ff]{model}[/#6cb6ff]", 5),
            (f"{tokens} {bar}".strip(), 4),
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
        md = self.query_one("#streaming", Markdown)
        md.display = True
        md.update("")

    def _update_streaming(self, task_id: str) -> None:
        self.query_one("#streaming", Markdown).update(self._token_buffer.get(task_id, ""))

    def _finish_streaming(self, task_id: str, final_output: str) -> None:
        md = self.query_one("#streaming", Markdown)
        md.display = False
        md.update("")
        if final_output:
            # Render the finalized message through a real markdown engine so the
            # scrollback matches what was shown live in the streaming Markdown
            # widget — tables, lists, and inline styling survive the handoff.
            # (Do NOT flatten with _to_prose here: it has no table support and
            # forks rendering from the streaming path, which is what produced the
            # "table un-renders when the stream finishes" bug.)
            # TODO(tier-2): for byte-identical fidelity, mount a Textual Markdown
            # widget into the log instead of writing a rich.markdown renderable —
            # rich's table/heading chrome differs subtly from Textual's. Deferred
            # because #log is a RichLog and that conversion touches every _log()
            # call site.
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

    async def _on_classified(self, task_id: str, data: dict) -> None:
        domain = data.get("domain", "")
        flag = "  ·  consequential" if data.get("is_consequential") else ""
        self._set_status(f"routing → {domain}{flag}…")

    async def _on_routed(self, task_id: str, data: dict) -> None:
        agents = data.get("agents") or []
        self._set_status(f"running {', '.join(agents) or 'general'}…")

    async def _on_north_star_checking(self, task_id: str, data: dict) -> None:
        self._set_status("checking goals…")

    async def _on_north_star_noop(self, task_id: str, data: dict) -> None:
        """north_star_aligned / north_star_check_skipped — no UI change."""

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
        # Write an agent header for specialist agents so the user can see who
        # is working. The default 'general' agent is suppressed — its header
        # would just echo the user's prompt (already shown) and the answer is
        # labelled '◆ north' below.
        if agent and agent != "general":
            task_desc = (data.get("task") or "").strip()
            desc_part = f"  [bright_black]{task_desc[:80]}[/bright_black]" if task_desc else ""
            self._log(f"  [cyan]◆[/cyan]  [white]{agent}[/white]{desc_part}")

    async def _on_tool_called(self, task_id: str, data: dict) -> None:
        tool = data.get("tool", "")
        params = data.get("params") or {}
        params_str = _fmt_params(params)
        suffix = f"[bright_black]({params_str})[/bright_black]" if params_str else ""
        self._log(f"    [bright_black]→[/bright_black]  [cyan]{tool}[/cyan]{suffix}")
        self._set_status(f"{tool}…")
        if task_id:
            self._task_tool_activity.setdefault(task_id, []).append(
                {"tool": tool, "params": params_str, "result": None}
            )

    async def _on_tool_result(self, task_id: str, data: dict) -> None:
        tool = data.get("tool", "")
        success = data.get("success", True)
        self._log(f"    [dim green]✓  {tool}[/dim green]" if success else f"    [dim red]✗  {tool}[/dim red]")
        if task_id:
            formatted = data.get("formatted", "")
            error = data.get("error", "")
            result = (
                formatted[:200].replace("\n", " ")
                if formatted
                else f"failed: {error[:100]}"
                if error
                else ("ok" if success else "failed")
            )
            for entry in self._task_tool_activity.get(task_id, []):
                if entry["tool"] == tool and entry["result"] is None:
                    entry["result"] = result
                    break
        self._set_status("thinking…")

    async def _on_token(self, task_id: str, data: dict) -> None:
        text = data.get("text", "")
        if not text:
            return
        self._token_buffer[task_id] = self._token_buffer.get(task_id, "") + text
        # Rough running token estimate (≈4 chars/token) for the status bar.
        self._session_tokens += max(1, len(text) // 4)
        if task_id not in self._streaming_active:
            self._streaming_active.add(task_id)
            self._set_status("")
            self._log("  [cyan]◆[/cyan]  [white]north[/white]")
            self._start_streaming()
        self._update_streaming(task_id)

    async def _on_task_synthesis(self, task_id: str, data: dict) -> None:
        self._set_status("synthesising…")

    async def _on_task_completed(self, task_id: str, data: dict) -> None:
        sys.stdout.write("\a")
        sys.stdout.flush()
        self._session_cost += float(data.get("cost_usd", 0.0) or 0.0)
        output = self._token_buffer.pop(task_id, "")
        was_streaming = task_id in self._streaming_active
        self._streaming_active.discard(task_id)

        if not output:
            output = await self._fetch_ledger_output(task_id)

        if was_streaming:
            self._finish_streaming(task_id, output)
        elif output:
            self._log("  [cyan]◆[/cyan]  [white]north[/white]")
            # Same markdown path as _finish_streaming — keep the non-streamed
            # branch (output fetched whole from the ledger) rendering tables
            # and lists identically rather than flattening to prose.
            self._log_rich(RichPadding(RichMarkdown(output), (0, 0, 0, 4)))

        # Refresh strategy in case the user issued a strategy command.
        self._strategy = _read_strategy(self._settings_path)
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
            async with httpx.AsyncClient() as c:
                r = await c.get(
                    f"{self.base_url}/orchestrator/ledger",
                    params={"task_id": task_id, "limit": 20},
                    headers=self.headers,
                    timeout=5.0,
                )
                entries = r.json()
                return "\n\n".join(
                    e["output"] for e in entries if e.get("action") == "agent_completed" and e.get("output")
                )
        except Exception:
            return ""

    async def _on_task_failed(self, task_id: str, data: dict) -> None:
        sys.stdout.write("\a")
        sys.stdout.flush()
        if task_id in self._streaming_active:
            self._finish_streaming(task_id, "")
        self._streaming_active.discard(task_id)
        self._token_buffer.pop(task_id, None)
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
        self._task_tool_activity.pop(task_id, None)
        self._set_status("")
        self._user_task_ids.discard(task_id)
        self._log("  [dim]cancelled[/dim]")
        self._render_status_bar()
        self._write_rule()

    async def _on_approval_required(self, task_id: str, data: dict) -> None:
        self._approval_pending = data
        self._set_status("")
        options = data.get("options") or ["Approve", "Reject"]
        if self.yolo:
            # Auto-approve mode: take the first option without prompting.
            self._log(f"  [#f85149]⚠[/#f85149]  [bright_black]auto-approved: {options[0]}[/bright_black]")
            await self._submit_approval("1")
            return
        self._log("  [yellow]◆[/yellow]  [yellow]approval required[/yellow]")
        self._log_rich(RichText("    " + data.get("message", ""), style="white"))
        for i, opt in enumerate(options, 1):
            self._log(f"    [bright_black][{i}][/bright_black]  {opt}")

    # ── SSE listener (runs as Textual worker in the same event loop) ─────────

    async def _listen(self) -> None:
        delay = _SSE_BACKOFF_BASE
        while True:
            try:
                async with (
                    httpx.AsyncClient() as client,
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
                    # Successful connection — reset backoff.
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
        if data is None:
            return
        options = data.get("options") or ["Approve", "Reject"]
        try:
            idx = int(raw) - 1
            chosen = options[idx] if 0 <= idx < len(options) else raw
        except ValueError:
            low = raw.lower()
            if low in ("a", "approve", "approved", "yes"):
                chosen = options[0]
            elif low in ("r", "reject", "rejected", "no"):
                chosen = options[1] if len(options) > 1 else options[0]
            else:
                chosen = raw or options[0]
        decision = (
            "approved"
            if chosen == options[0]
            else "rejected"
            if len(options) > 1 and chosen == options[1]
            else "answered"
        )
        try:
            async with httpx.AsyncClient() as c:
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
            return  # small paste — let the Input insert it normally
        self._pending_paste = text
        prompt = self.query_one("#prompt", Input)
        prompt.value = f"[pasted: {n_lines} lines, {len(text)} chars — press Enter to send]"
        prompt.cursor_position = len(prompt.value)
        event.prevent_default()
        event.stop()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.clear()
        # A pending paste replaces the placeholder text with the real content.
        if self._pending_paste is not None:
            text = self._pending_paste.strip()
            self._pending_paste = None
        if not text:
            return

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
            async with httpx.AsyncClient() as c:
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
        elif cmd == "/strategy":
            self._strategy = _read_strategy(self._settings_path)
            self._log(f"  [bright_black]strategy[/bright_black] {self._strategy}")
            self._render_status_bar()
        elif cmd == "/agents":
            agents = await self._fetch_agents()
            self._log("  [bright_black]agents[/bright_black]  " + (", ".join(agents) or "none"))
        elif cmd == "/help":
            for name, desc in _SLASH_COMMANDS.items():
                self._log(f"  [cyan]{name}[/cyan]  [bright_black]{desc}[/bright_black]")
        else:
            self._log(f"  [bright_black]unknown command: {cmd} — try /help[/bright_black]")

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
            self._log("  [dim]interrupted — press ctrl+c again to exit[/dim]")
        else:
            self._log("  [dim]press ctrl+c again to exit[/dim]")

    async def _cancel_active(self) -> None:
        """Ask the server to cancel every task this session started."""
        for task_id in list(self._user_task_ids):
            try:
                async with httpx.AsyncClient() as c:
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


async def run(
    base_url: str,
    headers: dict,
    workspace: str | None = None,
    yolo: bool = False,
) -> None:
    """Launch the TUI. Blocks until the user exits."""
    app = NorthApp(base_url=base_url, headers=headers, workspace=workspace, yolo=yolo)
    await app.run_async()
