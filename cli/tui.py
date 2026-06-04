"""North TUI — single-terminal chat + live task activity + inline approvals."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path

import httpx
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

_STRATEGY_COLORS = {
    "eco":    "ansigreen",
    "cruise": "ansicyan",
    "sport":  "ansiyellow",
}

_SPIN = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


def _get_strategy() -> str:
    try:
        from config.settings import settings as _s
        from config.strategy import NorthSettings as _NS
        return _NS(_s.north_home / "settings.json").strategy.value
    except Exception:
        return "cruise"


def _prompt_tokens() -> FormattedText:
    mode = _get_strategy()
    color = _STRATEGY_COLORS.get(mode, "ansicyan")
    return FormattedText([
        ("", "\n  "),
        (f"{color}", mode),
        ("", "  "),
        ("bold", "❯ "),
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
    """Animated status line that rewrites itself in-place using \\r."""

    def __init__(self) -> None:
        self._active = False
        self._text = ""
        self._frame = 0
        self._width = 0

    def _raw(self, s: str) -> None:
        sys.stdout.write(s)
        sys.stdout.flush()

    def _erase(self) -> None:
        if self._width:
            self._raw(f"\r{' ' * self._width}\r")
            self._width = 0

    def _draw(self) -> None:
        f = _SPIN[self._frame % len(_SPIN)]
        line = f"  {f}  {self._text}"
        padding = max(0, self._width - len(line))
        self._raw(f"\r{line}{' ' * padding}")
        self._width = len(line)

    def start(self, text: str) -> None:
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


async def run(
    base_url: str,
    headers: dict,
    workspace: str | None = None,
) -> None:
    """Launch the TUI. Blocks until the user exits."""
    history_file = Path.home() / ".north" / "tui_history"
    history_file.parent.mkdir(parents=True, exist_ok=True)

    session = PromptSession(
        history=FileHistory(str(history_file)),
        enable_history_search=True,
        mouse_support=False,
    )

    token_buffer: dict[str, str] = {}
    approval_queue: asyncio.Queue[dict] = asyncio.Queue()
    user_task_ids: set[str] = set()

    spinner = _Spinner()
    console: Console = Console()

    def _print(obj: object) -> None:
        spinner.before_print()
        console.print(obj)
        spinner.after_print()

    async def _handle_event(event: str, data: dict) -> None:  # noqa: C901
        task_id = data.get("task_id", "")

        if task_id and task_id not in user_task_ids:
            return

        if event == "classifying":
            spinner.update("classifying…")

        elif event == "classified":
            domain = data.get("domain", "")
            flag = "  ·  consequential" if data.get("is_consequential") else ""
            spinner.update(f"routing → {domain}{flag}…")

        elif event == "routed":
            agents = data.get("agents") or []
            label = ", ".join(agents) if agents else "general"
            spinner.update(f"running {label}…")

        elif event == "north_star_checking":
            spinner.update("checking goals…")

        elif event == "north_star_aligned":
            pass

        elif event == "north_star_check_skipped":
            pass

        elif event == "north_star_conflict":
            tension = (data.get("tension") or "")[:200]
            spinner.stop()
            console.print()
            console.print(
                Panel(
                    Text(tension, style="white"),
                    title="[yellow]goal conflict[/yellow]",
                    border_style="yellow",
                    padding=(1, 2),
                )
            )

        elif event == "executing":
            agents = data.get("agents") or []
            if agents:
                spinner.update(f"running {', '.join(agents)}…")

        elif event == "agent_started":
            spinner.update(f"running {data.get('agent', '')}…")

        elif event == "tool_called":
            tool = data.get("tool", "")
            params = data.get("params") or {}
            params_str = _fmt_params(params)
            suffix = f"[bright_black]({params_str})[/bright_black]" if params_str else ""
            _print(f"  [bright_black]→[/bright_black]  [cyan]{tool}[/cyan]{suffix}")
            spinner.update(f"{tool}…")

        elif event == "tool_result":
            tool = data.get("tool", "")
            success = data.get("success", True)
            if success:
                _print(f"  [dim green]✓  {tool}[/dim green]")
            else:
                _print(f"  [dim red]✗  {tool}[/dim red]")
            spinner.update("thinking…")

        elif event == "token":
            token_buffer[task_id] = token_buffer.get(task_id, "") + data.get("text", "")

        elif event == "task_synthesis":
            spinner.update("synthesising…")

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
            user_task_ids.discard(task_id)
            if output:
                console.print()
                console.print(
                    Panel(
                        Markdown(output),
                        title="[dim]north[/dim]",
                        border_style="bright_black",
                        padding=(1, 2),
                    )
                )

        elif event == "task_failed":
            sys.stdout.write("\a")
            sys.stdout.flush()
            token_buffer.pop(task_id, None)
            error = data.get("error", "Task failed.")
            spinner.stop()
            user_task_ids.discard(task_id)
            console.print()
            console.print(
                Panel(
                    Text(error, style="red"),
                    title="[dim]north — error[/dim]",
                    border_style="bright_black",
                    padding=(1, 2),
                )
            )

        elif event == "task_cancelled":
            token_buffer.pop(task_id, None)
            spinner.stop()
            user_task_ids.discard(task_id)
            console.print("\n  [dim]cancelled[/dim]")

        elif event == "approval_required":
            await approval_queue.put(data)
            spinner.stop()
            console.print()
            console.print(
                Panel(
                    Text(data.get("message", ""), style="white"),
                    title="[yellow]approval required[/yellow]",
                    border_style="yellow",
                    padding=(1, 2),
                )
            )
            options = data.get("options") or ["Approve", "Reject"]
            for i, opt in enumerate(options, 1):
                console.print(f"  [bright_black][{i}][/bright_black]  {opt}")
            console.print()

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

    async def _handle_pending_approval() -> None:
        if approval_queue.empty():
            return
        data = await approval_queue.get()
        options = data.get("options") or ["Approve", "Reject"]
        raw = (await session.prompt_async("  ❯ ")).strip()
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
        while True:
            await asyncio.sleep(0.08)
            spinner.tick()

    # Welcome banner
    _banner = Console()
    _banner.print()
    _banner.print("  [bold white]north[/bold white]  [bright_black]personal operating system[/bright_black]")
    _banner.print(f"  [bright_black]{'─' * 44}[/bright_black]")
    _banner.print(f"  [dim]strategy: {_get_strategy()}  ·  exit to quit[/dim]")
    _banner.print()

    listener = asyncio.create_task(_listen())

    with patch_stdout(raw=True):
        console = Console(force_terminal=True)
        spin_task = asyncio.create_task(_spin_loop())

        while True:
            if not approval_queue.empty():
                await _handle_pending_approval()
                continue

            try:
                text = await session.prompt_async(_prompt_tokens)
            except KeyboardInterrupt:
                console.print("  [dim]interrupted  (exit to quit)[/dim]")
                continue
            except EOFError:
                console.print("  [dim]goodbye[/dim]")
                break

            text = text.strip()
            if not text:
                continue
            if text.lower() in ("exit", "quit", "bye"):
                console.print("  [dim]goodbye[/dim]")
                break

            if not approval_queue.empty():
                await _handle_pending_approval()

            spinner.start("…")

            body: dict = {"prompt": text}
            if workspace:
                body["workspace"] = workspace
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
            except httpx.ConnectError:
                spinner.stop()
                console.print("  [red]cannot reach north server[/red]")
                continue
            except Exception as exc:
                spinner.stop()
                console.print(f"  [red]error: {exc}[/red]")
                continue

    spin_task.cancel()
    listener.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.gather(spin_task, listener)
