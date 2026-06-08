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

import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import TypedDict

import httpx
import typer
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from utils.security import load_secret

_console = Console(force_terminal=sys.stdout.isatty())


class _Provider(TypedDict):
    name: str
    env_key: str
    description: str
    url: str


_PROVIDERS: list[_Provider] = [
    {
        "name": "OpenRouter",
        "env_key": "NORTH_OPENROUTER_API_KEY",
        "description": "All models — Claude, GPT-4, Gemini, Llama, and more (recommended)",
        "url": "https://openrouter.ai/keys",
    },
    {
        "name": "Groq",
        "env_key": "NORTH_GROQ_API_KEY",
        "description": "Ultra-fast open-source models — Llama, Mixtral",
        "url": "https://console.groq.com/keys",
    },
    {
        "name": "Gemini",
        "env_key": "NORTH_GEMINI_API_KEY",
        "description": "Google Gemini 1.5 Pro and Flash",
        "url": "https://aistudio.google.com/apikey",
    },
]


def _provider_is_configured(provider: _Provider, env_file: Path) -> bool:
    if os.environ.get(provider["env_key"], "").strip():
        return True
    if not env_file.exists():
        return False
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{provider['env_key']}=") and line.split("=", 1)[1].strip():
            return True
    return False


def _any_provider_configured(env_file: Path) -> bool:
    return any(_provider_is_configured(p, env_file) for p in _PROVIDERS)


def _parse_provider_selection(raw: str) -> list[_Provider]:
    """Parse a comma-separated string of 1-based indices into provider entries."""
    seen: set[int] = set()
    selected: list[_Provider] = []
    for part in raw.replace(" ", "").split(","):
        try:
            idx = int(part) - 1
        except ValueError:
            continue
        if 0 <= idx < len(_PROVIDERS) and idx not in seen:
            selected.append(_PROVIDERS[idx])
            seen.add(idx)
    return selected


def _save_provider_key(env_file: Path, env_key: str, api_key: str) -> None:
    """Write or update the key in ~/.north/.env and export it to the running environment."""
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    prefix = f"{env_key}="
    found = False
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            lines[i] = f"{env_key}={api_key}"
            found = True
            break
    if not found:
        lines.append(f"{env_key}={api_key}")
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[env_key] = api_key


def _prompt_provider_keys(env_file: Path, providers: list[_Provider]) -> bool:
    """Prompt the user for each provider's API key. Returns True if at least one was saved."""
    any_saved = False
    for p in providers:
        typer.echo(f"\n  {p['name']}  —  get a key at {p['url']}")
        api_key = typer.prompt(f"  Enter your {p['name']} API key").strip()
        if not api_key:
            typer.secho(f"  Skipping {p['name']} (no key entered).", fg=typer.colors.YELLOW)
            continue
        _save_provider_key(env_file, p["env_key"], api_key)
        typer.secho(f"  ✓ {p['name']} key saved.", fg=typer.colors.GREEN)
        any_saved = True
    return any_saved


def _render_provider_menu() -> None:
    """Display available inference providers to choose from."""
    typer.echo("")
    typer.secho("No inference provider is configured.", fg=typer.colors.YELLOW)
    typer.echo("Choose which provider(s) you want to set up:\n")
    for i, p in enumerate(_PROVIDERS, 1):
        typer.echo(f"  [{i}] {p['name']:12}  {p['description']}")
    typer.echo("")


