"""North TUI — single-terminal chat + live task activity + inline approvals.

Replaces the old readline-based _chat_loop with a prompt_toolkit PromptSession
so concurrent SSE output never clobbers the input line.  All task events arrive
via the persistent global SSE stream (/stream) — no per-task subscription needed.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
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

_console = Console()

_STRATEGY_COLORS = {
    "eco":    "ansigreen",
    "cruise": "ansicyan",
    "sport":  "ansiyellow",
}


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
        ("", "\n"),
        (f"bold {color}", f"[{mode}] ❯ "),
    ])


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _fmt_params(params: dict) -> str:
    parts = []
    for k, v in params.items():
        if k == "workspace":
            continue
        v_str = repr(v)
        if len(v_str) > 40:
            v_str = v_str[:37] + "…'"
        parts.append(f"{k}={v_str}")
    return ", ".join(parts[:4])


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

    # token_buffer accumulates streamed tokens per task until task_completed.
    token_buffer: dict[str, str] = {}
    # approval_queue receives approval_required payloads from the listener.
    approval_queue: asyncio.Queue[dict] = asyncio.Queue()

    # ── SSE event renderer ────────────────────────────────────────────────────

    async def _handle_event(event: str, data: dict) -> None:
        task_id = data.get("task_id", "")

        if event == "agent_started":
            agent = data.get("agent", "")
            _console.print(f"  [dim]◎ {agent} running…[/dim]")

        elif event == "tool_called":
            tool = data.get("tool", "")
            params = data.get("params") or {}
            _console.print(f"  [dim]· {tool}({_fmt_params(params)})[/dim]")

        elif event == "tool_result":
            tool = data.get("tool", "")
            success = data.get("success", True)
            style = "dim green" if success else "dim red"
            icon = "✓" if success else "✗"
            _console.print(f"  [{style}]{icon} {tool}[/{style}]")

        elif event == "token":
            token_buffer[task_id] = token_buffer.get(task_id, "") + data.get("text", "")

        elif event == "task_completed":
            sys.stdout.write("\a")
            sys.stdout.flush()
            output = token_buffer.pop(task_id, "")
            if not output:
                # Multi-agent path — fetch from ledger
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
            if output:
                _console.print(
                    Panel(
                        Markdown(output),
                        title="[bold green]north[/bold green]",
                        border_style="green",
                        padding=(1, 2),
                    )
                )

        elif event == "task_failed":
            sys.stdout.write("\a")
            sys.stdout.flush()
            token_buffer.pop(task_id, None)
            error = data.get("error", "Task failed.")
            _console.print(
                Panel(Text(error, style="red"), title="[red]north — failed[/red]", border_style="red")
            )

        elif event == "task_cancelled":
            token_buffer.pop(task_id, None)
            _console.print("[dim]Task cancelled.[/dim]")

        elif event == "approval_required":
            await approval_queue.put(data)
            _console.print(
                Panel(
                    Text(data.get("message", ""), style="white"),
                    title=f"[bold yellow]? {data.get('title', 'Approval Required')}[/bold yellow]",
                    border_style="yellow",
                    padding=(1, 2),
                )
            )
            options = data.get("options") or ["Approve", "Reject"]
            for i, opt in enumerate(options, 1):
                _console.print(f"  [{i}] {opt}")
            _console.print("[dim]  — press Enter to respond —[/dim]")

    # ── Global SSE listener ───────────────────────────────────────────────────

    async def _listen() -> None:
        while True:
            try:
                async with httpx.AsyncClient() as client:
                    async with client.stream(
                        "GET",
                        f"{base_url}/stream",
                        headers=headers,
                        timeout=None,
                    ) as resp:
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
                # Reconnect after brief pause (server restart, network blip)
                await asyncio.sleep(2)

    # ── Approval handler ──────────────────────────────────────────────────────

    async def _handle_pending_approval() -> None:
        if approval_queue.empty():
            return
        data = await approval_queue.get()
        options = data.get("options") or ["Approve", "Reject"]
        raw = (await session.prompt_async(" ❯ ")).strip()
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

    # ── Main loop ─────────────────────────────────────────────────────────────

    _console.print(
        Panel(
            Text("Type your message and press Enter · Ctrl+C or 'exit' to quit", style="dim"),
            title="[bold]★ north[/bold]",
            border_style="bright_black",
        )
    )

    listener = asyncio.create_task(_listen())

    with patch_stdout():
        while True:
            # Pending approval takes priority — capture the response first.
            if not approval_queue.empty():
                await _handle_pending_approval()
                continue

            try:
                text = await session.prompt_async(_prompt_tokens)
            except KeyboardInterrupt:
                _console.print("\n[dim]Ctrl+C — type 'exit' to quit.[/dim]")
                continue
            except EOFError:
                _console.print("[dim]Goodbye.[/dim]")
                break

            text = text.strip()
            if not text:
                continue
            if text.lower() in ("exit", "quit", "bye"):
                _console.print("[dim]Goodbye.[/dim]")
                break

            # If an approval arrived while the user was typing, handle it first.
            if not approval_queue.empty():
                await _handle_pending_approval()

            # Submit the task
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
            except httpx.ConnectError:
                _console.print("[red]Cannot reach north server. Is it running?[/red]")
                continue
            except Exception as exc:
                _console.print(f"[red]Error submitting task: {exc}[/red]")
                continue

    listener.cancel()
    try:
        await listener
    except asyncio.CancelledError:
        pass
