"""Typer CLI for north.

Commands talk exclusively to the Orchestrator API on port 8000.

Usage:
    north task "Help me plan my week"
    north task cancel <id>
    north tasks
    north context show north_stars
    north context edit judgement_rules
    north context add --text "I prefer mornings for deep work"
    north context add --url "https://example.com/article"
    north context add --file resume.pdf
    north ledger [--task <id>] [--agent <name>] [--source <src>]
    north jobs [--status pending]
    north job cancel <id>
    north agents
    north agent run <name> <task>
    north inference costs [--period week] [--agent finance]
    north inference models
    north tools confidence [--agent health]
    north config set <key> <value>

See docs/CODING_STYLE.md Section 8 and README Section 10.2.
"""

from __future__ import annotations

import asyncio
import json
import os
import readline  # noqa: F401 — enables arrow-key/history editing in input()
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import httpx
import typer
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

_console = Console(force_terminal=sys.stdout.isatty())


def _setup_readline() -> None:
    """Configure readline key bindings for comfortable terminal editing."""
    try:
        using_libedit = "libedit" in (getattr(readline, "__doc__", "") or "")
        if using_libedit:
            readline.parse_and_bind("bind -e")
            # libedit uses ^[ for ESC (not \e) and different function names.
            # Bind every common sequence so it works regardless of terminal app.
            for seq, fn in [
                ("^[b",      "ed-prev-word"),   # ESC b  — Terminal.app, VS Code
                ("^[f",      "em-next-word"),   # ESC f
                ("^[[1;3D",  "ed-prev-word"),   # opt+left  iTerm2
                ("^[[1;3C",  "em-next-word"),   # opt+right iTerm2
                ("^[[3D",    "ed-prev-word"),   # opt+left  some terminals
                ("^[[3C",    "em-next-word"),   # opt+right some terminals
                ("^[^[[D",   "ed-prev-word"),   # opt+left  VS Code
                ("^[^[[C",   "em-next-word"),   # opt+right VS Code
            ]:
                try:
                    readline.parse_and_bind(f'bind "{seq}" {fn}')
                except Exception:
                    pass
        else:
            for binding in [
                r'"\e[1;3D": backward-word',   # opt+left  iTerm2
                r'"\e[1;3C": forward-word',    # opt+right iTerm2
                r'"\eb": backward-word',       # ESC b
                r'"\ef": forward-word',        # ESC f
            ]:
                try:
                    readline.parse_and_bind(binding)
                except Exception:
                    pass
    except Exception:
        pass


from utils.security import load_secret

app = typer.Typer(
    name="north",
    help="north — Personal Life Operating System CLI",
    no_args_is_help=True,
    add_completion=False,
)

_BASE_URL = "http://127.0.0.1:8000"
_TIMEOUT = 30.0


def _headers() -> dict[str, str]:
    return {"X-North-Secret": load_secret()}