def _ensure_api_keys() -> None:
    """Ensure at least one inference provider API key is configured.

    Checks env vars and ~/.north/.env first. If none are set, presents
    the available providers and lets the user choose which to configure.
    """
    from config.settings import settings

    env_file = settings.north_home / ".env"
    if _any_provider_configured(env_file):
        return

    _render_provider_menu()
    raw = typer.prompt("Enter number(s) separated by commas (e.g. 1 or 1,3)").strip()
    chosen = _parse_provider_selection(raw)
    if not chosen:
        typer.secho("No valid selection — north cannot start.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    settings.north_home.mkdir(parents=True, exist_ok=True)
    if not _prompt_provider_keys(env_file, chosen):
        typer.secho("No API keys saved — north cannot start.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


app = typer.Typer(
    name="north",
    help="north — Personal Life Operating System CLI",
    no_args_is_help=False,
    add_completion=False,
    invoke_without_command=True,
)

_BASE_URL = "http://127.0.0.1:8000"
_TIMEOUT = 30.0


@app.callback()
def _root(ctx: typer.Context) -> None:
    """north — Personal Life Operating System.

    Run without a subcommand to open the interactive TUI.
    """
    if ctx.invoked_subcommand is None:
        # No subcommand — boot the server if needed, then open the TUI.
        _launch_tui()


def _launch_tui(
    host: str = "127.0.0.1",
    port: int = 8000,
    workspace: str | None = None,
) -> None:
    """Auto-start the server if not running, then launch the TUI."""
    if not _port_in_use(host, port) or not _is_north_server(host, port):
        _console.print("  [dim]server offline — starting…[/dim]")
        # Re-invoke `north start --no-chat` to start the server only, then TUI below.
        from config.settings import settings
        from utils.security import load_secret

        settings.north_home.mkdir(parents=True, exist_ok=True)
        load_secret()

        log_path = settings.north_home / "north.log"
        pid_path = settings.north_home / "north.pid"
        resolved_workspace = workspace or str(Path.home())

        cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            "orchestrator.app:app",
            "--host",
            host,
            "--port",
            str(port),
            "--log-level",
            "info",
        ]
        server_env = {**os.environ, "NORTH_NORTH_WORKSPACE": resolved_workspace}
        log_file = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
        proc = subprocess.Popen(cmd, stdout=log_file, stderr=log_file, env=server_env)
        pid_path.write_text(str(proc.pid), encoding="utf-8")
        _wait_for_server(host, port)
        workspace = resolved_workspace

    base_url = f"http://{host}:{port}"
    headers = _headers()
    resolved_workspace = workspace or str(_find_git_root(Path.cwd()))

    import asyncio

    from cli.tui import run as _tui_run

    asyncio.run(_tui_run(base_url=base_url, headers=headers, workspace=resolved_workspace))


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
            "ERROR: Cannot reach the north server. Is it running?\n  uvicorn orchestrator.app:app --port 8000",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from None
    except httpx.HTTPStatusError as exc:
        typer.secho(
            f"ERROR: Server returned {exc.response.status_code}: {exc.response.text}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from None


# ── task ─────────────────────────────────────────────────────────────────────

task_app = typer.Typer(help="Task management.", no_args_is_help=True)
app.add_typer(task_app, name="task")


@task_app.callback(invoke_without_command=True)
def task_default(
    ctx: typer.Context,
    prompt: str | None = typer.Argument(None, help="Prompt to submit as a new task."),
    workspace: str | None = typer.Option(
        None,
        "--workspace",
        "-w",
        help="Root directory agents can read/write. Defaults to git root of current directory.",
    ),
) -> None:
    """Submit a task and stream results live."""
    if ctx.invoked_subcommand is not None:
        return
    if prompt is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()
    resolved = workspace or str(_find_git_root(Path.cwd()))
    _run_task(prompt, workspace=resolved)


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
        _console.print("  [dim]no active tasks[/dim]")
        return
    _console.print()
    for t in tasks:
        _console.print(
            f"  [bright_black]{t['task_id']}[/bright_black]"
            f"  [dim]{t['status']}[/dim]"
            f"  [bright_black]{t['created_at']}[/bright_black]"
        )
    _console.print()


# ── chat ─────────────────────────────────────────────────────────────────────

# ── shared task runner ────────────────────────────────────────────────────────

_STEP_ICONS: dict[str, str] = {
    "classifying": "→",
    "classified": "✓",
    "classified_as_trivial": "✓",
    "north_star_checking": "→",
    "north_star_aligned": "✓",
    "north_star_conflict": "◆",
    "routing": "→",
    "routed": "✓",
    "executing": "→",
    "agent_started": "→",
    "agent_completed": "✓",
    "tool_called": "→",
    "tool_result": "✓",
}

_STEP_LABELS: dict[str, str] = {
    "classifying": "classifying…",
    "classified": "classified",
    "classified_as_trivial": "quick task",
    "north_star_checking": "checking goals…",
    "north_star_aligned": "goals aligned",
    "north_star_conflict": "goal conflict",
    "routing": "planning…",
    "routed": "plan ready",
    "executing": "running agents…",
}


def _build_steps_table(steps: list[tuple[str, str, bool]]) -> Table:
    """Render pipeline steps as a borderless table. Each step is (icon, label, active)."""
    t = Table.grid(padding=(0, 2))
    t.add_column(width=1)
    t.add_column()
    for icon, label, active in steps:
        if active:
            t.add_row(
                Text(icon, style="white"),
                Text(label, style="white"),
            )
        else:
            t.add_row(
                Text(icon, style="dim green"),
                Text(label, style="dim"),
            )
    return t


def _run_task(prompt: str, workspace: str | None = None) -> str:
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
            _build_steps_table(steps) if steps else Text("starting…", style="dim"),
            border_style="bright_black",
            padding=(0, 1),
        )

    url = f"{_BASE_URL}/orchestrator/stream/{task_id}"
    output_text: str = ""
    failed_msg: str = ""
    token_buffer: str = ""

    try:
        with (
            Live(_make_renderable(), console=_console, refresh_per_second=8) as live,
            httpx.stream("GET", url, headers=_headers(), timeout=None) as stream,
        ):
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
                        steps.append(("→", f"  {tool}…", True))
                    elif event == "tool_result":
                        tool = data.get("tool", "tool")
                        success = data.get("success", True)
                        steps.append(("✓" if success else "✗", f"  {tool}", True))
                    elif event == "approval_required":
                        steps.append(("?", "Approval required", False))
                        live.update(_make_renderable())
                        live.stop()
                        _console.print()
                        _console.print(
                            Panel(
                                Text(data.get("message", ""), style="white"),
                                title="[yellow]approval required[/yellow]",
                                border_style="yellow",
                                padding=(1, 2),
                            )
                        )
                        options = data.get("options", ["Approve", "Reject"])
                        for i, opt in enumerate(options, 1):
                            _console.print(f"  [bright_black][{i}][/bright_black]  {opt}")
                        _console.print()
                        raw_choice = input("  ❯ ").strip()
                        try:
                            idx = int(raw_choice) - 1
                            chosen = options[idx] if 0 <= idx < len(options) else raw_choice
                        except ValueError:
                            chosen = raw_choice or options[0]
                        decision = (
                            "approved"
                            if chosen.lower() in ("approve", "approved", "yes")
                            else "rejected"
                            if chosen.lower() in ("reject", "rejected", "no")
                            else "answered"
                        )
                        with contextlib.suppress(SystemExit):
                            _api(
                                "POST",
                                "/orchestrator/approval/respond",
                                json={
                                    "card_id": data.get("card_id", ""),
                                    "task_id": task_id,
                                    "agent": data.get("agent", ""),
                                    "decision": decision,
                                    "chosen_option": chosen,
                                },
                            )
                        steps[-1] = ("✓" if decision != "rejected" else "✗", f"Approval: {chosen}", False)
                        # Do NOT restart Live — cursor is now past the approval panel.
                        # Subsequent live.update() calls on a stopped Live are no-ops;
                        # task_completed / task_cancelled will break the loop.
                        current_event = ""
                        continue
                    elif event == "token":
                        token_buffer += data.get("text", "")
                        # Don't add a step pill per token — just accumulate silently.
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
        _console.print()
        _console.print(
            Panel(
                Text(failed_msg, style="red"),
                title="[dim]north — error[/dim]",
                border_style="bright_black",
                padding=(1, 2),
            )
        )
        return ""

    if token_buffer:
        # Tokens were streamed — use them directly, no ledger round-trip needed.
        output_text = token_buffer
    else:
        # No tokens (multi-agent synthesis or older path) — fetch from ledger.
        try:
            ledger_resp = _api("GET", f"/orchestrator/ledger?task_id={task_id}&limit=20")
            entries = ledger_resp.json()
            outputs = [e["output"] for e in entries if e.get("action") == "agent_completed" and e.get("output")]
            output_text = "\n\n".join(outputs) if outputs else "Task completed."
        except Exception:
            output_text = "Task completed."

    _console.print()
    _console.print(
        Panel(
            Markdown(output_text),
            title="[dim]north[/dim]",
            border_style="bright_black",
            padding=(1, 2),
        )
    )
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
    rc = subprocess.call([editor, str(doc_path)])
    if rc != 0:
        typer.echo(f"Editor exited with code {rc}.", err=True)


@context_app.command("add")
def context_add(
    text: str | None = typer.Option(None, "--text", help="Raw text to inject."),
    url: str | None = typer.Option(None, "--url", help="URL to fetch and inject."),
    file: Path | None = typer.Option(None, "--file", help="File to inject."),
) -> None:
    """Inject context from text, a URL, or a file."""
    if file is not None:
        if not file.exists():
            typer.secho(f"ERROR: File not found: {file}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from None
        with open(file, "rb") as fh:
            files = {"file": (file.name, fh, "application/octet-stream")}
            response = _api("POST", "/orchestrator/context/add", files=files)  # type: ignore[arg-type]
    elif url is not None:
        response = _api("POST", "/orchestrator/context/add", data={"url": url})
    elif text is not None:
        response = _api("POST", "/orchestrator/context/add", data={"text": text})
    else:
        typer.secho("Provide --text, --url, or --file.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None

    data = response.json()
    typer.secho(
        f"✓ Injected into {data.get('document', '?')} (source: {data.get('source', '?')})",
        fg=typer.colors.GREEN,
    )


# ── ledger ───────────────────────────────────────────────────────────────────


@app.command("ledger")
def show_ledger(
    limit: int = typer.Option(20, "--limit", "-n", help="Number of entries to show."),
    task_id: str | None = typer.Option(None, "--task", help="Filter by task ID."),
    agent: str | None = typer.Option(None, "--agent", help="Filter by agent name."),
    source: str | None = typer.Option(None, "--source", help="Filter by source type."),
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
        _console.print("  [dim]no ledger entries[/dim]")
        return

    table = Table(box=None, padding=(0, 2), show_header=True, header_style="dim")
    table.add_column("time", style="bright_black", no_wrap=True)
    table.add_column("source", no_wrap=True)
    table.add_column("action")
    table.add_column("status", no_wrap=True)
    table.add_column("agent", style="dim")
    for entry in entries:
        ts = entry["timestamp"][:19].replace("T", " ")
        status = entry.get("status") or ""
        status_fmt = (
            f"[green]{status}[/green]"
            if status == "completed"
            else f"[red]{status}[/red]"
            if status == "failed"
            else f"[dim]{status}[/dim]"
        )
        table.add_row(
            ts,
            f"[dim]{entry['source']}[/dim]",
            entry.get("action") or "",
            status_fmt,
            entry.get("agent") or "",
        )
    _console.print()
    _console.print(table)
    _console.print()


# ── jobs ──────────────────────────────────────────────────────────────────────


@app.command("jobs")
def show_jobs(
    limit: int = typer.Option(20, "--limit", "-n", help="Number of jobs to show."),
    status: str | None = typer.Option(None, "--status", help="Filter by status."),
) -> None:
    """Show scheduled jobs."""
    params: dict[str, object] = {"limit": limit}
    if status:
        params["status"] = status

    response = _api("GET", "/orchestrator/jobs", params=params)
    jobs = response.json()
    if not jobs:
        _console.print("  [dim]no jobs[/dim]")
        return

    _console.print()
    for job in jobs:
        status = job["status"]
        status_style = (
            "green"
            if status == "completed"
            else "yellow"
            if status == "pending"
            else "red"
            if status == "failed"
            else "dim"
        )
        _console.print(
            f"  [bright_black]{job['job_id']}[/bright_black]  "
            f"[{status_style}]{status}[/{status_style}]  "
            f"[dim]{job['type']}[/dim]  {job['agent']}"
        )
    _console.print()


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
        _console.print("  [dim]no agents registered[/dim]")
        return
    _console.print()
    for a in agents:
        _console.print(
            f"  [white]{a['name']:<16}[/white]  "
            f"[dim]{a['domain']:<12}[/dim]  "
            f"[bright_black]{a['model_pool']}[/bright_black]"
        )
    _console.print()


@agent_app.command("create")
def create_agent(
    name: str | None = typer.Option(None, "--name", help="Agent name (slug, lowercase)."),
    domain: str | None = typer.Option(None, "--domain", help="Domain (e.g. health, finance)."),
    description: str | None = typer.Option(None, "--description", help="One-line description."),
    model_pool: str = typer.Option("fast_cheap", "--pool", help="Model pool: reasoning / fast_cheap / high_volume."),
    output_dir: Path | None = typer.Option(None, "--output-dir", help="Agent folder to create in (default: ./agents/)"),
) -> None:
    """Interactively scaffold a new domain-specialist agent."""
    import re

    if name is None:
        name = typer.prompt("Agent name (slug, e.g. travel)")
    name = name.strip().lower().replace(" ", "_")
    if not re.match(r"^[a-z][a-z0-9_]*$", name):
        typer.secho("ERROR: Name must be lowercase letters, digits, underscores.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None

    if domain is None:
        domain = typer.prompt("Domain (e.g. travel, fitness, finance)", default=name)
    domain = domain.strip().lower()

    if description is None:
        description = typer.prompt("One-line description", default=f"Domain specialist for {domain}.")

    agentic = typer.confirm(
        "Use agentic loop? (yes = ReAct loop with tool calls; no = single LLM call)",
        default=True,
    )

    raw_tools = typer.prompt(
        "Specialized tools (comma-separated, or blank).\n"
        "  Universal tools (web_search, fetch_url, read_file, write_file,\n"
        "  list_dir, search_files, schedule_task) are auto-included — skip them",
        default="",
    )
    tools = [t.strip() for t in raw_tools.split(",") if t.strip()]

    raw_accepts = typer.prompt("Accepts task keywords (comma-separated, or blank)", default=domain)
    accepts = [a.strip() for a in raw_accepts.split(",") if a.strip()]

    # Determine output location
    if output_dir is None:
        # Try to detect project root (has pyproject.toml or agents/ folder)
        cwd = Path.cwd()
        output_dir = cwd / "agents" if (cwd / "agents").is_dir() else cwd
    else:
        output_dir = output_dir.resolve()

    agent_dir = output_dir / name
    if agent_dir.exists():
        typer.secho(f"ERROR: Directory already exists: {agent_dir}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None

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
        "from agents.agentic_llm_agent import AgenticLLMAgent" if agentic else "from agents.llm_agent import LLMAgent"
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
        yaml.dump(
            {
                "agent": name,
                "domain": domain,
                "model_pool": model_pool,
                "accepts": accepts,
                "output_format": "structured_json",
                "version": "1.0.0",
                "class_name": class_name,
            },
            default_flow_style=False,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    # Discover universal tool names by scanning the installed package.
    try:
        import importlib.util as _ilu

        _spec = _ilu.find_spec("tools.universal")
        if _spec and _spec.submodule_search_locations:
            _udir = Path(list(_spec.submodule_search_locations)[0])
            _universal = {p.stem for p in _udir.glob("*.py") if not p.stem.startswith("_")}
        else:
            _universal = set()
    except Exception:
        _universal = set()

    universal_requested = [t for t in tools if t in _universal]
    specialized_tools = [t for t in tools if t not in _universal]

    if universal_requested:
        typer.secho(
            f"  Note: {', '.join(universal_requested)} are universal — auto-included, omitted from tools.yaml",
            fg=typer.colors.BRIGHT_BLACK,
        )

    _tools_comment = (
        "# Specialized tools for this agent. Universal tools are\n"
        "# automatically available to all agents and do not need to be listed here.\n"
    )
    _tools_body = "tools:\n" + "".join(f"  - {t}\n" for t in specialized_tools) if specialized_tools else "tools: []\n"
    (agent_dir / "tools.yaml").write_text(_tools_comment + _tools_body, encoding="utf-8")

    (agent_dir / "prompts" / "system.md").write_text(system_prompt, encoding="utf-8")
    (agent_dir / "README.md").write_text(
        f"# {name.title()} Agent\n\n{description}\n\n"
        f"**Domain:** {domain}  \n**Pool:** {model_pool}  \n**Accepts:** {', '.join(accepts)}\n",
        encoding="utf-8",
    )

    # Update prompts/planner.md so the new domain is routable.
    planner_updated = _update_planner_routing(
        domain=domain,
        description=description,
        output_dir=output_dir,
    )

    typer.secho(f"\n✓ Agent scaffold created at {agent_dir}", fg=typer.colors.GREEN)
    typer.echo(f"  {agent_dir}/agent.py")
    typer.echo(f"  {agent_dir}/config.yaml")
    typer.echo(f"  {agent_dir}/tools.yaml")
    typer.echo(f"  {agent_dir}/prompts/system.md")
    if planner_updated:
        typer.echo(f"  prompts/planner.md  ← domain '{domain}' added to routing table")
    else:
        typer.secho(
            "  Note: could not find prompts/planner.md — add the domain row manually.",
            fg=typer.colors.YELLOW,
        )
    typer.echo("\nRestart north to load the new agent.")


def _update_planner_routing(domain: str, description: str, output_dir: Path) -> bool:
    """Insert a new domain row into prompts/planner.md routing table.

    Walks up from output_dir to find the project root (has prompts/planner.md).
    Returns True if the file was found and updated (or already had the domain).
    """
    planner: Path | None = None
    for candidate in [output_dir, *output_dir.parents]:
        p = candidate / "prompts" / "planner.md"
        if p.exists():
            planner = p
            break

    if planner is None:
        return False

    content = planner.read_text(encoding="utf-8")
    marker = "| `general` |"
    if f"| `{domain}`" in content:
        return True  # already present

    # Summarise description to a short table entry (max 60 chars).
    summary = description[:60].rstrip(".")
    new_row = f"| `{domain}` | {summary} |\n"
    content = content.replace(marker, new_row + marker, 1)
    planner.write_text(content, encoding="utf-8")
    return True


@agent_app.command("run")
def run_agent(
    name: str = typer.Argument(..., help="Agent name (health, finance, job, university)."),
    task: str = typer.Argument(..., help="Task description for the agent."),
) -> None:
    """Manually trigger a specific agent."""
    response = _api("POST", "/orchestrator/agent/run", json={"agent": name, "task": task})
    data = response.json()
    typer.secho(f"✓ Task submitted: {data['task_id']}", fg=typer.colors.GREEN)
    typer.echo(f"  Stream with:  north stream {data['task_id']}")


# ── inference ─────────────────────────────────────────────────────────────────

inference_app = typer.Typer(help="Inference cost and model info.", no_args_is_help=True)
app.add_typer(inference_app, name="inference")


@inference_app.command("costs")
def inference_costs(
    period: str = typer.Option("week", "--period", help="day / week / month"),
    agent: str | None = typer.Option(None, "--agent", help="Filter by agent/component."),
) -> None:
    """Show inference cost summary."""
    params: dict[str, object] = {"period": period}
    if agent:
        params["agent"] = agent

    response = _api("GET", "/orchestrator/inference/costs", params=params)
    data = response.json()

    _console.print()
    _console.print(f"  [bold white]inference costs[/bold white]  [dim]{data['period']}[/dim]")
    _console.print(f"  [bright_black]{'─' * 44}[/bright_black]")
    _console.print(f"  [dim]total      [/dim]  ${data['total_cost_usd']:.6f}")

    if data.get("by_component"):
        _console.print("\n  [dim]by component[/dim]")
        for comp, cost in sorted(data["by_component"].items(), key=lambda x: -x[1]):
            _console.print(f"    [dim]{comp:<24}[/dim]  ${cost:.6f}")

    if data.get("by_model"):
        _console.print("\n  [dim]by model[/dim]")
        for model, cost in sorted(data["by_model"].items(), key=lambda x: -x[1]):
            _console.print(f"    [dim]{model:<40}[/dim]  ${cost:.6f}")
    _console.print()


@inference_app.command("models")
def inference_models() -> None:
    """Show current model pool state."""
    response = _api("GET", "/orchestrator/inference/models")
    pools = response.json()
    _console.print()
    for pool_name, pool_data in pools.items():
        models = pool_data.get("models", [])
        _console.print(f"  [bold white]{pool_name}[/bold white]  [bright_black]{len(models)} models[/bright_black]")
        for entry in models:
            _console.print(f"    [dim]{entry['id']}[/dim]  [bright_black]({entry['provider']})[/bright_black]")
        _console.print()


# ── metrics ──────────────────────────────────────────────────────────────────


@app.command("metrics")
def metrics(
    period: int = typer.Option(7, "--period", "-p", help="Look-back window in days (default 7)."),
) -> None:
    """Show system performance metrics from the ledger."""
    response = _api("GET", "/orchestrator/metrics", params={"days": period})
    data = response.json()

    _console.print()
    _console.print(f"  [bold white]metrics[/bold white]  [dim]last {data['period_days']} days[/dim]")
    _console.print(f"  [bright_black]{'─' * 44}[/bright_black]")
    _console.print(f"  [dim]tasks      [/dim]  {data['total_tasks']}")
    _console.print(f"  [dim]cost       [/dim]  ${data['total_cost_usd']:.6f}")
    _console.print(f"  [dim]tokens in  [/dim]  {data['total_tokens_in']:,}")
    _console.print(f"  [dim]tokens out [/dim]  {data['total_tokens_out']:,}")

    if data.get("by_agent"):
        _console.print()
        t = Table(box=None, padding=(0, 2), show_header=True, header_style="dim")
        t.add_column("agent", style="bold white")
        t.add_column("tasks", justify="right")
        t.add_column("success", justify="right")
        t.add_column("cost $", justify="right")
        t.add_column("p50 ms", justify="right")
        t.add_column("p95 ms", justify="right")
        for a in data["by_agent"]:
            t.add_row(
                a["agent"],
                str(a["tasks"]),
                f"{a['success_rate'] * 100:.0f}%",
                f"{a['cost_usd']:.6f}",
                str(a["p50_ms"]) if a["p50_ms"] is not None else "—",
                str(a["p95_ms"]) if a["p95_ms"] is not None else "—",
            )
        _console.print(t)

    if data.get("by_model"):
        _console.print("\n  [dim]cost by model[/dim]")
        for model, cost in sorted(data["by_model"].items(), key=lambda x: -x[1]):
            _console.print(f"    [dim]{model:<44}[/dim]  ${cost:.6f}")

    if data.get("top_errors"):
        _console.print("\n  [dim]top errors[/dim]")
        for err, count in data["top_errors"].items():
            _console.print(f"    [dim]{err:<30}[/dim]  {count}")

    _console.print()


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
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1) from None

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
        fg=typer.colors.BRIGHT_WHITE,
        bold=True,
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
        import io  # noqa: E401
        import wave

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
    agent: str | None = typer.Option(None, "--agent", help="Filter by agent name."),
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
    _console.print()
    for row in scores:
        if row["agent"] != current_agent:
            current_agent = row["agent"]
            _console.print(f"  [bold white]{current_agent}[/bold white]")
        conf = row["confidence"]
        bar_len = int(conf * 20)
        bar = "█" * bar_len + "░" * (20 - bar_len)
        bar_color = "green" if conf >= 0.7 else "yellow" if conf >= 0.4 else "red"
        _console.print(f"    [dim]{row['tool']:<24}[/dim]  [{bar_color}]{bar}[/{bar_color}]  [dim]{conf:.2f}[/dim]")
    _console.print()


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
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1) from None

    _, cast = _CONFIG_KEYS[key]
    try:
        cast(value)
    except ValueError:
        typer.secho(
            f"Invalid value {value!r} for {key}: expected {cast.__name__}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1) from None

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
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_north_server(host, port):
            _console.print("  [dim green]✓[/dim green]  server ready")
            return
        time.sleep(1)
    _console.print("  [red]server did not respond in time[/red]", err=True)
    raise typer.Exit(1) from None


def _sync_docker_secret(compose_file: Path) -> None:
    """Read the north secret from the running Docker container and cache it locally."""
    try:
        result = subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "exec", "-T", "north", "cat", "/data/secret.key"],
            capture_output=True,
            text=True,
            timeout=10,
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
                capture_output=True,
                text=True,
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


def _find_compose_file() -> Path | None:
    """Return the compose file to use.

    Priority:
    1. Walk up from CWD — lets developers use their local repo copy (with build: .).
    2. ~/.north/docker-compose.yml — written on first run from the bundled copy.
    3. Bundled copy inside the installed package (cli/docker-compose.yml).
    """
    north_home = Path(os.environ.get("NORTH_HOME", "~/.north")).expanduser()
    installed = north_home / "docker-compose.yml"

    cwd = Path.cwd()
    for candidate in [cwd, *cwd.parents]:
        f = candidate / "docker-compose.yml"
        if f.exists() and f != installed:
            return f

    if not installed.exists():
        bundled = Path(__file__).parent / "docker-compose.yml"
        if bundled.exists():
            north_home.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bundled, installed)

    return installed if installed.exists() else None


def _find_git_root(start: Path) -> Path:
    """Walk up from start to find the git repo root. Falls back to start."""
    current = start.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return current


def _docker_available() -> bool:
    return shutil.which("docker") is not None


@app.command("start")
def start(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind host."),
    port: int = typer.Option(8000, "--port", "-p", help="Bind port."),
    reload: bool = typer.Option(False, "--reload", help="Enable auto-reload (local mode only)."),
    docker: bool = typer.Option(False, "--docker", help="Run via Docker Compose (for server/headless deployments)."),
    no_chat: bool = typer.Option(False, "--no-chat", help="Start server only; skip interactive chat."),
    workspace: str | None = typer.Option(
        None,
        "--workspace",
        "-w",
        help="Root directory agents can read/write. Defaults to current directory.",
    ),
) -> None:
    """Start north, then drop into interactive chat.

    Runs locally with uvicorn by default — the right choice for personal use
    on your own machine. Pass --docker for server or headless deployments.
    Pass --no-chat to start the server without entering the chat REPL.
    """
    base = Path(workspace).resolve() if workspace else Path.cwd()
    resolved_workspace = str(base)

    compose_file = _find_compose_file()
    use_docker = docker and _docker_available() and compose_file is not None

    if docker and not _docker_available():
        typer.secho("Docker not found. Install Docker or run without --docker.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None
    if docker and compose_file is None:
        typer.secho(
            "No docker-compose.yml found. Run from the project root or omit --docker.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1) from None

    if use_docker:
        _console.print()
        _console.print("  [bold white]north[/bold white]  [bright_black]docker[/bright_black]")
        _console.print(f"  [bright_black]{'─' * 44}[/bright_black]")
        _console.print(f"  [dim]compose    [/dim]  {compose_file}")
        _console.print(f"  [dim]address    [/dim]  http://127.0.0.1:{port}")
        _console.print(f"  [dim]workspace  [/dim]  {resolved_workspace}")
        _console.print()
        docker_env = {**os.environ, "NORTH_NORTH_WORKSPACE": resolved_workspace}
        result = subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "up", "--build", "--detach"],
            env=docker_env,
            check=False,
        )
        if result.returncode != 0:
            typer.secho("Docker Compose failed to start.", fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from None

        _wait_for_server("127.0.0.1", port)
        _sync_docker_secret(compose_file)

        if not no_chat:
            _launch_tui(host="127.0.0.1", port=port, workspace=resolved_workspace)
        return

    # ── Local uvicorn launch ──────────────────────────────────────────────
    from config.settings import settings
    from utils.security import load_secret

    settings.north_home.mkdir(parents=True, exist_ok=True)
    (settings.north_home / "tasks").mkdir(parents=True, exist_ok=True)
    (settings.north_home / "context").mkdir(parents=True, exist_ok=True)
    _ensure_api_keys()
    load_secret()

    log_path = settings.north_home / "north.log"
    pid_path = settings.north_home / "north.pid"

    if _port_in_use(host, port):
        if _is_north_server(host, port):
            typer.secho(f"north is already running on port {port}.", fg=typer.colors.YELLOW)
            if not no_chat:
                typer.echo("")
                _launch_tui(host=host, port=port, workspace=resolved_workspace)
            return
        typer.secho(f"Port {port} is in use by another application.", fg=typer.colors.YELLOW)
        kill = typer.confirm("Kill the existing process and restart?", default=False)
        if not kill:
            raise typer.Exit(0)
        typer.echo(f"Stopping process on port {port}…")
        if not _kill_port(host, port):
            typer.secho(
                f"Could not stop the existing process. Try killing port {port} manually.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(1) from None
        time.sleep(1)

    _console.print()
    _console.print("  [bold white]north[/bold white]  [bright_black]local[/bright_black]")
    _console.print(f"  [bright_black]{'─' * 44}[/bright_black]")
    _console.print(f"  [dim]address    [/dim]  http://{host}:{port}")
    _console.print(f"  [dim]workspace  [/dim]  {resolved_workspace}")
    _console.print(f"  [dim]home       [/dim]  {settings.north_home}")
    _console.print(f"  [dim]logs       [/dim]  {log_path}")
    _console.print()

    # Launch the server as a subprocess so its stdout/stderr are fully
    # redirected to the log file at the OS level — no monkey-patching needed.
    # Every print(), logging call, traceback, and uvicorn line goes to the file.
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "orchestrator.app:app",
        "--host",
        host,
        "--port",
        str(port),
        "--log-level",
        "info",
    ]
    if reload:
        cmd.append("--reload")

    server_env = {
        **os.environ,
        "NORTH_NORTH_WORKSPACE": resolved_workspace,
    }

    workspace_path = settings.north_home / "workspace.txt"
    workspace_path.write_text(resolved_workspace, encoding="utf-8")

    log_file = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
    proc = subprocess.Popen(cmd, stdout=log_file, stderr=log_file, env=server_env)
    pid_path.write_text(str(proc.pid), encoding="utf-8")

    _wait_for_server(host, port)

    if no_chat:
        typer.secho(f"north running (pid {proc.pid}). Stop with: north stop", fg=typer.colors.GREEN)
        return

    _launch_tui(host=host, port=port, workspace=resolved_workspace)


@app.command("stop")
def stop(
    port: int = typer.Option(8000, "--port", "-p", help="Port to stop."),
    docker: bool = typer.Option(False, "--docker", help="Stop Docker Compose deployment instead of a local process."),
) -> None:
    """Stop north."""
    import signal

    if docker:
        compose_file = _find_compose_file()
        if not _docker_available() or compose_file is None:
            typer.secho("Docker or docker-compose.yml not found.", fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from None
        typer.echo("Stopping Docker Compose services…")
        subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "down"],
            check=False,
        )
        return

    from config.settings import settings

    pid_path = settings.north_home / "north.pid"

    if pid_path.exists():
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
            os.kill(pid, signal.SIGTERM)
            pid_path.unlink()
            typer.secho(f"✓ Stopped (pid {pid}).", fg=typer.colors.GREEN)
        except ProcessLookupError:
            pid_path.unlink()
            typer.secho("north was not running (removed stale PID file).", fg=typer.colors.YELLOW)
        except Exception as exc:
            typer.secho(f"Failed to stop: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from None
        return

    # Fallback: no PID file, try port-based kill
    if _port_in_use("127.0.0.1", port):
        typer.echo(f"Stopping process on port {port}…")
        if _kill_port("127.0.0.1", port):
            typer.secho("✓ Stopped.", fg=typer.colors.GREEN)
        else:
            typer.secho("Could not stop the process. Try killing it manually.", fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from None
    else:
        typer.echo("north is not running.")


@app.command("reset")
def reset(
    all: bool = typer.Option(False, "--all", help="Also remove the API key and .env config."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """Wipe north's data and start fresh.

    Stops the server and deletes all local state — ledger, context, tasks,
    logs, learned preferences, and the secret key. Your API key in .env is
    kept unless you pass --all.
    """
    from config.settings import settings

    north_home = settings.north_home

    # What gets wiped
    if all:
        scope = f"{north_home}/ (everything including .env and API key)"
    else:
        scope = f"{north_home}/ (data only — .env and API key are kept)"

    if not yes:
        typer.secho(f"This will permanently delete: {scope}", fg=typer.colors.YELLOW)
        typer.confirm("Are you sure?", abort=True)

    # Stop the server first
    import signal

    pid_path = north_home / "north.pid"
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
            os.kill(pid, signal.SIGTERM)
            typer.echo(f"  Stopped server (pid {pid})")
        except ProcessLookupError:
            pass

    if all:
        shutil.rmtree(north_home, ignore_errors=True)
        typer.secho("✓ north fully removed. Run north start to begin fresh.", fg=typer.colors.GREEN)
        return

    # Selective wipe — keep .env
    env_backup = None
    env_file = north_home / ".env"
    if env_file.exists():
        env_backup = env_file.read_text(encoding="utf-8")

    shutil.rmtree(north_home, ignore_errors=True)

    if env_backup is not None:
        north_home.mkdir(parents=True, exist_ok=True)
        env_file.write_text(env_backup, encoding="utf-8")

    typer.secho("✓ Data wiped. API key kept. Run north start to begin fresh.", fg=typer.colors.GREEN)


@app.command("update")
def update(
    port: int = typer.Option(8000, "--port", "-p", help="Port the server is running on."),
    docker: bool = typer.Option(False, "--docker", help="Update a Docker Compose deployment."),
    restart: bool = typer.Option(True, "--restart/--no-restart", help="Restart the server after updating."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """Pull the latest north code and update dependencies.

    Works from any directory. Detects how north was installed and updates
    accordingly: uv tool installs are upgraded via uv; local git clones
    are updated with git pull + uv sync. Pass --docker to update a Docker
    Compose deployment instead.
    """
    _console.print()
    _console.print("  [bold white]north update[/bold white]")
    _console.print(f"  [bright_black]{'─' * 44}[/bright_black]")

    install_url, is_git_url = _get_install_url()
    if install_url:
        _console.print(f"  [dim]source     [/dim]  {install_url}")
    _console.print()

    if not yes:
        typer.confirm("Proceed with update?", default=True, abort=True)

    # ── Docker path ───────────────────────────────────────────────────────
    if docker:
        project_root = _find_project_root()
        compose_file = _find_compose_file()
        if not _docker_available() or compose_file is None:
            typer.secho("Docker or docker-compose.yml not found.", fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from None
        if project_root:
            _console.print("  [dim]→[/dim]  git pull…")
            _run_command(["git", "pull"], cwd=project_root)
        _console.print("  [dim]→[/dim]  docker compose build…")
        result = subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "up", "--build", "--detach"],
            cwd=project_root or Path.cwd(),
        )
        if result.returncode != 0:
            typer.secho("Docker Compose rebuild failed.", fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from None
        _wait_for_server("127.0.0.1", port)
        _console.print()
        typer.secho("✓ north updated and restarted via Docker.", fg=typer.colors.GREEN)
        return

    was_running = _port_in_use("127.0.0.1", port) and _is_north_server("127.0.0.1", port)
    if was_running:
        _console.print("  [dim]→[/dim]  stopping server…")
        _stop_server(port)

    # ── uv tool install from git URL ──────────────────────────────────────
    if is_git_url:
        if not shutil.which("uv"):
            typer.secho(
                "ERROR: uv not found — cannot upgrade. Install uv or run: pip install --upgrade git+<url>",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(1) from None
        _console.print("  [dim]→[/dim]  upgrading via uv…")
        result = subprocess.run(
            ["uv", "tool", "upgrade", "north"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            typer.secho(
                f"uv tool upgrade failed:\n{(result.stdout + result.stderr).strip()}",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(1) from None
        _console.print("  [dim green]✓[/dim green]  upgraded")

    # ── local git clone ───────────────────────────────────────────────────
    else:
        project_root = _find_project_root()
        if project_root is None:
            typer.secho(
                "ERROR: Could not locate the north project root.\n"
                "Install north with: uv tool install git+<your-repo-url>",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(1) from None
        if not (project_root / ".git").exists():
            typer.secho(
                "ERROR: Project directory has no .git — cannot pull.\nRun: uv sync",
                fg=typer.colors.YELLOW,
                err=True,
            )
            raise typer.Exit(1) from None

        _console.print("  [dim]→[/dim]  git pull…")
        pull_result = subprocess.run(["git", "pull"], cwd=project_root, capture_output=True, text=True)
        if pull_result.returncode != 0:
            typer.secho(f"git pull failed:\n{pull_result.stderr.strip()}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from None
        if "Already up to date" in pull_result.stdout:
            _console.print("  [dim green]✓[/dim green]  already up to date")
        else:
            log = _git_log_since_pull(project_root)
            for line in log[:8]:
                _console.print(f"  [dim]  {line}[/dim]")

        _console.print("  [dim]→[/dim]  syncing dependencies…")
        if not _sync_dependencies(project_root):
            typer.secho("Dependency sync failed — run 'uv sync' manually.", fg=typer.colors.YELLOW, err=True)

    _console.print()
    if restart and (was_running or typer.confirm("Start north now?", default=True)):
        _console.print("  [dim]→[/dim]  restarting…")
        proc = _start_server_process(port)
        _wait_for_server("127.0.0.1", port)
        typer.secho(f"✓ north updated and restarted (pid {proc.pid}).", fg=typer.colors.GREEN)
    else:
        typer.secho("✓ north updated. Run north start to restart.", fg=typer.colors.GREEN)


def _get_install_url() -> tuple[str | None, bool]:
    """Return (url, is_git_url) describing how north was installed.

    Reads the PEP 610 direct_url.json from the installed dist-info.
    is_git_url is True when north was installed with `uv tool install git+<url>`,
    meaning `uv tool upgrade north` is the right update path.
    """
    try:
        import json as _json
        from importlib.metadata import Distribution

        du = Distribution.from_name("north").read_text("direct_url.json")
        if du:
            data = _json.loads(du)
            url = data.get("url", "")
            is_git = "vcs_info" in data and not url.startswith("file://")
            return url, is_git
    except Exception:
        pass
    return None, False


def _find_project_root() -> Path | None:
    """Find the north project root (directory containing pyproject.toml + agents/).

    Walks up from __file__ first so it works whether north is installed as an
    editable or non-editable package (the latter places cli/main.py deep inside
    .venv/lib/python3.x/site-packages/). Falls back to walking up from CWD.
    """
    for p in Path(__file__).resolve().parents:
        if (p / "pyproject.toml").exists() and (p / "agents").is_dir():
            return p
    for p in [Path.cwd(), *Path.cwd().parents]:
        if (p / "pyproject.toml").exists() and (p / "agents").is_dir():
            return p
    return None


def _stop_server(port: int) -> None:
    """Stop a locally-running north server. Mirrors the logic in the stop command."""
    import signal

    from config.settings import settings

    pid_path = settings.north_home / "north.pid"
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
            os.kill(pid, signal.SIGTERM)
            pid_path.unlink(missing_ok=True)
            # Give up to 3 seconds for graceful shutdown, then SIGKILL.
            for _ in range(3):
                time.sleep(1)
                try:
                    os.kill(pid, 0)  # check if still alive
                except ProcessLookupError:
                    break
            else:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pid_path.unlink(missing_ok=True)
        except Exception as exc:
            typer.secho(f"  Warning: could not stop via PID: {exc}", fg=typer.colors.YELLOW)
    # Kill any remaining process still bound to the port (covers stale pid files).
    if _port_in_use("127.0.0.1", port):
        _kill_port("127.0.0.1", port)
        time.sleep(1)


def _start_server_process(port: int, project_root: Path | None = None) -> subprocess.Popen:
    """Spawn uvicorn and record the PID. Mirrors the logic in the start command."""
    from config.settings import settings

    log_path = settings.north_home / "north.log"
    pid_path = settings.north_home / "north.pid"
    workspace_path = settings.north_home / "workspace.txt"
    workspace = workspace_path.read_text(encoding="utf-8").strip() if workspace_path.exists() else str(Path.home())
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "orchestrator.app:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--log-level",
        "info",
    ]
    server_env = {**os.environ, "NORTH_NORTH_WORKSPACE": workspace}
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
    proc = subprocess.Popen(cmd, stdout=log_file, stderr=log_file, env=server_env)
    pid_path.write_text(str(proc.pid), encoding="utf-8")
    return proc


def _sync_dependencies(project_root: Path) -> bool:
    """Run uv sync, falling back to pip install -e . Returns True on success."""
    if shutil.which("uv") and _run_command(["uv", "sync"], cwd=project_root):
        return True
    return _run_command([sys.executable, "-m", "pip", "install", "-e", "."], cwd=project_root)


def _run_command(cmd: list[str], *, cwd: Path) -> bool:
    """Run a subprocess, printing its output only on failure. Returns True on success."""
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0 and (result.stdout or result.stderr):
        output = (result.stdout + result.stderr).strip()
        _console.print(f"  [dim red]{output}[/dim red]")
    return result.returncode == 0


def _git_describe(root: Path) -> str | None:
    try:
        r = subprocess.run(
            ["git", "log", "-1", "--pretty=format:%h %s"],
            capture_output=True,
            text=True,
            cwd=root,
            timeout=5,
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def _git_log_since_pull(root: Path) -> list[str]:
    """Return the subject lines of commits pulled in the last git pull."""
    try:
        r = subprocess.run(
            ["git", "log", "HEAD@{1}..HEAD", "--pretty=format:%h %s"],
            capture_output=True,
            text=True,
            cwd=root,
            timeout=5,
        )
        return [line for line in r.stdout.strip().splitlines() if line] if r.returncode == 0 else []
    except Exception:
        return []


if __name__ == "__main__":
    app()
