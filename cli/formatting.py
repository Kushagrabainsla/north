"""Pure presentation helpers for the north CLI and TUI.

Formatting and rendering functions with no I/O and no shared state - they take
values and return strings or Rich renderables. Kept out of `cli/main.py` and
`cli/tui.py` so those files hold commands and the App, not display plumbing (§4.1).
"""

from __future__ import annotations

import base64
import re
import subprocess
import sys

from rich.box import ROUNDED
from rich.table import Table
from rich.text import Text

from cli.constants import _FILL_COLOURS, _MARKUP_RE, _SLASH_COMMANDS


def _reconstruct_task_output(entries: list[dict]) -> str:
    """Join the ``agent_completed`` outputs from ledger *entries* into one string."""
    return "\n\n".join(e["output"] for e in entries if e.get("action") == "agent_completed" and e.get("output"))


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


def _short_model(model: str) -> str:
    """Trim a router model id (e.g. 'meta-llama/llama-4-scout-17b:free') to a
    compact label for the info bar."""
    name = model.rsplit("/", 1)[-1].removesuffix(":free")
    return name if len(name) <= 28 else name[:27] + "…"


def _compute_suggestion(value: str, history: list[str]) -> str | None:
    """Ghost-text completion for the prompt: slash commands when the line starts
    with '/', otherwise the most recent matching history entry."""
    if not value:
        return None
    if value.startswith("/"):
        for cmd in _SLASH_COMMANDS:
            if cmd.startswith(value) and cmd != value:
                return cmd
        return None
    for past in reversed(history):
        if past.startswith(value) and past != value:
            return past
    return None


def _strip_markup(s: str) -> str:
    """Remove Textual console-markup tags so a segment's display width can be
    measured. Only well-formed [tag] / [/tag] spans are removed."""
    return _MARKUP_RE.sub("", s)


def _to_prose(md: str) -> str:
    """Flatten markdown to clean terminal prose: drop ``` fences, heading hashes,
    and bold/italic/inline-code markers, while preserving code-block *content*
    and list structure verbatim.

    NOTE: not for the chat view - assistant messages render through a real
    markdown engine (RichMarkdown / the streaming Markdown widget) so tables and
    lists survive. This flattener has no table support and is kept only for
    plain-text sinks (exports, logs) where there is no renderer to defer to."""
    out: list[str] = []
    in_code = False
    for line in md.split("\n"):
        if line.lstrip().startswith("```"):
            in_code = not in_code  # drop the fence marker line itself
            continue
        if in_code:
            out.append(line)  # preserve code lines exactly
            continue
        line = re.sub(r"^\s*#{1,6}\s*", "", line)  # heading hashes
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)  # **bold**
        line = re.sub(r"__(.+?)__", r"\1", line)  # __bold__
        line = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", line)  # *italic*
        line = re.sub(r"`([^`]+)`", r"\1", line)  # `inline code`
        out.append(line)
    return "\n".join(out)


def _fmt_tokens(n: int) -> str:
    """Compact token count: 940 → '940', 12_400 → '12.4K', 200_000 → '200K'."""
    if n < 1000:
        return str(n)
    k = n / 1000
    return f"{k:.0f}K" if k >= 100 or k == int(k) else f"{k:.1f}K"


def _fmt_elapsed(seconds: float) -> str:
    """Elapsed session time: '0:42', '12:05', '1:03:20'."""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _fill_bar(fraction: float, width: int = 10) -> str:
    """Block-character fill bar coloured green→yellow→orange→red by fraction."""
    fraction = max(0.0, min(1.0, fraction))
    colour = next(c for limit, c in _FILL_COLOURS if fraction < limit)
    filled = int(round(fraction * width))
    bar = "█" * filled + "░" * (width - filled)
    return f"[{colour}]{bar}[/{colour}]"


def _build_steps_table(steps: list[tuple[str, str, bool]]) -> Table:
    """Render pipeline steps as a borderless table. Each step is (icon, label, active)."""
    t = Table.grid(padding=(0, 2))
    t.add_column(width=1)
    t.add_column()
    for icon, label, active in steps:
        if active:
            label_text = Text.from_markup(label) if "[" in label else Text(label, style="white")
            t.add_row(
                Text(icon, style="white"),
                label_text,
            )
        else:
            icon_style = "dim red" if icon == "✗" else "dim green"
            label_text = Text.from_markup(label) if "[" in label else Text(label)
            label_text.stylize("dim")
            t.add_row(
                Text(icon, style=icon_style),
                label_text,
            )
    return t