def _api(method: str, path: str, **kwargs: object) -> httpx.Response:
    """Execute a synchronous HTTP call to the Orchestrator API."""
    url = f"{_BASE_URL}{path}"
    try:
        response = httpx.request(method, url, headers=_headers(), timeout=_TIMEOUT, **kwargs)  # type: ignore[arg-type]
        response.raise_for_status()
        return response
    except httpx.ConnectError:
        typer.secho(
            "ERROR: Cannot reach the north server. Is it running?\n"
            "  uvicorn orchestrator.app:app --port 8000",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    except httpx.HTTPStatusError as exc:
        typer.secho(
            f"ERROR: Server returned {exc.response.status_code}: {exc.response.text}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)


# ── task ─────────────────────────────────────────────────────────────────────

task_app = typer.Typer(help="Task management.", no_args_is_help=True)
app.add_typer(task_app, name="task")


@task_app.callback(invoke_without_command=True)
def task_default(
    ctx: typer.Context,
    prompt: Optional[str] = typer.Argument(None, help="Prompt to submit as a new task."),
) -> None:
    """Submit a task and stream results live."""
    if ctx.invoked_subcommand is not None:
        return
    if prompt is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()
    _run_task(prompt)


@task_app.command("cancel")
def cancel_task(
    task_id: str = typer.Argument(..., help="Task ID to cancel."),
) -> None:
    """Cancel a pending task."""
    _api("DELETE", f"/orchestrator/task/{task_id}")
    typer.secho(f"✓ Task {task_id} cancelled.", fg=typer.colors.YELLOW)


# ── tasks ─────────────────────────────────────────────────────────────────────

@app.command("tasks")
def list_tasks() -> None:
    """List all currently pending tasks."""
    response = _api("GET", "/orchestrator/tasks")
    tasks = response.json()
    if not tasks:
        typer.echo("No pending tasks.")
        return
    for t in tasks:
        typer.secho(f"  {t['task_id']}", fg=typer.colors.CYAN, nl=False)
        typer.echo(f"  {t['status']}  {t['created_at']}")


# ── chat ─────────────────────────────────────────────────────────────────────

@app.command("chat")
def chat(
    workspace: Optional[str] = typer.Option(
        None, "--workspace", "-w",
        help="Root directory the code agent can read/write (e.g. ./my-project).",
    ),
) -> None:
    """Interactive chat with north — live pipeline steps and markdown responses."""
    _chat_loop(workspace=workspace)


def _strip_history_prefix(prompt: str) -> str:
    """Extract the raw user message from a prompt that may contain injected history."""
    marker = "[Current message]\n"
    idx = prompt.find(marker)
    return prompt[idx + len(marker):] if idx != -1 else prompt


def _load_history_from_ledger(limit: int = 20) -> list[tuple[str, str]]:
    """Reconstruct recent conversation turns from the ledger."""
    try:
        resp = _api("GET", "/orchestrator/ledger?limit=300")
        entries = resp.json()
    except Exception:
        return []

    tasks: dict[str, dict] = {}
    for e in entries:
        tid = e.get("task_id")
        if not tid:
            continue
        if tid not in tasks:
            tasks[tid] = {"user": None, "assistant": None, "ts": ""}
        action = e.get("action", "")
        if action == "task_received" and e.get("input"):
            tasks[tid]["user"] = _strip_history_prefix(e["input"])
            tasks[tid]["ts"] = e.get("timestamp", "")
        elif action == "agent_completed" and e.get("output"):
            # Last agent's output wins if multiple agents ran
            tasks[tid]["assistant"] = e["output"]

    pairs = [
        (t["user"], t["assistant"])
        for t in sorted(tasks.values(), key=lambda x: x["ts"])
        if t["user"] and t["assistant"]
    ]
    return pairs[-limit:]


def _inject_history(prompt: str, history: list[tuple[str, str]]) -> str:
    """Prepend the last N conversation turns so the agent keeps context."""
    if not history:
        return prompt
    turns = "\n".join(f"User: {u}\nAssistant: {a}" for u, a in history[-5:])
    return f"[Conversation so far]\n{turns}\n\n[Current message]\n{prompt}"


def _chat_loop(workspace: Optional[str] = None) -> None:
    """Inner chat REPL — called by both `chat` command and `start` command."""
    _setup_readline()
    subtitle = f"workspace: {workspace}" if workspace else "Type your message and press Enter. Ctrl+C or 'exit' to quit."
    _console.print(
        Panel(
            Text(subtitle, style="dim"),
            title="[bold]★ north[/bold]",
            border_style="bright_black",
        )
    )
    # \001/\002 wrap non-printing ANSI bytes so readline measures prompt width correctly.
    # Without them Option+Left overshoots and eats the ❯ character.
    _STRATEGY_COLORS = {"eco": "\x1b[1;32m", "cruise": "\x1b[1;36m", "sport": "\x1b[1;33m"}

    def _chat_prompt() -> str:
        from config.settings import settings as _settings
        from config.strategy import NorthSettings as _NS
        try:
            mode = _NS(_settings.north_home / "settings.json").strategy.value
        except Exception:
            mode = "cruise"
        color = _STRATEGY_COLORS.get(mode, "\x1b[1;36m")
        return f"\n\001{color}\002[{mode}] ❯ \001\x1b[0m\002"

    history = _load_history_from_ledger(limit=20)
    while True:
        try:
            prompt = input(_chat_prompt()).strip()
        except (KeyboardInterrupt, EOFError):
            _console.print("\n[dim]Goodbye.[/dim]")
            break
        if not prompt or prompt.lower() in ("exit", "quit", "bye"):
            _console.print("[dim]Goodbye.[/dim]")
            break
        full_prompt = _inject_history(prompt, history)
        output = _run_task(full_prompt, workspace=workspace)
        if output and output != "Task completed.":
            history.append((prompt, output))
            if len(history) > 20:
                history = history[-20:]


# ── shared task runner ────────────────────────────────────────────────────────

_STEP_ICONS: dict[str, str] = {
    "classifying":           "◎",
    "classified":            "✓",
    "classified_as_trivial": "✓",
    "north_star_checking":   "★",
    "north_star_aligned":    "✓",
    "north_star_conflict":   "!",
    "routing":               "⇢",
    "routed":                "✓",
    "executing":             "▶",
    "agent_started":         "◎",
    "agent_completed":       "✓",
    "tool_called":           "⚙",
    "tool_result":           "✓",
}

_STEP_LABELS: dict[str, str] = {
    "classifying":           "Classifying…",
    "classified":            "Classified",
    "classified_as_trivial": "Quick task — skipping north star check",
    "north_star_checking":   "Checking north stars…",
    "north_star_aligned":    "Aligned with goals",
    "north_star_conflict":   "North star conflict — check approvals",
    "routing":               "Planning execution…",
    "routed":                "Execution plan ready",
    "executing":             "Running agents…",
}


def _build_steps_table(steps: list[tuple[str, str, bool]]) -> Table:
    """Render pipeline steps as a borderless table. Each step is (icon, label, active)."""
    t = Table.grid(padding=(0, 1))
    t.add_column(width=2)
    t.add_column()
    for icon, label, active in steps:
        if active:
            t.add_row(
                Text(icon, style="bold blue"),
                Text(label, style="bold white"),
            )
        else:
            t.add_row(
                Text(icon, style="dim green"),
                Text(label, style="dim"),
            )
    return t


def _run_task(prompt: str, workspace: Optional[str] = None) -> str:
    """Submit prompt, stream SSE pipeline steps live, then render the response. Returns output text."""
    body: dict = {"prompt": prompt}
    if workspace:
        body["workspace"] = workspace
    try:
        resp = _api("POST", "/orchestrator/task", json=body)
    except SystemExit:
        return ""
    task_id = resp.json()["task_id"]

    steps: list[tuple[str, str, bool]] = []

    def _make_renderable() -> Panel:
        return Panel(
            _build_steps_table(steps) if steps else Text("Starting…", style="dim"),
            title=f"[dim]{task_id}[/dim]",
            border_style="bright_black",
        )

    url = f"{_BASE_URL}/orchestrator/stream/{task_id}"
    output_text: str = ""
    failed_msg: str = ""

    try:
        with Live(_make_renderable(), console=_console, refresh_per_second=8) as live:
            with httpx.stream("GET", url, headers=_headers(), timeout=None) as stream:
                current_event = ""
                for line in stream.iter_lines():
                    if line.startswith("event:"):
                        current_event = line[6:].strip()
                    elif line.startswith("data:"):
                        try:
                            data = json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            continue

                        event = current_event or data.get("event", "")

                        # Mark previous active step done
                        if steps:
                            icon, label, _ = steps[-1]
                            steps[-1] = (icon, label, False)

                        if event == "agent_started":
                            agent = data.get("agent", "agent")
                            steps.append(("◎", f"{agent} agent running…", True))
                        elif event == "agent_completed":
                            agent = data.get("agent", "agent")
                            summary = data.get("summary", "")
                            label = f"{agent}: {summary}" if summary else f"{agent} agent done"
                            steps.append(("✓", label, True))
                        elif event == "tool_called":
                            tool = data.get("tool", "tool")
                            steps.append(("⚙", f"  {tool}…", True))
                        elif event == "tool_result":
                            tool = data.get("tool", "tool")
                            success = data.get("success", True)
                            steps.append(("✓" if success else "✗", f"  {tool} done", True))
                        elif event == "approval_required":
                            steps.append(("?", "Approval required", False))
                            live.update(_make_renderable())
                            live.stop()
                            _console.print()
                            _console.print(Panel(
                                Text(data.get("message", ""), style="white"),
                                title=f"[bold yellow]? {data.get('title', 'Approval Required')}[/bold yellow]",
                                border_style="yellow",
                                padding=(1, 2),
                            ))
                            options = data.get("options", ["Approve", "Reject"])
                            for i, opt in enumerate(options, 1):
                                _console.print(f"  [{i}] {opt}")
                            raw_choice = input("\n ❯ ").strip()
                            try:
                                idx = int(raw_choice) - 1
                                chosen = options[idx] if 0 <= idx < len(options) else raw_choice
                            except ValueError:
                                chosen = raw_choice or options[0]
                            decision = "approved" if chosen.lower() in ("approve", "approved", "yes") else \
                                       "rejected" if chosen.lower() in ("reject", "rejected", "no") else \
                                       "answered"
                            try:
                                _api("POST", "/orchestrator/approval/respond", json={
                                    "card_id": data.get("card_id", ""),
                                    "task_id": task_id,
                                    "agent": data.get("agent", ""),
                                    "decision": decision,
                                    "chosen_option": chosen,
                                })
                            except SystemExit:
                                pass
                            steps[-1] = ("✓" if decision != "rejected" else "✗", f"Approval: {chosen}", False)
                            # Do NOT restart Live — cursor is now past the approval panel.
                            # Subsequent live.update() calls on a stopped Live are no-ops;
                            # task_completed / task_cancelled will break the loop.
                            current_event = ""
                            continue
                        elif event in _STEP_LABELS:
                            steps.append((_STEP_ICONS.get(event, "·"), _STEP_LABELS[event], True))

                        live.update(_make_renderable())

                        if event == "task_completed":
                            if steps:
                                icon, label, _ = steps[-1]
                                steps[-1] = (icon, label, False)
                            live.update(_make_renderable())
                            break
                        if event == "task_failed":
                            failed_msg = data.get("error", "Task failed.")
                            if steps:
                                icon, label, _ = steps[-1]
                                steps[-1] = ("✗", label, False)
                            live.update(_make_renderable())
                            break
                        if event == "task_cancelled":
                            if steps:
                                icon, label, _ = steps[-1]
                                steps[-1] = (icon, label, False)
                            live.update(_make_renderable())
                            _console.print("[dim]Task cancelled.[/dim]")
                            break
                        current_event = ""

    except KeyboardInterrupt:
        _console.print("[dim]Interrupted.[/dim]")
        return ""

    if failed_msg:
        _console.print(Panel(
            Text(failed_msg, style="red"),
            title="[red]north — failed[/red]",
            border_style="red",
        ))
        return ""

    # Fetch the actual output from the ledger
    try:
        ledger_resp = _api("GET", f"/orchestrator/ledger?task_id={task_id}&limit=20")
        entries = ledger_resp.json()
        outputs = [
            e["output"] for e in entries
            if e.get("action") == "agent_completed" and e.get("output")
        ]
        output_text = "\n\n".join(outputs) if outputs else "Task completed."
    except Exception:
        output_text = "Task completed."

    _console.print(Panel(
        Markdown(output_text),
        title="[bold green]north[/bold green]",
        border_style="green",
        padding=(1, 2),
    ))
    return output_text


# ── stream (raw) ──────────────────────────────────────────────────────────────

@app.command("stream")
def stream_task(
    task_id: str = typer.Argument(..., help="Task ID to stream raw events for."),
) -> None:
    """Stream raw SSE events for a task (debug view)."""
    url = f"{_BASE_URL}/orchestrator/stream/{task_id}"
    _console.print(f"[dim]Streaming {task_id} — Ctrl+C to stop[/dim]\n")
    try:
        with httpx.stream("GET", url, headers=_headers(), timeout=None) as response:
            for line in response.iter_lines():
                if line.startswith("event:"):
                    _console.print(f"[cyan]{line}[/cyan]")
                elif line.startswith("data:"):
                    try:
                        data = json.loads(line[5:].strip())
                        _console.print_json(json.dumps(data))
                    except json.JSONDecodeError:
                        _console.print(line)
    except KeyboardInterrupt:
        _console.print("\n[dim]Stream closed.[/dim]")


# ── context ──────────────────────────────────────────────────────────────────

context_app = typer.Typer(help="Manage context documents.", no_args_is_help=True)
app.add_typer(context_app, name="context")

_VALID_DOCS = ["public", "private", "privacy_rules", "judgement_rules", "north_stars"]


@context_app.command("show")
def context_show(
    document: str = typer.Argument(..., help=f"Document name: {', '.join(_VALID_DOCS)}"),
) -> None:
    """Print the contents of a context document."""
    response = _api("GET", f"/orchestrator/context/{document}")
    data = response.json()
    typer.echo(data.get("content") or "(empty)")


@context_app.command("edit")
def context_edit(
    document: str = typer.Argument(..., help=f"Document name: {', '.join(_VALID_DOCS)}"),
) -> None:
    """Open a context document in $EDITOR."""
    from config.settings import settings

    doc_name = f"{document}.md" if not document.endswith(".md") else document
    doc_path = settings.north_home / "context" / doc_name
    doc_path.parent.mkdir(parents=True, exist_ok=True)
    if not doc_path.exists():
        doc_path.touch()

    editor = os.environ.get("EDITOR", "nano")
    subprocess.call([editor, str(doc_path)])


@context_app.command("add")
def context_add(
    text: Optional[str] = typer.Option(None, "--text", help="Raw text to inject."),
    url: Optional[str] = typer.Option(None, "--url", help="URL to fetch and inject."),
    file: Optional[Path] = typer.Option(None, "--file", help="File to inject."),
) -> None:
    """Inject context from text, a URL, or a file."""
    if file is not None:
        if not file.exists():
            typer.secho(f"ERROR: File not found: {file}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
        with open(file, "rb") as fh:
            files = {"file": (file.name, fh, "application/octet-stream")}
            response = _api("POST", "/orchestrator/context/add", files=files)  # type: ignore[arg-type]
    elif url is not None:
        response = _api("POST", "/orchestrator/context/add", data={"url": url})
    elif text is not None:
        response = _api("POST", "/orchestrator/context/add", data={"text": text})
    else:
        typer.secho("Provide --text, --url, or --file.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    data = response.json()
    typer.secho(
        f"✓ Injected into {data.get('document', '?')} (source: {data.get('source', '?')})",
        fg=typer.colors.GREEN,
    )


# ── ledger ───────────────────────────────────────────────────────────────────

@app.command("ledger")
def show_ledger(
    limit: int = typer.Option(20, "--limit", "-n", help="Number of entries to show."),
    task_id: Optional[str] = typer.Option(None, "--task", help="Filter by task ID."),
    agent: Optional[str] = typer.Option(None, "--agent", help="Filter by agent name."),
    source: Optional[str] = typer.Option(None, "--source", help="Filter by source type."),
) -> None:
    """Show recent ledger entries."""
    params: dict[str, object] = {"limit": limit}
    if task_id:
        params["task_id"] = task_id
    if agent:
        params["agent"] = agent
    if source:
        params["source"] = source

    response = _api("GET", "/orchestrator/ledger", params=params)
    entries = response.json()
    if not entries:
        typer.echo("No ledger entries found.")
        return

    for entry in entries:
        ts = entry["timestamp"][:19].replace("T", " ")
        agent_str = f"  agent={entry['agent']}" if entry.get("agent") else ""
        typer.secho(f"[{ts}] ", fg=typer.colors.BRIGHT_BLACK, nl=False)
        typer.secho(f"{entry['source']:<18}", fg=typer.colors.CYAN, nl=False)
        typer.secho(f" {(entry.get('action') or ''):<40}", nl=False)
        status = entry.get("status")
        if status:
            colour = typer.colors.GREEN if status == "completed" else typer.colors.RED
            typer.secho(f" {status}", fg=colour, nl=False)
        typer.echo(agent_str)


# ── jobs ──────────────────────────────────────────────────────────────────────

@app.command("jobs")
def show_jobs(
    limit: int = typer.Option(20, "--limit", "-n", help="Number of jobs to show."),
    status: Optional[str] = typer.Option(None, "--status", help="Filter by status."),
) -> None:
    """Show scheduled jobs."""
    params: dict[str, object] = {"limit": limit}
    if status:
        params["status"] = status

    response = _api("GET", "/orchestrator/jobs", params=params)
    jobs = response.json()
    if not jobs:
        typer.echo("No jobs found.")
        return

    for job in jobs:
        typer.secho(f"  {job['job_id']:<36}", fg=typer.colors.CYAN, nl=False)
        typer.echo(f"  {job['status']:<12}  {job['type']:<10}  {job['agent']}")


job_app = typer.Typer(help="Job management.", no_args_is_help=True)
app.add_typer(job_app, name="job")


@job_app.command("cancel")
def cancel_job(
    job_id: str = typer.Argument(..., help="Job ID to cancel."),
) -> None:
    """Cancel a pending or running job."""
    _api("DELETE", f"/orchestrator/jobs/{job_id}")
    typer.secho(f"✓ Job {job_id} cancelled.", fg=typer.colors.YELLOW)


# ── agents ────────────────────────────────────────────────────────────────────

agent_app = typer.Typer(help="Agent management.", no_args_is_help=True)
app.add_typer(agent_app, name="agent")


@app.command("agents")
def list_agents_top() -> None:
    """List all registered domain-specialist agents."""
    _list_agents_impl()


@agent_app.command("list")
def list_agents() -> None:
    """List all registered domain-specialist agents."""
    _list_agents_impl()


def _list_agents_impl() -> None:
    response = _api("GET", "/orchestrator/agents")
    agents = response.json()
    if not agents:
        typer.echo("No agents registered.")
        return
    for a in agents:
        typer.secho(f"  {a['name']:<16}", fg=typer.colors.CYAN, nl=False)
        typer.echo(f"  domain={a['domain']:<12}  pool={a['model_pool']}")


@agent_app.command("create")
def create_agent(
    name: Optional[str] = typer.Option(None, "--name", help="Agent name (slug, lowercase)."),
    domain: Optional[str] = typer.Option(None, "--domain", help="Domain (e.g. health, finance)."),
    description: Optional[str] = typer.Option(None, "--description", help="One-line description."),
    model_pool: str = typer.Option("fast_cheap", "--pool", help="Model pool: reasoning / fast_cheap / high_volume."),
    output_dir: Optional[Path] = typer.Option(None, "--output-dir", help="Where to create the agent folder (default: ./agents/)."),
) -> None:
    """Interactively scaffold a new domain-specialist agent."""
    import re

    if name is None:
        name = typer.prompt("Agent name (slug, e.g. travel)")
    name = name.strip().lower().replace(" ", "_")
    if not re.match(r"^[a-z][a-z0-9_]*$", name):
        typer.secho("ERROR: Name must be lowercase letters, digits, underscores.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    if domain is None:
        domain = typer.prompt("Domain (e.g. travel, fitness, finance)", default=name)
    domain = domain.strip().lower()

    if description is None:
        description = typer.prompt("One-line description", default=f"Domain specialist for {domain}.")

    agentic = typer.confirm(
        "Use agentic loop? (yes = ReAct loop with tool calls; no = single LLM call)",
        default=True,
    )

    raw_tools = typer.prompt("Tools (comma-separated, or blank)", default="web_search")
    tools = [t.strip() for t in raw_tools.split(",") if t.strip()]

    raw_accepts = typer.prompt("Accepts task keywords (comma-separated, or blank)", default=domain)
    accepts = [a.strip() for a in raw_accepts.split(",") if a.strip()]

    # Determine output location
    if output_dir is None:
        # Try to detect project root (has pyproject.toml or agents/ folder)
        cwd = Path.cwd()
        if (cwd / "agents").is_dir():
            output_dir = cwd / "agents"
        else:
            output_dir = cwd
    else:
        output_dir = output_dir.resolve()

    agent_dir = output_dir / name
    if agent_dir.exists():
        typer.secho(f"ERROR: Directory already exists: {agent_dir}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    # Generate system.md via LLM
    typer.echo("Generating system prompt via LLM…")
    try:
        resp = _api(
            "POST",
            "/orchestrator/agent/create",
            json={
                "name": name,
                "domain": domain,
                "description": description,
                "model_pool": model_pool,
                "tools": tools,
                "accepts": accepts,
            },
        )
        system_prompt = resp.json()["system_prompt"]
    except Exception:
        system_prompt = (
            f"You are the {name.title()} Agent of north (Personal Life Operating System).\n"
            f"Your role is to specialise in {domain}-related tasks.\n\n"
            f"Description: {description}\n\n"
            f"Available tools: {', '.join(tools) if tools else 'none'}.\n"
        )

    # Write scaffold to disk
    agent_dir.mkdir(parents=True)
    (agent_dir / "prompts").mkdir()

    class_name = "".join(word.title() for word in name.split("_")) + "Agent"

    base_import = (
        "from agents.agentic_llm_agent import AgenticLLMAgent"
        if agentic else
        "from agents.llm_agent import LLMAgent"
    )
    base_class = "AgenticLLMAgent" if agentic else "LLMAgent"

    (agent_dir / "agent.py").write_text(
        f'"""{class_name} domain specialist.\n\nSee docs/CODING_STYLE.md Section 15.\n"""\n\n'
        f"from __future__ import annotations\n\n"
        f"{base_import}\n\n\n"
        f"class {class_name}({base_class}):\n"
        f'    """Domain specialist for {domain}."""\n',
        encoding="utf-8",
    )

    import yaml  # type: ignore[import-untyped]
    (agent_dir / "config.yaml").write_text(
        yaml.dump({
            "agent": name,
            "domain": domain,
            "model_pool": model_pool,
            "accepts": accepts,
            "output_format": "structured_json",
            "version": "1.0.0",
            "class_name": class_name,
        }, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )

    tools_data = {"tools": [{"name": t, "purpose": f"Used by {name} agent."} for t in tools]}
    (agent_dir / "tools.yaml").write_text(
        yaml.dump(tools_data, default_flow_style=False),
        encoding="utf-8",
    )

    (agent_dir / "prompts" / "system.md").write_text(system_prompt, encoding="utf-8")
    (agent_dir / "README.md").write_text(
        f"# {name.title()} Agent\n\n{description}\n\n"
        f"**Domain:** {domain}  \n**Pool:** {model_pool}  \n**Accepts:** {', '.join(accepts)}\n",
        encoding="utf-8",
    )

    typer.secho(f"\n✓ Agent scaffold created at {agent_dir}", fg=typer.colors.GREEN)
    typer.echo(f"  {agent_dir}/agent.py")
    typer.echo(f"  {agent_dir}/config.yaml")
    typer.echo(f"  {agent_dir}/tools.yaml")
    typer.echo(f"  {agent_dir}/prompts/system.md")
    typer.echo(f"\nRestart north to load the new agent.")


@agent_app.command("run")
def run_agent(
    name: str = typer.Argument(..., help="Agent name (health, finance, job, university)."),
    task: str = typer.Argument(..., help="Task description for the agent."),
) -> None:
    """Manually trigger a specific agent."""
    response = _api(
        "POST", "/orchestrator/agent/run", json={"agent": name, "task": task}
    )
    data = response.json()
    typer.secho(f"✓ Task submitted: {data['task_id']}", fg=typer.colors.GREEN)
    typer.echo(f"  Stream with:  north stream {data['task_id']}")


# ── inference ─────────────────────────────────────────────────────────────────

inference_app = typer.Typer(help="Inference cost and model info.", no_args_is_help=True)
app.add_typer(inference_app, name="inference")


@inference_app.command("costs")
def inference_costs(
    period: str = typer.Option("week", "--period", help="day / week / month"),
    agent: Optional[str] = typer.Option(None, "--agent", help="Filter by agent/component."),
) -> None:
    """Show inference cost summary."""
    params: dict[str, object] = {"period": period}
    if agent:
        params["agent"] = agent

    response = _api("GET", "/orchestrator/inference/costs", params=params)
    data = response.json()

    typer.secho(f"\nInference costs — {data['period']}", fg=typer.colors.BRIGHT_WHITE)
    typer.echo(f"  Total: ${data['total_cost_usd']:.6f}")

    if data.get("by_component"):
        typer.echo("\nBy component:")
        for comp, cost in sorted(data["by_component"].items(), key=lambda x: -x[1]):
            typer.secho(f"  {comp:<24}", fg=typer.colors.CYAN, nl=False)
            typer.echo(f"  ${cost:.6f}")

    if data.get("by_model"):
        typer.echo("\nBy model:")
        for model, cost in sorted(data["by_model"].items(), key=lambda x: -x[1]):
            typer.secho(f"  {model:<40}", fg=typer.colors.CYAN, nl=False)
            typer.echo(f"  ${cost:.6f}")


@inference_app.command("models")
def inference_models() -> None:
    """Show current model pool state."""
    response = _api("GET", "/orchestrator/inference/models")
    pools = response.json()
    for pool_name, pool_data in pools.items():
        typer.secho(f"\n  {pool_name}", fg=typer.colors.BRIGHT_WHITE)
        for model in pool_data.get("models", []):
            typer.echo(f"    {model}")


# ── dictate ───────────────────────────────────────────────────────────────────

@app.command("dictate")
def dictate(
    hotkey: str = typer.Option(
        "right_alt+space",
        "--hotkey",
        help="Hold-to-talk hotkey (pynput key names, plus-separated). Default: right_alt+space.",
    ),
    sample_rate: int = typer.Option(16000, "--sample-rate", help="Audio sample rate in Hz."),
) -> None:
    """Push-to-talk voice input. Hold the hotkey, speak, release to transcribe.

    Audio is captured via sounddevice, transcribed via OpenRouter Whisper,
    and submitted as a task to the Orchestrator. Press Ctrl+C to exit.
    """
    try:
        import numpy as np
        import sounddevice as sd
        from pynput import keyboard as kb
    except ImportError as exc:
        typer.secho(
            f"ERROR: Missing dependency: {exc}. Install with: uv add sounddevice pynput",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(1)

    # Parse hotkey string into a frozenset of pynput Key/KeyCode objects
    def _parse_key(part: str) -> object:
        part = part.strip()
        if hasattr(kb.Key, part):
            return getattr(kb.Key, part)
        return kb.KeyCode.from_char(part)

    required_keys: frozenset = frozenset(_parse_key(p) for p in hotkey.split("+"))
    held_keys: set = set()
    frames: list = []
    recording = False

    typer.secho(
        f"★ north dictate  (hold {hotkey} to record, Ctrl+C to quit)",
        fg=typer.colors.BRIGHT_WHITE, bold=True,
    )

    def _audio_callback(indata, frame_count, time_info, status):  # type: ignore[no-untyped-def]
        if recording:
            frames.append(indata.copy())

    stream = sd.InputStream(
        samplerate=sample_rate,
        channels=1,
        dtype="int16",
        callback=_audio_callback,
    )
    stream.start()

    def _on_press(key: object) -> None:
        nonlocal recording
        held_keys.add(key)
        if required_keys.issubset(held_keys) and not recording:
            frames.clear()
            recording = True
            typer.secho("  ● Recording…", fg=typer.colors.RED)

    def _on_release(key: object) -> None:
        nonlocal recording
        held_keys.discard(key)
        if recording and not required_keys.issubset(held_keys):
            recording = False
            typer.secho("  ■ Processing…", fg=typer.colors.YELLOW)
            _send_audio(frames[:], sample_rate)

    def _send_audio(captured: list, sr: int) -> None:
        if not captured:
            typer.echo("  (nothing recorded)")
            return

        audio_np = np.concatenate(captured, axis=0)
        # Encode as 16-bit PCM WAV in memory
        import io, wave  # noqa: E401
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(sr)
            wf.writeframes(audio_np.tobytes())
        wav_bytes = buf.getvalue()

        # Send to Orchestrator transcription endpoint (raw bytes — bypass _api helper)
        try:
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(
                    f"{_BASE_URL}/orchestrator/transcribe",
                    content=wav_bytes,
                    headers={**_headers(), "Content-Type": "audio/wav"},
                )
                resp.raise_for_status()
            text = resp.json().get("text", "").strip()
        except Exception as exc:
            typer.secho(f"  ERROR transcribing: {exc}", fg=typer.colors.RED, err=True)
            return

        if not text:
            typer.echo("  (empty transcript)")
            return

        typer.secho(f"  ✎ {text}", fg=typer.colors.CYAN)

        # Submit as a task
        try:
            resp = _api("POST", "/orchestrator/task", json={"prompt": text})
            task_id = resp.json().get("task_id", "?")
            typer.secho(f"  ✓ Task submitted: {task_id}", fg=typer.colors.GREEN)
        except Exception as exc:
            typer.secho(f"  ERROR submitting task: {exc}", fg=typer.colors.RED, err=True)

    listener = kb.Listener(on_press=_on_press, on_release=_on_release)
    listener.start()
    try:
        listener.join()
    except KeyboardInterrupt:
        pass
    finally:
        stream.stop()
        stream.close()
        listener.stop()
        typer.echo("\nDictate session ended.")


# ── tools ─────────────────────────────────────────────────────────────────────

tools_app = typer.Typer(help="Tool confidence management.", no_args_is_help=True)
app.add_typer(tools_app, name="tools")


@tools_app.command("confidence")
def tools_confidence(
    agent: Optional[str] = typer.Option(None, "--agent", help="Filter by agent name."),
) -> None:
    """Show tool confidence scores per agent."""
    params: dict[str, object] = {}
    if agent:
        params["agent"] = agent

    response = _api("GET", "/orchestrator/tools/confidence", params=params)
    scores = response.json()
    if not scores:
        typer.echo("No confidence scores found.")
        return

    current_agent = None
    for row in scores:
        if row["agent"] != current_agent:
            current_agent = row["agent"]
            typer.secho(f"\n  {current_agent}", fg=typer.colors.BRIGHT_WHITE)
        bar_len = int(row["confidence"] * 20)
        bar = "█" * bar_len + "░" * (20 - bar_len)
        typer.secho(f"    {row['tool']:<24}", fg=typer.colors.CYAN, nl=False)
        typer.echo(f"  {bar}  {row['confidence']:.2f}")


# ── config ────────────────────────────────────────────────────────────────────

config_app = typer.Typer(help="System configuration.", no_args_is_help=True)
app.add_typer(config_app, name="config")

_CONFIG_KEYS = {
    "ledger.retention_days": ("task_cleanup_completed_days", int),
    "jobs.poll_interval_seconds": ("job_poll_interval_seconds", int),
    "agent.read_timeout_seconds": ("agent_read_timeout_seconds", int),
    "inference.pool_refresh_hours": ("inference_pool_refresh_interval_hours", int),
}


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help=f"Config key. Valid: {', '.join(_CONFIG_KEYS)}"),
    value: str = typer.Argument(..., help="New value."),
) -> None:
    """Persist a configuration value to the .env file in north_home."""
    from config.settings import settings

    if key not in _CONFIG_KEYS:
        typer.secho(
            f"Unknown key {key!r}. Valid keys: {', '.join(_CONFIG_KEYS)}",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(1)

    _, cast = _CONFIG_KEYS[key]
    try:
        cast(value)
    except ValueError:
        typer.secho(
            f"Invalid value {value!r} for {key}: expected {cast.__name__}",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(1)

    env_file = settings.north_home / ".env"
    settings.north_home.mkdir(parents=True, exist_ok=True)

    env_key = f"NORTH_{key.upper().replace('.', '_')}"
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    updated = False
    for i, line in enumerate(lines):
        if line.startswith(f"{env_key}="):
            lines[i] = f"{env_key}={value}"
            updated = True
            break
    if not updated:
        lines.append(f"{env_key}={value}")
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    typer.secho(f"✓ {env_key}={value}", fg=typer.colors.GREEN)
    typer.echo("  Restart north for the change to take effect.")


# ── port helpers ──────────────────────────────────────────────────────────────

def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _is_north_server(host: str, port: int) -> bool:
    try:
        resp = httpx.get(f"http://{host}:{port}/docs", timeout=2.0)
        return resp.status_code == 200
    except Exception:
        return False


def _wait_for_server(host: str, port: int, timeout: int = 90) -> None:
    """Poll until the server responds or timeout expires."""
    typer.echo("Waiting for server ", nl=False)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_north_server(host, port):
            typer.secho(" ready.", fg=typer.colors.GREEN)
            return
        typer.echo(".", nl=False)
        time.sleep(1)
    typer.echo("")
    typer.secho("Server did not become ready in time.", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def _sync_docker_secret(compose_file: Path) -> None:
    """Read the north secret from the running Docker container and cache it locally."""
    try:
        result = subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "exec", "-T", "north",
             "cat", "/data/secret.key"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            secret_path = Path.home() / ".north" / "secret.key"
            secret_path.parent.mkdir(parents=True, exist_ok=True)
            secret_path.write_text(result.stdout.strip(), encoding="utf-8")
    except Exception:
        pass


def _kill_port(host: str, port: int) -> bool:
    try:
        import psutil
        killed = False
        for conn in psutil.net_connections(kind="inet"):
            if conn.laddr.port == port and conn.status == "LISTEN":
                try:
                    psutil.Process(conn.pid).kill()
                    killed = True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        return killed
    except ImportError:
        # Fallback: platform-agnostic subprocess approach
        try:
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, text=True,
            )
            pids = result.stdout.strip().split()
            if not pids:
                return False
            subprocess.run(["kill", "-9"] + pids, capture_output=True)
            return True
        except Exception:
            return False
    except Exception:
        return False


# ── start ─────────────────────────────────────────────────────────────────────

def _find_compose_file() -> Optional[Path]:
    """Walk up from CWD looking for docker-compose.yml."""
    cwd = Path.cwd()
    for candidate in [cwd, *cwd.parents]:
        f = candidate / "docker-compose.yml"
        if f.exists():
            return f
    return None


def _docker_available() -> bool:
    return shutil.which("docker") is not None


@app.command("start")
def start(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind host (local mode only)."),
    port: int = typer.Option(8000, "--port", "-p", help="Bind port."),
    reload: bool = typer.Option(False, "--reload", help="Enable auto-reload (local mode only)."),
    local: bool = typer.Option(False, "--local", help="Skip Docker; run directly with uvicorn."),
    no_chat: bool = typer.Option(False, "--no-chat", help="Start server only; skip interactive chat."),
) -> None:
    """Start north, then drop into interactive chat.

    By default uses Docker Compose when a docker-compose.yml is found and
    Docker is available. Pass --local to force direct uvicorn launch.
    Pass --no-chat to start the server without entering the chat REPL.
    """
    compose_file = _find_compose_file()
    use_docker = not local and _docker_available() and compose_file is not None

    if use_docker:
        typer.secho("★ north", fg=typer.colors.BRIGHT_WHITE, bold=True, nl=False)
        typer.echo(f"  Mode         → Docker Compose")
        typer.echo(f"  Compose file → {compose_file}")
        typer.echo(f"  Web UI       → http://127.0.0.1:{port}/ui/")
        typer.echo("")
        result = subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "up", "--build", "--detach"],
            check=False,
        )
        if result.returncode != 0:
            typer.secho("Docker Compose failed to start.", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)

        _wait_for_server("127.0.0.1", port)
        _sync_docker_secret(compose_file)

        if not no_chat:
            _chat_loop()
        return

    if not local and not _docker_available():
        typer.secho(
            "Docker not found — falling back to local mode. Install Docker for container support.",
            fg=typer.colors.YELLOW,
        )
    elif not local and compose_file is None:
        typer.secho(
            "No docker-compose.yml found — falling back to local mode. "
            "Run from the project root for Docker support.",
            fg=typer.colors.YELLOW,
        )

    # ── Local uvicorn launch ──────────────────────────────────────────────
    from config.settings import settings
    from utils.security import load_secret

    settings.north_home.mkdir(parents=True, exist_ok=True)
    (settings.north_home / "tasks").mkdir(parents=True, exist_ok=True)
    (settings.north_home / "context").mkdir(parents=True, exist_ok=True)
    load_secret()

    if _port_in_use(host, port):
        if _is_north_server(host, port):
            typer.secho(f"north is already running on port {port}.", fg=typer.colors.YELLOW)
            if not no_chat:
                typer.echo("")
                _chat_loop()
            return
        else:
            typer.secho(f"Port {port} is in use by another application.", fg=typer.colors.YELLOW)
        kill = typer.confirm("Kill the existing process and restart?", default=False)
        if not kill:
            raise typer.Exit(0)
        typer.echo(f"Stopping process on port {port}…")
        if not _kill_port(host, port):
            typer.secho(
                f"Could not stop the existing process. Try killing port {port} manually.",
                fg=typer.colors.RED, err=True,
            )
            raise typer.Exit(1)
        time.sleep(1)

    typer.secho("★ north", fg=typer.colors.BRIGHT_WHITE, bold=True, nl=False)
    typer.echo(f"  Mode         → Local")
    typer.echo(f"  Orchestrator → http://{host}:{port}")
    typer.echo(f"  Web UI       → http://{host}:{port}/ui/")
    typer.echo(f"  API docs     → http://{host}:{port}/docs")
    typer.echo(f"  Home         → {settings.north_home}")
    typer.echo("")

    import uvicorn

    if reload or no_chat:
        # reload uses multiprocessing and can't share a thread with the chat REPL
        uvicorn.run(
            "orchestrator.app:app",
            host=host,
            port=port,
            reload=reload,
            log_level="info",
        )
        return

    # Start server in a background daemon thread, then enter chat
    import threading

    def _run_server() -> None:
        uvicorn.run(
            "orchestrator.app:app",
            host=host,
            port=port,
            log_level="error",
            access_log=False,
        )

    threading.Thread(target=_run_server, daemon=True).start()
    _wait_for_server(host, port)
    _chat_loop()


@app.command("stop")
def stop(
    port: int = typer.Option(8000, "--port", "-p", help="Port to stop."),
) -> None:
    """Stop north (Docker Compose or local process)."""
    compose_file = _find_compose_file()

    if _docker_available() and compose_file is not None:
        typer.echo("Stopping Docker Compose services…")
        subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "down"],
            check=False,
        )
        return

    if _port_in_use("127.0.0.1", port):
        typer.echo(f"Stopping process on port {port}…")
        if _kill_port("127.0.0.1", port):
            typer.secho(f"✓ Stopped.", fg=typer.colors.GREEN)
        else:
            typer.secho("Could not stop the process. Try killing it manually.", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
    else:
        typer.echo("north is not running.")


if __name__ == "__main__":
    app()
