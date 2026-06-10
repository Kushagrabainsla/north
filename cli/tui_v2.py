"""North TUI v2 — Textual-based chat UI.

Replaces tui.py (prompt_toolkit + Rich.Live) with a Textual App that owns the
full render cycle. This is the same approach used by Bubbletea-based tools like
gh copilot: the framework explicitly re-positions the cursor inside the Input
widget after every frame, so live streaming output and the input box coexist
without cursor conflicts.

tui.py is kept for reference but is no longer invoked.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import deque
from pathlib import Path

import httpx
from rich.markdown import Markdown as RichMarkdown
from rich.padding import Padding as RichPadding
from rich.text import Text as RichText
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Input, Markdown, RichLog, Static

_SPIN = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

# Seconds between SSE reconnect attempts; doubles on each failure up to _SSE_BACKOFF_MAX.
_SSE_BACKOFF_BASE = 2.0
_SSE_BACKOFF_MAX = 30.0


def _fmt_params(params: dict) -> str:
    parts = []
    for k, v in params.items():
        if k in ("workspace", "task_id"):
            continue
        v_str = repr(v)
        if len(v_str) > 60:
            v_str = v_str[:57] + "…'"
        parts.append(f"{k}={v_str}")
    return ", ".join(parts[:4])


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
    #   #status        — spinner / info line                     (1 row)
    #   #sep           — ─── single rule above the input         (1 row)
    #   #input-row     — >  [                           ]        (1 row)

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
        scrollbar-size: 1 1;
        scrollbar-background: $background;
        scrollbar-background-hover: $background;
        scrollbar-color: $background;
        scrollbar-color-hover: $primary;
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

    #status {
        width: 100%;
        height: 1;
        background: $background;
        color: $text-muted;
        padding: 0;
    }

    #sep {
        width: 100%;
        height: 1;
        background: $background;
        color: $text-muted;
    }

    #input-row {
        width: 100%;
        height: 1;
        background: $background;
    }

    #prompt-prefix {
        width: auto;
        height: 1;
        padding: 0 1 0 2;
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

    Input {
        border: none;
        background: $background;
        padding: 0;
    }

    /* ── kill all focus / hover / active tints on every widget ── */
    /* Textual's DEFAULT_CSS applies accent borders and background   */
    /* tints on focus — override every state to stay flat.          */

    Screen:focus-within,
    #log:focus,
    #log:focus-within,
    #log:hover,
    #streaming:focus,
    #streaming:focus-within,
    #status:focus,
    #status:hover,
    #sep:focus,
    #sep:hover,
    #input-row:focus,
    #input-row:focus-within,
    #input-row:hover,
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
        Binding("ctrl+c", "quit", "Quit", priority=True),
        Binding("up", "history_prev", "Previous", show=False),
        Binding("down", "history_next", "Next", show=False),
    ]

    def __init__(
        self,
        base_url: str,
        headers: dict,
        workspace: str | None = None,
    ) -> None:
        super().__init__()
        self.base_url = base_url
        self.headers = headers
        self.workspace = workspace

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
        self._settings_path = Path.home() / ".north" / "settings.json"

    def compose(self) -> ComposeResult:
        yield RichLog(id="log", highlight=False, markup=True, wrap=True)
        yield Markdown("", id="streaming")
        yield Static("", id="status")
        yield Static("", id="sep")
        with Horizontal(id="input-row"):
            yield Static(">", id="prompt-prefix")
            yield Input(id="prompt")

    def on_mount(self) -> None:
        history_file = Path.home() / ".north" / "tui_history"
        if history_file.exists():
            try:
                self._input_history = [
                    line for line in history_file.read_text().splitlines() if line.strip()
                ]
            except Exception:
                pass

        self._strategy = _read_strategy(self._settings_path)
        self._redraw_seps()
        self._set_status("")

        self.set_interval(0.08, self._tick)
        self.run_worker(self._listen(), exclusive=False)
        self.query_one("#prompt", Input).focus()
        # Defer so the log's width is known before drawing the banner rule.
        self.call_after_refresh(self._draw_banner)

    def _draw_banner(self) -> None:
        # Top-anchored: the banner is the first thing in the log; chat flows
        # downward beneath it and the input stays pinned at the bottom.
        log = self.query_one("#log", RichLog)
        log.write("")
        log.write("  [bold white]north[/bold white]")
        log.write("")
        self._write_rule()

    def on_resize(self) -> None:
        self._redraw_seps()

    # ── rendering helpers ────────────────────────────────────────────────────

    def _redraw_seps(self) -> None:
        line = "[bright_black]" + "─" * self.size.width + "[/bright_black]"
        self.query_one("#sep", Static).update(line)

    def _write_rule(self) -> None:
        log = self.query_one("#log", RichLog)
        width = log.scrollable_content_region.width or (self.size.width - 1) or 80
        log.write("[bright_black]" + "─" * width + "[/bright_black]")
        log.scroll_end(animate=False)

    def _tick(self) -> None:
        self._spin_frame += 1
        if self._status_text:
            f = _SPIN[self._spin_frame % len(_SPIN)]
            self.query_one("#status", Static).update(
                f"[bright_black]  {f}  {self._status_text}[/bright_black]"
            )

    def _idle_status(self) -> str:
        return f"[bright_black]  {self._strategy}  ·  ↑↓ history  ·  ctrl+c quit[/bright_black]"

    def _set_status(self, text: str) -> None:
        self._status_text = text
        if not text:
            self.query_one("#status", Static).update(self._idle_status())
        else:
            f = _SPIN[self._spin_frame % len(_SPIN)]
            self.query_one("#status", Static).update(
                f"[bright_black]  {f}  {text}[/bright_black]"
            )

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
        self.query_one("#streaming", Markdown).update(
            self._token_buffer.get(task_id, "")
        )

    def _finish_streaming(self, task_id: str, final_output: str) -> None:
        md = self.query_one("#streaming", Markdown)
        md.display = False
        md.update("")
        if final_output:
            self._log_rich(RichPadding(RichMarkdown(final_output), (0, 0, 0, 4)))

    # ── SSE event handler ────────────────────────────────────────────────────

    async def _handle_event(self, event: str, data: dict) -> None:  # noqa: C901
        task_id = data.get("task_id", "")

        if task_id and task_id not in self._user_task_ids:
            return

        if event == "classifying":
            self._set_status("classifying…")

        elif event == "classified":
            domain = data.get("domain", "")
            flag = "  ·  consequential" if data.get("is_consequential") else ""
            self._set_status(f"routing → {domain}{flag}…")

        elif event == "routed":
            agents = data.get("agents") or []
            self._set_status(f"running {', '.join(agents) or 'general'}…")

        elif event == "north_star_checking":
            self._set_status("checking goals…")

        elif event in ("north_star_aligned", "north_star_check_skipped"):
            pass

        elif event == "north_star_conflict":
            tension = (data.get("tension") or "")[:200]
            self._set_status("")
            self._log("  [yellow]◆[/yellow]  [yellow]goal conflict[/yellow]")
            self._log_rich(RichText("    " + tension, style="white"))

        elif event in ("executing", "agent_started"):
            agent = data.get("agent", "")
            agents = data.get("agents") or []
            label = ", ".join(agents) if agents else agent or "general"
            self._set_status(f"running {label}…")
            # Write an agent header line to the chat log so the user can see
            # which agent is working and on what task.
            if agent:
                task_desc = (data.get("task") or "").strip()
                if task_desc:
                    desc_part = f"  [bright_black]{task_desc[:80]}[/bright_black]"
                else:
                    desc_part = ""
                self._log(f"  [cyan]◆[/cyan]  [white]{agent}[/white]{desc_part}")

        elif event == "tool_called":
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

        elif event == "tool_result":
            tool = data.get("tool", "")
            success = data.get("success", True)
            self._log(
                f"    [dim green]✓  {tool}[/dim green]"
                if success
                else f"    [dim red]✗  {tool}[/dim red]"
            )
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

        elif event == "token":
            text = data.get("text", "")
            if not text:
                return
            self._token_buffer[task_id] = self._token_buffer.get(task_id, "") + text
            if task_id not in self._streaming_active:
                self._streaming_active.add(task_id)
                self._set_status("")
                self._log("  [cyan]◆[/cyan]  [white]north[/white]")
                self._start_streaming()
            self._update_streaming(task_id)

        elif event == "task_synthesis":
            self._set_status("synthesising…")

        elif event == "task_completed":
            sys.stdout.write("\a")
            sys.stdout.flush()
            output = self._token_buffer.pop(task_id, "")
            was_streaming = task_id in self._streaming_active
            self._streaming_active.discard(task_id)

            if not output:
                try:
                    async with httpx.AsyncClient() as c:
                        r = await c.get(
                            f"{self.base_url}/orchestrator/ledger",
                            params={"task_id": task_id, "limit": 20},
                            headers=self.headers,
                            timeout=5.0,
                        )
                        entries = r.json()
                        output = "\n\n".join(
                            e["output"]
                            for e in entries
                            if e.get("action") == "agent_completed" and e.get("output")
                        )
                except Exception:
                    pass

            if was_streaming:
                self._finish_streaming(task_id, output)
            elif output:
                self._log("  [cyan]◆[/cyan]  [white]north[/white]")
                self._log_rich(RichPadding(RichMarkdown(output), (0, 0, 0, 4)))

            # Refresh strategy in case the user issued a strategy command.
            self._strategy = _read_strategy(self._settings_path)
            self._set_status("")
            self._user_task_ids.discard(task_id)
            user_msg = self._pending_user_messages.pop(task_id, "")
            tools_used = self._task_tool_activity.pop(task_id, [])
            if user_msg and output:
                short = output[:600] + ("…" if len(output) > 600 else "")
                self._conversation_history.append(
                    {"user": user_msg, "tools": tools_used, "north": short}
                )
            self._write_rule()

        elif event == "task_failed":
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
            self._write_rule()

        elif event == "task_cancelled":
            if task_id in self._streaming_active:
                self._finish_streaming(task_id, "")
            self._streaming_active.discard(task_id)
            self._token_buffer.pop(task_id, None)
            self._task_tool_activity.pop(task_id, None)
            self._set_status("")
            self._user_task_ids.discard(task_id)
            self._log("  [dim]cancelled[/dim]")
            self._write_rule()

        elif event == "approval_required":
            self._approval_pending = data
            self._set_status("")
            self._log("  [yellow]◆[/yellow]  [yellow]approval required[/yellow]")
            self._log_rich(RichText("    " + data.get("message", ""), style="white"))
            options = data.get("options") or ["Approve", "Reject"]
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

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.clear()
        if not text:
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
        except httpx.ConnectError:
            self._set_status("")
            self._log("  [red]◆[/red]  [red]cannot reach north server[/red]")
        except Exception as exc:
            self._set_status("")
            self._log(f"  [red]◆[/red]  [red]error: {exc}[/red]")

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


async def run(base_url: str, headers: dict, workspace: str | None = None) -> None:
    """Launch the TUI. Blocks until the user exits."""
    app = NorthApp(base_url=base_url, headers=headers, workspace=workspace)
    await app.run_async()