def _format_jobs_table(jobs: list[dict]) -> Table:
    """Render a styled table of background and scheduled jobs."""
    t = Table(title="Scheduled & Background Jobs", box=ROUNDED, header_style="bold cyan")
    t.add_column("ID", style="dim", width=12)
    t.add_column("Agent", style="white")
    t.add_column("Task", style="bright_black", max_width=32)
    t.add_column("Status", style="green")
    t.add_column("Priority", justify="right")
    t.add_column("Scheduled / Created", style="dim")
    if not jobs:
        t.add_row("-", "none", "No active or scheduled jobs", "-", "-", "-")
        return t
    for j in jobs:
        status = j.get("status", "pending")
        status_style = "green" if status == "completed" else "yellow" if status == "running" else "dim"
        t.add_row(
            str(j.get("job_id", ""))[:10],
            str(j.get("agent", "")),
            str(j.get("task", ""))[:30],
            Text(status, style=status_style),
            str(j.get("priority", 0)),
            str(j.get("scheduled_at") or j.get("created_at") or "")[:19].replace("T", " "),
        )
    return t


def _format_plan_table(plan_steps: list[dict], dod_evals: list[dict] | None = None) -> Table:
    """Render an interactive execution plan and Definition of Done evaluation table."""
    t = Table(title="Execution Plan & Step Status", box=ROUNDED, header_style="bold green")
    t.add_column("Step", style="dim", width=6)
    t.add_column("Agent / Phase", style="cyan", width=18)
    t.add_column("Description / Task", style="white", max_width=40)
    t.add_column("Status", style="green", width=14)

    if not plan_steps:
        t.add_row("-", "none", "No plan steps registered for current task", "pending")
    else:
        for s in plan_steps:
            st = s.get("status", "pending")
            st_style = (
                "green"
                if st in ("completed", "done")
                else "yellow"
                if st == "in_progress"
                else "red"
                if st == "failed"
                else "dim"
            )
            icon = (
                "✓ "
                if st in ("completed", "done")
                else "▶ "
                if st == "in_progress"
                else "✗ "
                if st == "failed"
                else "○ "
            )
            t.add_row(
                str(s.get("step_id", s.get("id", ""))),
                str(s.get("agent", s.get("phase", ""))),
                str(s.get("task", s.get("description", ""))),
                Text(icon + st, style=st_style),
            )

    if dod_evals:
        t.add_section()
        for d in dod_evals:
            passed = d.get("passed", False)
            reasons = d.get("reasons") or []
            summary = ", ".join(reasons) if reasons else ("criteria met" if passed else "criteria unmet")
            icon = "✓ " if passed else "✗ "
            style = "green" if passed else "yellow"
            t.add_row("DoD", "Definition of Done", summary, Text(icon + ("PASS" if passed else "UNMET"), style=style))

    return t


def _format_turn_summary(turn: dict) -> str:
    """Format a compact, Claude Code-style one-line collapsible summary for a turn."""
    parts = []
    agents = turn.get("agents") or ([turn.get("domain")] if turn.get("domain") else [])
    agent_str = ", ".join(agents) if agents else "general"

    tools = turn.get("tools") or []
    tool_count = len(tools)
    if tool_count > 0:
        tool_counts: dict[str, int] = {}
        for t in tools:
            name = t.get("tool", "tool")
            tool_counts[name] = tool_counts.get(name, 0) + 1
        tool_breakdown = ", ".join(f"{count} {name}" if count > 1 else name for name, count in tool_counts.items())
        parts.append(f"{tool_count} tool{'s' if tool_count != 1 else ''} ({tool_breakdown})")
    elif agent_str:
        parts.append(f"{agent_str} agent")

    turn_dur = turn.get("turn_duration", 0.0)
    thought_dur = turn.get("thought_duration", 0.0)
    thought_toks = turn.get("thought_tokens", 0)
    if thought_dur > 0 or thought_toks > 0:
        dur_str = f"{thought_dur:.1f}s" if thought_dur >= 0.1 else f"{thought_dur:.2f}s"
        parts.append(f"{dur_str} thoughts")
    elif turn_dur > 0:
        dur_str = f"{turn_dur:.1f}s" if turn_dur >= 0.1 else f"{turn_dur:.2f}s"
        parts.append(dur_str)

    verifs = turn.get("verifications") or []
    if verifs:
        passed_count = sum(1 for v in verifs if v.get("passed"))
        parts.append(f"{passed_count}/{len(verifs)} checks passed")

    summary_content = " · ".join(parts) if parts else "direct answer"
    return f"  [bold cyan]▶[/bold cyan] [dim]{summary_content}[/dim] [bright_black]· Ctrl+O to expand[/bright_black]"


