"""North TUI — single-terminal chat + live task activity + inline approvals."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import sys
from collections import deque
from pathlib import Path

import httpx
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.markdown import Markdown
from rich.padding import Padding
from rich.rule import Rule
from rich.text import Text

_STRATEGY_COLORS = {
    "eco":    "ansigreen",
    "cruise": "ansicyan",
    "sport":  "ansiyellow",
}

_SPIN = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

_PROMPT_STYLE = Style.from_dict({
    "bottom-toolbar": "noreverse bg:default fg:ansibrightblack",
})


def _get_strategy() -> str:
    try:
        from config.settings import settings as _s
        from config.strategy import NorthSettings as _NS
        return _NS(_s.north_home / "settings.json").strategy.value
    except Exception:
        return "cruise"


def _term_width() -> int:
    return shutil.get_terminal_size((80, 24)).columns


def _prompt_tokens() -> FormattedText:
    return FormattedText([
        ("", "  "),
        ("fg:ansicyan bold", "❯ "),
    ])


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


class _Spinner:
    """Animated status line — raw \\r writes only when no prompt is active."""

    def __init__(self) -> None:
        self._active = False
        self._text = ""
        self._frame = 0
        self._width = 0
        self.prompt_active = False  # set True while prompt_async() is running

    def _raw(self, s: str) -> None:
        sys.stdout.write(s)
        sys.stdout.flush()

    def _erase(self) -> None:
        if self._width:
            if not self.prompt_active:
                self._raw(f"\r{' ' * self._width}\r")
            self._width = 0

    def _draw(self) -> None:
        if self.prompt_active:
            return  # toolbar handles display; raw writes would corrupt the prompt
        f = _SPIN[self._frame % len(_SPIN)]
        line = f"  {f}  {self._text}"
        padding = max(0, self._width - len(line))
        self._raw(f"\r{line}{' ' * padding}")
        self._width = len(line)

    def start(self, text: str) -> None:
        self._raw("\n")  # advance past the committed prompt line before drawing
        self._erase()
        self._text = text
        self._active = True
        self._frame = 0
        self._draw()

    def update(self, text: str) -> None:
        self._text = text
        if self._active:
            self._draw()

    def tick(self) -> None:
        if self._active:
            self._frame += 1
            self._draw()

    def before_print(self) -> None:
        self._erase()

    def after_print(self) -> None:
        if self._active:
            self._draw()

    def stop(self) -> None:
        self._erase()
        self._active = False

    def begin_prompt(self) -> None:
        """Erase the spinner line in-place and hand the cursor to prompt_toolkit."""
        if self._active and self._width:
            # Clear the spinner text without advancing: cursor ends at col 0 of
            # the same line so prompt_async() renders right there on a clean line.
            self._raw(f"\r{' ' * self._width}\r")
        self._width = 0
        self.prompt_active = True


async def run(
    base_url: str,
    headers: dict,
    workspace: str | None = None,
) -> None:
    """Launch the TUI. Blocks until the user exits."""
    history_file = Path.home() / ".north" / "tui_history"
    history_file.parent.mkdir(parents=True, exist_ok=True)

    approval_pending: list[bool] = [False]
    toolbar_status: list[str] = [""]   # current processing status for toolbar
    spin_frame: list[int] = [0]        # shared frame counter for toolbar animation

    def _toolbar() -> FormattedText:
        mode = _get_strategy()
        sep = "─" * _term_width()
        if approval_pending[0]:
            hint = f"  [1] approve  [2] cancel  ·  strategy: {mode}"
        elif toolbar_status[0]:
            f = _SPIN[spin_frame[0] % len(_SPIN)]
            hint = f"  {f}  {toolbar_status[0]}"
        else:
            hint = f"  strategy: {mode}  ·  ↑↓ history  ·  exit to quit"
        return FormattedText([
            ("fg:ansibrightblack", sep + "\n"),
            ("fg:ansibrightblack", hint),
        ])

    session = PromptSession(
        history=FileHistory(str(history_file)),
        enable_history_search=True,
        mouse_support=False,
        bottom_toolbar=_toolbar,
        style=_PROMPT_STYLE,
    )

    token_buffer: dict[str, str] = {}
    approval_queue: asyncio.Queue[dict] = asyncio.Queue()
    user_task_ids: set[str] = set()
    conversation_history: deque[dict] = deque(maxlen=5)
    pending_user_messages: dict[str, str] = {}
    # Accumulates structured tool call activity per task_id.
    # Each entry: {"tool": name, "params": str, "result": str | None}
    # Stored with the conversation turn so the model knows what actions were taken.
    task_tool_activity: dict[str, list[dict]] = {}

    spinner = _Spinner()
    console: Console = Console()

    def _print(obj: object) -> None:
        spinner.before_print()
        console.print(obj)
        spinner.after_print()

    def _set_status(text: str) -> None:
        """Update both the raw spinner and the toolbar status."""
        spinner.update(text)
        toolbar_status[0] = text

    async def _handle_event(event: str, data: dict) -> None:  # noqa: C901
        task_id = data.get("task_id", "")

        if task_id and task_id not in user_task_ids:
            return

        if event == "classifying":
            _set_status("classifying…")

        elif event == "classified":
            domain = data.get("domain", "")
            flag = "  ·  consequential" if data.get("is_consequential") else ""
            _set_status(f"routing → {domain}{flag}…")

        elif event == "routed":
            agents = data.get("agents") or []
            label = ", ".join(agents) if agents else "general"
            _set_status(f"running {label}…")

        elif event == "north_star_checking":
            _set_status("checking goals…")

        elif event == "north_star_aligned" or event == "north_star_check_skipped":
            pass

        elif event == "north_star_conflict":
            tension = (data.get("tension") or "")[:200]
            spinner.stop()
            toolbar_status[0] = ""
            console.print("  [yellow]goal conflict[/yellow]")
            console.print(Padding(Text(tension, style="white"), (0, 0, 0, 2)))

        elif event == "executing":
            agents = data.get("agents") or []
            if agents:
                _set_status(f"running {', '.join(agents)}…")

        elif event == "agent_started":
            _set_status(f"running {data.get('agent', '')}…")

        elif event == "tool_called":
            tool = data.get("tool", "")
            params = data.get("params") or {}
            params_str = _fmt_params(params)
            suffix = f"[bright_black]({params_str})[/bright_black]" if params_str else ""
            _print(f"  [bright_black]→[/bright_black]  [cyan]{tool}[/cyan]{suffix}")
            _set_status(f"{tool}…")
            if task_id:
                task_tool_activity.setdefault(task_id, []).append(
                    {"tool": tool, "params": params_str, "result": None}
                )

        elif event == "tool_result":
            tool = data.get("tool", "")
            success = data.get("success", True)
            if success:
                _print(f"  [dim green]✓  {tool}[/dim green]")
            else:
                _print(f"  [dim red]✗  {tool}[/dim red]")
            if task_id:
                formatted = data.get("formatted", "")
                error = data.get("error", "")
                result = (
                    formatted[:200].replace("\n", " ") if formatted
                    else f"failed: {error[:100]}" if error
                    else ("ok" if success else "failed")
                )
                for entry in task_tool_activity.get(task_id, []):
                    if entry["tool"] == tool and entry["result"] is None:
                        entry["result"] = result
                        break
            _set_status("thinking…")

        elif event == "token":
            token_buffer[task_id] = token_buffer.get(task_id, "") + data.get("text", "")

        elif event == "task_synthesis":
            _set_status("synthesising…")

        elif event == "task_completed":
            sys.stdout.write("\a")
            sys.stdout.flush()
            output = token_buffer.pop(task_id, "")
            if not output:
                try:
                    async with httpx.AsyncClient() as c:
                        r = await c.get(
                            f"{base_url}/orchestrator/ledger",
                            params={"task_id": task_id, "limit": 20},
                            headers=headers,
                            timeout=5.0,
                        )
                        entries = r.json()
                        output = "\n\n".join(
                            e["output"] for e in entries
                            if e.get("action") == "agent_completed" and e.get("output")
                        )
                except Exception:
                    pass
            spinner.stop()
            toolbar_status[0] = ""
            user_task_ids.discard(task_id)
            user_msg = pending_user_messages.pop(task_id, "")
            tools_used = task_tool_activity.pop(task_id, [])
            if user_msg and output:
                short = output[:600] + ("…" if len(output) > 600 else "")
                conversation_history.append({
                    "user": user_msg,
                    "tools": tools_used,
                    "north": short,
                })
            if output:
                console.print("  [bright_black]north[/bright_black]")
                console.print(Padding(Markdown(output), (0, 0, 0, 2)))
            console.print(Rule(style="bright_black"))

        elif event == "task_failed":
            sys.stdout.write("\a")
            sys.stdout.flush()
            token_buffer.pop(task_id, None)
            task_tool_activity.pop(task_id, None)
            error = data.get("error", "Task failed.")
            spinner.stop()
            toolbar_status[0] = ""
            user_task_ids.discard(task_id)
            console.print("  [bright_black]north — error[/bright_black]")
            console.print(Padding(Text(error, style="red"), (0, 0, 0, 2)))
            console.print(Rule(style="bright_black"))

        elif event == "task_cancelled":
            token_buffer.pop(task_id, None)
            task_tool_activity.pop(task_id, None)
            spinner.stop()
            toolbar_status[0] = ""
            user_task_ids.discard(task_id)
            console.print("  [dim]cancelled[/dim]")
            console.print(Rule(style="bright_black"))

        elif event == "approval_required":
            await approval_queue.put(data)
            approval_pending[0] = True
            spinner.stop()
            toolbar_status[0] = ""
            console.print("  [yellow]approval required[/yellow]")
            console.print(Padding(Text(data.get("message", ""), style="white"), (0, 0, 0, 2)))
            options = data.get("options") or ["Approve", "Reject"]
            for i, opt in enumerate(options, 1):
                console.print(f"  [bright_black][{i}][/bright_black]  {opt}")

    async def _listen() -> None:
        while True:
            try:
                async with httpx.AsyncClient() as client, client.stream(
                    "GET",
                    f"{base_url}/orchestrator/stream",
                    headers=headers,
                    timeout=None,
                ) as resp:
                    if resp.status_code != 200:
                        await resp.aread()
                        await asyncio.sleep(2)
                        continue
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
                            event = current_event or payload.get("event", "")
                            await _handle_event(event, payload)
                            current_event = ""
            except asyncio.CancelledError:
                return
            except Exception:
                await asyncio.sleep(2)

    async def _handle_pending_approval(prefilled: str | None = None) -> None:
        if approval_queue.empty():
            return
        data = await approval_queue.get()
        options = data.get("options") or ["Approve", "Reject"]
        raw = prefilled if prefilled is not None else (await session.prompt_async("  ❯ ")).strip()
        approval_pending[0] = not approval_queue.empty()
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
            "approved" if chosen == options[0] else
            "rejected" if len(options) > 1 and chosen == options[1] else
            "answered"
        )
        try:
            async with httpx.AsyncClient() as c:
                await c.post(
                    f"{base_url}/orchestrator/approval/respond",
                    headers=headers,
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

    async def _spin_loop() -> None:
        from prompt_toolkit.application.current import get_app
        while True:
            await asyncio.sleep(0.08)
            spin_frame[0] += 1
            spinner.tick()
            # When the prompt is active, the raw spinner is suppressed; invalidate
            # the prompt_toolkit app instead so the toolbar re-renders with fresh status.
            if spinner.prompt_active:
                with contextlib.suppress(Exception):
                    get_app().invalidate()

    # Welcome banner
    _banner = Console()
    _banner.print()
    _banner.print("  [bold white]north[/bold white]  [bright_black]personal operating system[/bright_black]")
    _banner.print(f"  [dim]strategy: {_get_strategy()}[/dim]")
    _banner.print()
    _banner.print(Rule(style="bright_black"))

    listener = asyncio.create_task(_listen())

    with patch_stdout(raw=True):
        console = Console(force_terminal=True)
        spin_task = asyncio.create_task(_spin_loop())

        while True:
            if not approval_queue.empty():
                await _handle_pending_approval()
                continue

            spinner.begin_prompt()
            try:
                text = await session.prompt_async(_prompt_tokens)
            except KeyboardInterrupt:
                spinner.prompt_active = False
                console.print("  [dim]interrupted  (exit to quit)[/dim]")
                continue
            except EOFError:
                spinner.prompt_active = False
                console.print("  [dim]goodbye[/dim]")
                break
            spinner.prompt_active = False

            text = text.strip()
            if not text:
                continue
            if text.lower() in ("exit", "quit", "bye"):
                console.print("  [dim]goodbye[/dim]")
                break

            if not approval_queue.empty():
                await _handle_pending_approval(prefilled=text)
                continue

            spinner.start("…")
            toolbar_status[0] = "…"

            body: dict = {"prompt": text}
            if workspace:
                body["workspace"] = workspace
            if conversation_history:
                turns: list[str] = []
                for turn in conversation_history:
                    parts = [f"User: {turn['user']}"]
                    if turn.get("tools"):
                        summaries = [
                            f"{e['tool']}({e['params']}) → {e['result']}"
                            if e.get("params") else
                            f"{e['tool']} → {e['result']}"
                            for e in turn["tools"] if e.get("result")
                        ]
                        if summaries:
                            parts.append("[actions: " + "; ".join(summaries) + "]")
                    parts.append(f"north: {turn['north']}")
                    turns.append("\n".join(parts))
                body["context"] = "## Recent conversation\n" + "\n\n".join(turns)
            try:
                async with httpx.AsyncClient() as c:
                    resp = await c.post(
                        f"{base_url}/orchestrator/task",
                        headers=headers,
                        json=body,
                        timeout=30.0,
                    )
                    resp.raise_for_status()
                    task_id = resp.json().get("task_id", "")
                    if task_id:
                        user_task_ids.add(task_id)
                        pending_user_messages[task_id] = text
            except httpx.ConnectError:
                spinner.stop()
                toolbar_status[0] = ""
                console.print("  [red]cannot reach north server[/red]")
                continue
            except Exception as exc:
                spinner.stop()
                toolbar_status[0] = ""
                console.print(f"  [red]error: {exc}[/red]")
                continue

    spin_task.cancel()
    listener.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.gather(spin_task, listener)