def _format_turn_details(turn: dict) -> list[str]:
    """Format an expanded, detailed step-by-step breakdown of everything the system did."""
    lines = [
        "  [bold cyan]▼[/bold cyan] [cyan]Execution Details[/cyan] [bright_black](Ctrl+O to collapse)[/bright_black]"
    ]

    domain = turn.get("domain")
    if domain:
        flag = " [dim](complex)[/dim]" if turn.get("is_consequential") else " [dim](direct)[/dim]"
        lines.append(f"    [dim green]✓[/dim green]  [dim]classified:[/dim] [cyan]{domain}[/cyan]{flag}")

    agents = turn.get("agents") or []
    if agents:
        lines.append(f"    [dim green]✓[/dim green]  [dim]plan ready:[/dim] [cyan]{', '.join(agents)}[/cyan]")
        model_str = f" [dim]on [cyan]{turn.get('model')}[/cyan][/dim]" if turn.get("model") else ""
        lines.append(f"    [bright_black]◎[/bright_black]  [cyan]{', '.join(agents)}[/cyan] [dim]agent running{model_str}…[/dim]")

    turn_dur = turn.get("turn_duration", 0.0)
    thought_dur = turn.get("thought_duration", 0.0)
    thought_toks = turn.get("thought_tokens", 0)
    if thought_dur > 0 or thought_toks > 0:
        dur_str = f"{thought_dur:.1f}s" if thought_dur >= 0.1 else f"{thought_dur:.2f}s"
        lines.append(f"    [dim cyan]🧠 Thought for {dur_str} ({thought_toks} tokens · Ctrl+T to view)[/dim cyan]")
    elif turn_dur > 0:
        dur_str = f"{turn_dur:.1f}s" if turn_dur >= 0.1 else f"{turn_dur:.2f}s"
        lines.append(f"    [dim green]✓[/dim green]  [dim]latency:[/dim] [cyan]{dur_str}[/cyan]")

    for t in turn.get("tools") or []:
        tool = t.get("tool", "")
        success = t.get("success", True)
        icon = "[dim green]✓[/dim green]" if success else "[dim red]✗[/dim red]"
        params_str = t.get("params_str") or ""
        suffix = f"[bright_black]({params_str})[/bright_black]" if params_str else ""
        res = t.get("result") or ("ok" if success else "failed")
        dur = t.get("duration")
        dur_str = f" [dim]({dur:.2f}s)[/dim]" if dur is not None else ""
        lines.append(f"    {icon}  [cyan]{tool}[/cyan]{suffix} [dim]→ {res}[/dim]{dur_str}")

    for v in turn.get("verifications") or []:
        cmd = v.get("command", "")
        passed = v.get("passed", False)
        icon = "[dim green]✓[/dim green]" if passed else "[dim red]✗[/dim red]"
        verdict = "[green]PASS[/green]" if passed else "[red]FAIL[/red]"
        lines.append(f"    {icon}  [dim]verify({cmd})[/dim] {verdict}")

    return lines


def _format_help_table(commands: dict[str, str]) -> Table:
    """Render a styled help command palette table."""
    t = Table(title="Available Slash Commands & Keybindings", box=ROUNDED, header_style="bold cyan")
    t.add_column("Command / Key", style="cyan", width=18)
    t.add_column("Description", style="white")
    for cmd, desc in commands.items():
        t.add_row(cmd, desc)
    t.add_section()
    t.add_row("Ctrl+O", "Toggle compact summary vs detailed execution steps")
    t.add_row("Ctrl+T", "Toggle live Chain-of-Thought reasoning drawer")
    t.add_row("Ctrl+I", "Open interactive Tool Call & Diff Inspector modal")
    t.add_row("Ctrl+P", "Open Execution Plan & Step Tree Cockpit modal")
    t.add_row("Esc", "Open In-Flight Action & Steer Menu during active tasks")
    t.add_row("Ctrl+D", "Toggle push-to-talk voice dictation (Whisper)")
    t.add_row("Ctrl+G", "Open full prompt in external $EDITOR (vi/nano)")
    t.add_row("Ctrl+Y", "Copy last assistant response to clipboard")
    t.add_row("Ctrl+C", "Interrupt running task (press twice to exit)")
    t.add_row("Tab", "Accept ghost-text autocomplete suggestion")
    t.add_row("Up / Down", "Navigate prompt input history")
    return t


def _copy_to_clipboard(text: str) -> bool:
    """Copy text to the system clipboard via OSC 52 or platform utilities."""
    if not text:
        return False
    # Attempt OSC 52 escape sequence for modern terminals
    try:
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        osc52 = f"\033]52;c;{encoded}\a"
        sys.stdout.write(osc52)
        sys.stdout.flush()
    except Exception:
        pass
    # Also attempt platform CLI tool
    if sys.platform == "darwin":
        try:
            p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
            p.communicate(input=text.encode("utf-8"))
            return True
        except Exception:
            pass
    elif sys.platform.startswith("linux"):
        for tool in (["wl-copy"], ["xclip", "-selection", "clipboard"]):
            try:
                p = subprocess.Popen(tool, stdin=subprocess.PIPE)
                p.communicate(input=text.encode("utf-8"))
                return True
            except Exception:
                continue
    return True
