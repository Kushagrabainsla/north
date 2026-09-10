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
    north cron [list]
    north cron add "Plan my meals" --hour 7 [--minute 30] [--day mon] [--agent wellness]
    north cron set <name> [--hour 8] [--task "..."]
    north cron rm <name>
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
import contextlib
import datetime
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from cli._client import _api, _headers
from cli._server import (
    _docker_available,
    _find_compose_file,
    _find_project_root,
    _get_install_url,
    _is_north_server,
    _kill_port,
    _port_in_use,
    _resolve_workspace,
    _start_server_process,
    _sync_docker_secret,
)
from cli._task_stream import TaskStream, follow_task
from cli.constants import (
    _BASE_URL,
    _CONFIG_KEYS,
    _PROVIDERS,
    _VALID_DOCS,
    _Provider,
)
from cli.dictation import parse_hotkey as _parse_hotkey
from cli.dictation import wav_bytes as _wav_bytes
from cli.formatting import _reconstruct_task_output
from cli.provider_env import any_provider_configured, parse_provider_selection, provider_is_configured
from cli.provider_env import load_env_keys as _load_env_keys
from cli.provider_env import save_provider_key as _save_provider_key
from cli.provider_env import update_env_file as _update_env_file
from cli.scheduling import day_selection
from cli.startup_report import last_error_lines as _last_error_lines
from cli.tui import run as _tui_run
from cli.update_spec import pinned_git_spec as _pinned_git_spec
from cli.web_build import web_build_is_stale as _web_build_is_stale
from config.security import load_secret
from utils.time import local_timezone_name
from utils.version import NORTH_VERSION

_console = Console(force_terminal=sys.stdout.isatty())
# Errors go to stderr, and rich decides that at construction - `Console.print`
# has no `err=` argument. Passing one raises TypeError, which is survivable
# anywhere except where it was: the two calls that report a failed startup. A
# server that died on boot printed a TypeError about the error reporter instead
# of saying the server had died.
_err_console = Console(stderr=True, force_terminal=sys.stderr.isatty())


def _provider_is_configured(provider: _Provider, env_keys: dict[str, str]) -> bool:
    from inference.registry import get_provider_definition

    return provider_is_configured(
        provider, env_keys, lambda provider_id: get_provider_definition(provider_id).is_configured()
    )


def _any_provider_configured(env_file: Path) -> bool:
    from inference.registry import get_provider_definition

    return any_provider_configured(
        _PROVIDERS,
        _load_env_keys(env_file),
        lambda provider_id: get_provider_definition(provider_id).is_configured(),
    )


def _parse_provider_selection(raw: str) -> list[_Provider]:
    """Parse comma-separated 1-based indexes against the CLI provider list."""
    return parse_provider_selection(raw, _PROVIDERS)


def _prompt_provider_keys(env_file: Path, providers: list[_Provider]) -> bool:
    """Prompt the user for each provider's API key. Returns True if at least one was saved."""
    any_saved = False
    for p in providers:
        if p["auth_kind"] == "oauth_pkce":
            try:
                _login_codex_interactive(open_browser=True)
            except Exception as exc:
                typer.secho(f"  OpenAI Codex login failed: {exc}", fg=typer.colors.RED, err=True)
                continue
            any_saved = True
            continue
        typer.echo(f"\n  {p['name']}  -  get a key at {p['url']}")
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


def _login_codex_interactive(*, open_browser: bool) -> None:
    from inference.codex_auth import CodexCredentialProvider

    def show_url(url: str) -> None:
        typer.echo("\nOpen this URL to authenticate OpenAI Codex:\n")
        typer.echo(url)
        typer.echo("\nWaiting for the browser callback…")

    credentials = CodexCredentialProvider(authorization_callback=show_url)
    status = asyncio.run(credentials.login(open_browser=open_browser))
    account = f" ({status.account_id})" if status.account_id else ""
    typer.secho(f"✓ OpenAI Codex connected{account}.", fg=typer.colors.GREEN)
    typer.echo("Restart North if it is already running so the new provider is added to its model pools.")


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
        typer.secho("No valid selection - north cannot start.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    settings.north_home.mkdir(parents=True, exist_ok=True)
    if not _prompt_provider_keys(env_file, chosen):
        typer.secho("No API keys saved - north cannot start.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


app = typer.Typer(
    name="north",
    help="north - Personal Life Operating System CLI",
    no_args_is_help=False,
    add_completion=False,
    invoke_without_command=True,
)


# Set by the root callback; read by `_run_task` so --yolo works outside the TUI.
_YOLO = False


@app.callback()
def _root(
    ctx: typer.Context,
    yolo: bool = typer.Option(False, "--yolo", help="Auto-approve every approval prompt (shows a ⚠ YOLO badge)."),
) -> None:
    """north - Personal Life Operating System.

    Run without a subcommand to open the interactive TUI.
    """
    # Remembered for whichever subcommand runs next: `--yolo` is a root option,
    # but it used to reach only the TUI, so `north --yolo task "..."` parsed the
    # flag, ignored it, and still stopped dead at an approval prompt.
    global _YOLO
    _YOLO = yolo
    if ctx.invoked_subcommand is None:
        # No subcommand - boot the server if needed, then open the TUI.
        _launch_tui(yolo=yolo)


def _launch_tui(
    host: str = "127.0.0.1",
    port: int = 8000,
    workspace: str | None = None,
    yolo: bool = False,
) -> None:
    """Auto-start the server if not running, then launch the TUI."""
    if not _port_in_use(host, port) or not _is_north_server(host, port):
        _console.print("  [dim]server offline - starting…[/dim]")
        # Re-invoke `north start --no-chat` to start the server only, then TUI below.
        from config.security import load_secret
        from config.settings import settings

        settings.north_home.mkdir(parents=True, exist_ok=True)
        load_secret()

        resolved_workspace = _resolve_workspace(workspace)
        proc = _start_server_process(port, resolved_workspace, host=host)
        _wait_for_server(host, port, proc=proc)
        workspace = resolved_workspace

    base_url = f"http://{host}:{port}"
    headers = _headers()
    resolved_workspace = _resolve_workspace(workspace)

    import asyncio

    asyncio.run(_tui_run(base_url=base_url, headers=headers, workspace=resolved_workspace, yolo=yolo))


# ── task ─────────────────────────────────────────────────────────────────────


# A plain command, not a command group. As a group with a positional `prompt`,
# the first word of every prompt was parsed as a subcommand name first: the
# documented `north task cancel <id>` failed with "No such command <id>", and
# `-w` was only accepted before the prompt. Cancelling is `north cancel <id>`,
# which handles tasks and jobs alike.
@app.command("task")
def submit_task(
    prompt: str = typer.Argument(..., help="Prompt to submit as a new task."),
    workspace: str | None = typer.Option(
        None,
        "--workspace",
        "-w",
        help="Root directory agents can read/write. Defaults to git root of current directory.",
    ),
) -> None:
    """Submit a task and stream results live."""
    _run_task(prompt, workspace=_resolve_workspace(workspace))


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


def _submit_task(prompt: str, workspace: str | None) -> str:
    body: dict = {"prompt": prompt}
    if workspace:
        body["workspace"] = workspace
    return _api("POST", "/orchestrator/task", json=body).json()["task_id"]


def _answer_from_ledger(task_id: str) -> str:
    """The kept answer for a task that streamed no usable tokens."""
    try:
        entries = _api("GET", f"/orchestrator/ledger?task_id={task_id}&limit=20").json()
    except Exception:
        return "Task completed, but the result could not be retrieved."
    return _reconstruct_task_output(entries) or "Task completed."


def _print_panel(body: Markdown | Text, title: str) -> None:
    _console.print()
    _console.print(Panel(body, title=title, border_style="bright_black", padding=(1, 2)))


def _run_task(prompt: str, workspace: str | None = None) -> str:
    """Submit prompt, stream SSE pipeline steps live, then render the response. Returns output text."""
    task_id = _submit_task(prompt, workspace)
    try:
        feed = follow_task(TaskStream(task_id=task_id, console=_console, yolo=_YOLO))
    except KeyboardInterrupt:
        _console.print("[dim]Interrupted.[/dim]")
        return ""

    if feed.failure:
        _print_panel(Text(feed.failure, style="red"), "[dim]north - error[/dim]")
        return ""

    output_text = feed.answer or _answer_from_ledger(task_id)
    _print_panel(Markdown(output_text), "[dim]north[/dim]")
    return output_text


# ── stream (raw) ──────────────────────────────────────────────────────────────


@app.command("stream")
def stream_task(
    task_id: str = typer.Argument(..., help="Task ID to stream raw events for."),
) -> None:
    """Stream raw SSE events for a task (debug view)."""
    url = f"{_BASE_URL}/orchestrator/stream/{task_id}"
    _console.print(f"[dim]Streaming {task_id} - Ctrl+C to stop[/dim]\n")
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
        if doc_name == "soul.md":
            from utils.prompts import load_prompt

            try:
                doc_path.write_text(load_prompt("prompts/soul.md"), encoding="utf-8")
            except Exception:
                doc_path.touch()
        else:
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

ledger_app = typer.Typer(help="Ledger operations.", no_args_is_help=True)
app.add_typer(ledger_app, name="ledger")


@ledger_app.callback(invoke_without_command=True)
def show_ledger(
    ctx: typer.Context,
    limit: int = typer.Option(20, "--limit", "-n", help="Number of entries to show."),
    task_id: str | None = typer.Option(None, "--task", help="Filter by task ID."),
    agent: str | None = typer.Option(None, "--agent", help="Filter by agent name."),
    source: str | None = typer.Option(None, "--source", help="Filter by source type."),
) -> None:
    """Show recent ledger entries."""
    if ctx.invoked_subcommand is not None:
        return
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
        ts = datetime.datetime.fromisoformat(entry["timestamp"]).astimezone().strftime("%Y-%m-%d %H:%M")
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


@ledger_app.command("search")
def search_ledger(
    query: str = typer.Argument(..., help="Search query."),
    limit: int = typer.Option(20, "--limit", "-n", help="Max results."),
    agent: str | None = typer.Option(None, "--agent", help="Filter by agent."),
    source: str | None = typer.Option(None, "--source", help="Filter by source."),
) -> None:
    """Full-text search ledger entries (FTS5)."""
    params: dict[str, object] = {"q": query, "limit": limit}
    if agent:
        params["agent"] = agent
    if source:
        params["source"] = source

    response = _api("GET", "/orchestrator/ledger/search", params=params)
    results = response.json()
    if not results:
        _console.print("  [dim]no matches[/dim]")
        return

    _console.print()
    for r in results:
        entry = r["entry"]
        ts = datetime.datetime.fromisoformat(entry["timestamp"]).astimezone().strftime("%Y-%m-%d %H:%M")
        status = entry.get("status") or ""
        status_fmt = (
            f"[green]{status}[/green]"
            if status == "completed"
            else f"[red]{status}[/red]"
            if status == "failed"
            else f"[dim]{status}[/dim]"
        )
        _console.print(
            f"  [bright_black]{ts}[/bright_black]"
            f"  [{status_fmt}]{status}[/{status_fmt}]"
            f"  [dim]{entry.get('agent', '')}[/dim]"
        )
        _console.print(f"  [dim]{r['snippet']}[/dim]")
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
            f"[dim]{job['type']}[/dim]  {job['agent']}  "
            f"[bright_black]{job.get('scheduled_local', '')}[/bright_black]"
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


@app.command("cancel")
def cancel_any(
    target: str = typer.Argument("", help="Task or job ID to stop. Omit and pass --all to stop everything."),
    all_: bool = typer.Option(False, "--all", help="Stop ALL active tasks and pending/scheduled jobs."),
) -> None:
    """Stop anything: a task or job by ID, or everything in flight with --all.

    A running scheduled (cron) job is just an active task/job, so this stops it too.
    To stop a recurring schedule from ever firing again, use `north cron rm <name>`.
    """
    if all_:
        data = _api("POST", "/orchestrator/cancel-all").json()
        typer.secho(
            f"✓ Stopped {data['tasks_cancelled']} active task(s) and {data['jobs_cancelled']} pending job(s).",
            fg=typer.colors.YELLOW,
        )
        return
    if not target:
        typer.secho("Provide a task/job ID, or use --all to stop everything.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    data = _api("POST", f"/orchestrator/cancel/{target}").json()
    typer.secho(f"✓ Cancelled {data['cancelled']} {data['id']}.", fg=typer.colors.YELLOW)


# ── schedules ─────────────────────────────────────────────────────────────────

cron_app = typer.Typer(help="Recurring schedules: list, add, change, remove.", invoke_without_command=True)
app.add_typer(cron_app, name="cron")


def _day_selection(days: str | None) -> list[str] | str | None:
    """Turn --days into what the API takes, checking it here so errors land locally.

    "mon,wed" becomes ["mon", "wed"]; a group word like "weekdays" is passed
    through whole. Validated against the same reader the API uses, so the CLI
    cannot accept a spelling the server would reject, or vice versa.
    """
    if days is None:
        return None
    from tools.universal._schedules import parse_weekdays

    try:
        return day_selection(days, parse_weekdays)
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None


def _print_cron_entries(entries: list[dict]) -> None:
    if not entries:
        _console.print("  [dim]no schedules - add one with `north cron add`[/dim]")
        return
    # The footer names the local zone once, so a row only spells out its own zone
    # when the schedule is in a different one - that is the case worth reading.
    local_suffix = f" ({local_timezone_name()})"
    table = Table(box=None, padding=(0, 2), show_header=True, header_style="dim")
    table.add_column("name", no_wrap=True)
    table.add_column("when", no_wrap=True)
    table.add_column("next run", style="bright_black", no_wrap=True)
    table.add_column("agent", style="dim", no_wrap=True)
    table.add_column("task")
    for entry in entries:
        name = entry["name"]
        if entry.get("source") == "builtin":
            name = f"[dim]{name} (built-in)[/dim]"
        table.add_row(
            name,
            entry["schedule"].removesuffix(local_suffix),
            entry["next_run_local"],
            entry["agent"],
            entry["task"],
        )
    _console.print()
    _console.print(table)
    _console.print(f"  [dim]times shown in {local_timezone_name()}[/dim]")
    _console.print()


@cron_app.callback()
def cron_root(ctx: typer.Context) -> None:
    """Show recurring schedules (bare `north cron`), or run a subcommand."""
    if ctx.invoked_subcommand is None:
        _print_cron_entries(_api("GET", "/orchestrator/cron").json())


@cron_app.command("list")
def list_cron() -> None:
    """List recurring schedules, soonest first."""
    _print_cron_entries(_api("GET", "/orchestrator/cron").json())


@cron_app.command("add")
def add_cron(
    task: str = typer.Argument(..., help="What north should do, in plain language."),
    hour: int = typer.Option(..., "--hour", "-h", help="Hour to run, 0-23, your local time."),
    minute: int = typer.Option(0, "--minute", "-m", help="Minute to run, 0-59."),
    days: str | None = typer.Option(
        None,
        "--days",
        "-d",
        help="Days to run: mon…sun (comma-separated), or weekdays / weekends / daily. Omit for daily.",
    ),
    agent: str = typer.Option("general", "--agent", "-a", help="Agent that runs it."),
    name: str | None = typer.Option(None, "--name", help="Name to address it by (default: from the task)."),
) -> None:
    """Add a recurring schedule. Times are your local time."""
    body = {
        "name": name,
        "agent": agent,
        "task": task,
        "hour": hour,
        "minute": minute,
        "days": _day_selection(days),
    }
    entry = _api("POST", "/orchestrator/cron", json=body).json()
    typer.secho(f"✓ {entry['name']}: {entry['schedule']} - next run {entry['next_run_local']}.", fg=typer.colors.GREEN)


@cron_app.command("set")
def set_cron(
    name: str = typer.Argument(..., help="Schedule name (see `north cron`)."),
    hour: int | None = typer.Option(None, "--hour", "-h", help="New hour, 0-23, local."),
    minute: int | None = typer.Option(None, "--minute", "-m", help="New minute, 0-59."),
    days: str | None = typer.Option(
        None, "--days", "-d", help="New days: mon…sun (comma-separated), or weekdays / weekends / daily."
    ),
    task: str | None = typer.Option(None, "--task", help="New task text."),
    agent: str | None = typer.Option(None, "--agent", "-a", help="New agent."),
    pause: bool = typer.Option(False, "--pause", help="Pause it without deleting it."),
    resume: bool = typer.Option(False, "--resume", help="Resume a paused schedule."),
) -> None:
    """Change a schedule. Only the options you pass are changed."""
    if pause and resume:
        typer.secho("Pass either --pause or --resume, not both.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    body = {
        "hour": hour,
        "minute": minute,
        "days": _day_selection(days),
        "task": task,
        "agent": agent,
        "enabled": False if pause else (True if resume else None),
    }
    changes = {k: v for k, v in body.items() if v is not None}
    if not changes:
        typer.secho(
            "Nothing to change - pass --hour, --minute, --days, --task, --agent, --pause or --resume.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)
    entry = _api("PATCH", f"/orchestrator/cron/{name}", json=changes).json()
    typer.secho(f"✓ {entry['name']}: {entry['schedule']} - next run {entry['next_run_local']}.", fg=typer.colors.GREEN)


@cron_app.command("rm")
def remove_cron(
    name: str = typer.Argument(..., help="Schedule name (see `north cron`)."),
) -> None:
    """Remove a recurring schedule, so it never fires again."""
    _api("DELETE", f"/orchestrator/cron/{name}")
    typer.secho(f"✓ Removed schedule {name}.", fg=typer.colors.YELLOW)


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


@dataclass
class _AgentScaffold:
    """Everything needed to write a new agent's folder to disk."""

    name: str
    domain: str
    description: str
    model_pool: str
    tools: list[str]
    accepts: list[str]
    agentic: bool
    system_prompt: str


def _discover_universal_tool_names() -> set[str]:
    """Names of universal tools (auto-included for every agent) from the installed package."""
    try:
        import importlib.util as ilu

        spec = ilu.find_spec("tools.universal")
        if spec and spec.submodule_search_locations:
            udir = Path(list(spec.submodule_search_locations)[0])
            return {p.stem for p in udir.glob("*.py") if not p.stem.startswith("_")}
    except Exception:
        pass
    return set()


def _write_agent_scaffold(agent_dir: Path, scaffold: _AgentScaffold) -> None:
    """Write the agent.py / config.yaml / tools.yaml / prompts / README files to disk."""
    import yaml  # type: ignore[import-untyped]

    agent_dir.mkdir(parents=True)
    (agent_dir / "prompts").mkdir()

    class_name = "".join(word.title() for word in scaffold.name.split("_")) + "Agent"
    base_import = (
        "from agents.agentic_llm_agent import AgenticLLMAgent"
        if scaffold.agentic
        else "from agents.llm_agent import LLMAgent"
    )
    base_class = "AgenticLLMAgent" if scaffold.agentic else "LLMAgent"

    (agent_dir / "agent.py").write_text(
        f'"""{class_name} domain specialist.\n\nSee docs/CODING_STYLE.md Section 15.\n"""\n\n'
        f"from __future__ import annotations\n\n"
        f"{base_import}\n\n\n"
        f"class {class_name}({base_class}):\n"
        f'    """Domain specialist for {scaffold.domain}."""\n',
        encoding="utf-8",
    )
    (agent_dir / "config.yaml").write_text(
        yaml.dump(
            {
                "agent": scaffold.name,
                "domain": scaffold.domain,
                "model_pool": scaffold.model_pool,
                "accepts": scaffold.accepts,
                "output_format": "structured_json",
                "version": NORTH_VERSION,
                "class_name": class_name,
            },
            default_flow_style=False,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    universal = _discover_universal_tool_names()
    universal_requested = [t for t in scaffold.tools if t in universal]
    specialized_tools = [t for t in scaffold.tools if t not in universal]
    if universal_requested:
        typer.secho(
            f"  Note: {', '.join(universal_requested)} are universal - auto-included, omitted from tools.yaml",
            fg=typer.colors.BRIGHT_BLACK,
        )
    tools_comment = (
        "# Specialized tools for this agent. Universal tools are\n"
        "# automatically available to all agents and do not need to be listed here.\n"
    )
    tools_body = "tools:\n" + "".join(f"  - {t}\n" for t in specialized_tools) if specialized_tools else "tools: []\n"
    (agent_dir / "tools.yaml").write_text(tools_comment + tools_body, encoding="utf-8")

    (agent_dir / "prompts" / "system.md").write_text(scaffold.system_prompt, encoding="utf-8")
    (agent_dir / "README.md").write_text(
        f"# {scaffold.name.title()} Agent\n\n{scaffold.description}\n\n"
        f"**Domain:** {scaffold.domain}  \n**Pool:** {scaffold.model_pool}  "
        f"\n**Accepts:** {', '.join(scaffold.accepts)}\n",
        encoding="utf-8",
    )


@agent_app.command("create")
def create_agent(
    name: str | None = typer.Option(None, "--name", help="Agent name (slug, lowercase)."),
    domain: str | None = typer.Option(None, "--domain", help="Domain (e.g. health, finance)."),
    description: str | None = typer.Option(None, "--description", help="One-line description."),
    model_pool: str = typer.Option("fast_cheap", "--pool", help="Model pool: reasoning / fast_cheap / high_volume."),
    output_dir: Path | None = typer.Option(None, "--output-dir", help="Agent folder to create in (default: ./agents/)"),
) -> None:
    """Interactively scaffold a new domain-specialist agent."""

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
        "  list_dir, search_files, schedule_task) are auto-included - skip them",
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

    _write_agent_scaffold(
        agent_dir,
        _AgentScaffold(
            name=name,
            domain=domain,
            description=description,
            model_pool=model_pool,
            tools=tools,
            accepts=accepts,
            agentic=agentic,
            system_prompt=system_prompt,
        ),
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
            "  Note: could not find prompts/planner.md - add the domain row manually.",
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
    name: str = typer.Argument(..., help="Agent name (run `north agents` to list; e.g. coder, researcher, reviewer)."),
    task: str = typer.Argument(..., help="Task description for the agent."),
) -> None:
    """Manually trigger a specific agent (runs that agent directly, not the planner)."""
    response = _api("POST", "/orchestrator/agent/run", json={"agent": name, "task": task})
    data = response.json()
    typer.secho(f"✓ Task submitted: {data['task_id']}", fg=typer.colors.GREEN)
    typer.echo(f"  Stream with:  north stream {data['task_id']}")


# ── inference ─────────────────────────────────────────────────────────────────

# ── provider authentication ─────────────────────────────────────────────────

auth_app = typer.Typer(help="Inference provider authentication.", no_args_is_help=True)
app.add_typer(auth_app, name="auth")


@auth_app.command("login")
def auth_login(
    provider: str = typer.Argument("openai-codex", help="Provider to authenticate."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Print the login URL without opening it."),
) -> None:
    """Log in to an OAuth inference provider."""
    from inference.registry import AuthKind, get_provider_definition

    try:
        definition = get_provider_definition(provider)
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc
    if definition.auth_kind is not AuthKind.OAUTH_PKCE or definition.id != "openai_codex":
        typer.secho(
            f"{definition.display_name} uses an API key; configure {definition.env_key}.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(2)
    _login_codex_interactive(open_browser=not no_browser)


@auth_app.command("status")
def auth_status() -> None:
    """Show configured inference credentials without revealing secrets."""
    from config.settings import settings
    from inference.codex_auth import CodexCredentialProvider
    from inference.registry import PROVIDER_DEFINITIONS, AuthKind

    table = Table(show_header=True, header_style="bold")
    table.add_column("Provider")
    table.add_column("Authentication")
    table.add_column("Status")
    table.add_column("Account")
    for definition in PROVIDER_DEFINITIONS:
        if definition.auth_kind is AuthKind.OAUTH_PKCE and definition.id == "openai_codex":
            status = CodexCredentialProvider().status()
            detail = "connected" if status.configured and not status.needs_login else status.detail
            account = status.account_id or "—"
        else:
            configured = definition.is_configured(settings)
            detail = "configured" if configured else "not configured"
            account = "—"
        table.add_row(definition.display_name, definition.auth_kind.value, detail, account)
    _console.print(table)


@auth_app.command("logout")
def auth_logout(provider: str = typer.Argument("openai-codex", help="Provider to disconnect.")) -> None:
    """Delete North's locally stored OAuth credentials."""
    from inference.codex_auth import CodexCredentialProvider
    from inference.registry import get_provider_definition

    try:
        definition = get_provider_definition(provider)
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc
    if definition.id != "openai_codex":
        typer.secho(
            f"{definition.display_name} uses an API key; remove {definition.env_key} instead.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(2)
    asyncio.run(CodexCredentialProvider().logout())
    typer.secho("✓ OpenAI Codex credentials removed from North.", fg=typer.colors.GREEN)


# ── inference ────────────────────────────────────────────────────────────────

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

    _print_cache_usage(period)
    _console.print()


def _print_cache_usage(period: str) -> None:
    """Show how much of the prompt each provider served from its cache.

    An agent loop re-sends the same opening block on every turn, so this is the
    difference between paying for it once and paying for it twenty times. A cache
    is a prefix match, and one changed byte near the front silently drops the hit
    rate to zero with no error - so the number is only useful if it is shown.
    """
    import sqlite3

    from config.settings import settings

    days = {"day": 1, "week": 7, "month": 30}.get(period, 7)
    db = settings.north_home / "ledger.db"
    if not db.exists():
        return
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        # A ledger written before caching was recorded has no such column. That is
        # not "no data" - it is "every call so far was uncached", which is worth
        # saying out loud rather than showing an empty section.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(ledger)")}
        # Older ledgers have no cache columns. Treat absent fields as unavailable,
        # not as proof a cache never missed.
        cached_expr = "SUM(COALESCE(cached_tokens,0))" if "cached_tokens" in columns else "0"
        missed_expr = "SUM(COALESCE(cache_missed_tokens,0))" if "cache_missed_tokens" in columns else "0"
        miss_count_expr = "SUM(COALESCE(cache_miss_count,0))" if "cache_miss_count" in columns else "0"
        rows = conn.execute(
            f"SELECT model_used, SUM(COALESCE(tokens_in,0)), {cached_expr}, {missed_expr}, {miss_count_expr}"
            " FROM ledger WHERE model_used IS NOT NULL"
            f" AND created_at >= datetime('now', '-{days} days')"
            " GROUP BY model_used HAVING SUM(COALESCE(tokens_in,0)) > 0"
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return
    if not rows:
        return

    total_in = sum(r[1] for r in rows)
    total_cached = sum(r[2] for r in rows)
    total_missed = sum(r[3] for r in rows)
    miss_count = sum(r[4] for r in rows)
    _console.print("\n  [dim]prompt cache[/dim]")
    if total_cached == 0:
        _console.print(
            f"    [bright_black]0 of {total_in:,} prompt tokens reused"
            " - no configured provider is caching[/bright_black]"
        )
        if miss_count:
            _console.print(
                f"    [yellow]likely cache misses:[/yellow] {total_missed:,} prompt tokens across {miss_count} turn(s)"
            )
        return
    share = 100 * total_cached / total_in
    _console.print(f"    [dim]{'reused across all models':<40}[/dim]  {total_cached:,} / {total_in:,} ({share:.0f}%)")
    for model, tokens_in, cached, _missed, _misses in sorted(rows, key=lambda r: -r[2]):
        if cached:
            pct = 100 * cached / tokens_in
            _console.print(f"    [dim]{str(model)[:40]:<40}[/dim]  {cached:,} / {tokens_in:,} ({pct:.0f}%)")
    if miss_count:
        _console.print(
            f"    [yellow]likely cache misses:[/yellow] {total_missed:,} prompt tokens across {miss_count} turn(s)"
        )
    _console.print()


@app.command("models")
@inference_app.command("models")
def inference_models() -> None:
    """Show current model pool state and discovered models."""
    response = _api("GET", "/orchestrator/inference/models")
    pools = response.json()
    _console.print()
    # Pools are price-derived bins. They no longer decide anything - routing
    # ranks the whole catalog per part of a task - and showing them as if they
    # did is how a user ends up tuning the wrong thing.
    _console.print(
        "  [yellow]Pools below are a catalog view only.[/yellow] "
        "[bright_black]Routing selects per part from fetched facts - "
        "run `north routing` to see the actual decisions.[/bright_black]"
    )
    _console.print()
    for pool_name, pool_data in pools.items():
        models = pool_data.get("models", [])
        _console.print(f"  [bold white]{pool_name}[/bold white]  [bright_black]{len(models)} models[/bright_black]")
        for entry in models:
            _console.print(f"    [dim]{entry['id']}[/dim]  [bright_black]({entry['provider']})[/bright_black]")
        _console.print()


@app.command("speed")
@inference_app.command("speed")
def inference_speed(
    limit: int = typer.Option(30, "--limit", help="How many models to show."),
) -> None:
    """Show how fast each model has actually been on this install.

    Reads ~/.north/tools.db directly, so it works with the server offline.
    Routing ranks on quality and price; this is the third signal it uses - a
    model measured below the floor is pushed to the tail of its chain, so a free
    model that takes two minutes stops being preferred over one that answers in
    five seconds. A model with too few samples is not yet judged either way.
    """
    from config.settings import settings
    from inference.constants import _MODEL_SPEED_FLOOR_TOK_PER_SEC, _MODEL_SPEED_MIN_SAMPLES
    from tools.confidence import ConfidenceTracker

    speeds = ConfidenceTracker(db_path=settings.north_home / "tools.db").load_model_speeds_sync()
    _console.print()
    if not speeds:
        _console.print("  [bright_black]No model has been timed yet.[/bright_black]")
        _console.print()
        return

    table = Table(box=None, padding=(0, 2), show_header=True, header_style="dim")
    table.add_column("model", style="bold white")
    table.add_column("provider")
    table.add_column("tok/s", justify="right")
    table.add_column("samples", justify="right")
    table.add_column("ranking")
    rows = sorted(speeds.items(), key=lambda item: item[1][0])
    for (model_id, provider), (rate, samples) in rows[: max(1, limit)]:
        if samples < _MODEL_SPEED_MIN_SAMPLES:
            verdict = "[bright_black]not yet judged[/bright_black]"
        elif rate < _MODEL_SPEED_FLOOR_TOK_PER_SEC:
            verdict = "[yellow]tailed - too slow[/yellow]"
        else:
            verdict = "[green]normal[/green]"
        table.add_row(model_id, provider, f"{rate:.1f}", str(samples), verdict)
    _console.print(table)
    _console.print(
        f"  [bright_black]Below {_MODEL_SPEED_FLOOR_TOK_PER_SEC:.0f} tok/s over "
        f"{_MODEL_SPEED_MIN_SAMPLES}+ calls is ranked last, never removed.[/bright_black]"
    )
    _console.print()


@app.command("routing")
@inference_app.command("routing")
def inference_routing(
    task: str = typer.Option("", "--task", help="Only decisions for this task id."),
    part: str = typer.Option("", "--part", help="Only decisions for this part, e.g. coder."),
    limit: int = typer.Option(20, "--limit", help="How many decisions to show."),
) -> None:
    """Show why each part of a task ran on the model it ran on.

    Reads ~/.north/models.db directly, so it works with the server offline. Each
    row is one model selection: what the part needed, how many models were
    considered, why the ones ahead of the winner were passed over, and what
    happened. This is the answer to "why did the coder run on a free model?".
    """
    from config.settings import settings
    from inference.decisions import DecisionLog

    log = DecisionLog(settings.north_home / "models.db")
    rows = log.recent(task_id=task or None, part=part or None, limit=limit)
    _console.print()
    if not rows:
        _console.print("  [bright_black]No routing decisions recorded yet.[/bright_black]")
        _console.print("  [bright_black]They are written as calls are made, under NORTH_ROUTING=chain.[/bright_black]")
        _console.print()
        return
    for row in rows:
        outcome = row.get("outcome") or "?"
        colour = {"success": "green", "exhausted": "red", "diverged": "yellow"}.get(outcome, "white")
        chosen = row.get("chosen_model") or "-"
        _console.print(
            f"  [bold white]{row['part']}[/bold white] "
            f"[{colour}]{outcome}[/{colour}]  [dim]{chosen}[/dim] "
            f"[bright_black]({row.get('chosen_provider') or '-'})[/bright_black]"
        )
        needs = row.get("requirements") or {}
        if needs:
            rendered = ", ".join(f"{k}={v}" for k, v in needs.items())
            _console.print(f"    [bright_black]needs {rendered}[/bright_black]")
        skipped = row.get("skipped") or []
        _console.print(
            f"    [bright_black]considered {row.get('considered', 0)}, passed over {len(skipped)}[/bright_black]"
        )
        for skip in skipped[:5]:
            _console.print(
                f"      [dim]{skip.get('model', '?')}[/dim] "
                f"[bright_black]{skip.get('provider', '?')} - {skip.get('reason', '?')}[/bright_black]"
            )
        if len(skipped) > 5:
            _console.print(f"      [bright_black]... {len(skipped) - 5} more[/bright_black]")
        _console.print()


@app.command("limits")
def limits() -> None:
    """Show provider/model rate-limit & cooldown status with precise reset times.

    Reads the on-disk status file (~/.north/rate_limit_status.json) directly, so
    it works even when the server is offline. Shows, for every provider/model
    that is currently rate-limited or billing-exhausted, exactly when it will be
    usable again and which signal the provider gave (retry-after, X-RateLimit-Reset,
    etc.), plus the tier (free/paid) and request limit/remaining when reported.
    """
    from config.settings import settings
    from inference.rate_limit_status import format_status_table

    table = format_status_table(settings.north_home / "rate_limit_status.json")
    _console.print()
    _console.print(table)
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
        # A stage can answer and still not finish: it never wrote the artifact
        # it declares in `produces`. That reads as success everywhere else.
        t.add_column("incomplete", justify="right")
        t.add_column("cost $", justify="right")
        t.add_column("p50 ms", justify="right")
        t.add_column("p95 ms", justify="right")
        for a in data["by_agent"]:
            t.add_row(
                a["agent"],
                str(a["tasks"]),
                f"{a['success_rate'] * 100:.0f}%",
                (f"[yellow]{a['incomplete']}[/yellow]" if a.get("incomplete") else " - "),
                f"{a['cost_usd']:.6f}",
                str(a["p50_ms"]) if a["p50_ms"] is not None else " - ",
                str(a["p95_ms"]) if a["p95_ms"] is not None else " - ",
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


@app.command("status")
def status() -> None:
    """Show a live snapshot of the running north instance.

    Aggregates server health, configured inference providers, model-pool
    sizes, registered agents, and the active strategy into one view.
    """
    # Server health
    try:
        health = _api("GET", "/health").json()
        server_ok = health.get("status") == "ok"
    except Exception:
        server_ok = False
        health = {}

    _console.print()
    _console.print("  [bold white]north status[/bold white]")

    # ── Server ──
    health_icon = "[green]●[/green]" if server_ok else "[red]●[/red]"
    uptime = health.get("uptime_seconds")
    uptime_str = f"  [dim]uptime {int(uptime)}s[/dim]" if uptime is not None else ""
    status_text = "healthy" if server_ok else "down"
    _console.print(f"  {health_icon} server  [bright_black]{status_text}[/bright_black]{uptime_str}")

    if not server_ok:
        _console.print()
        return

    # ── Strategy ──
    try:
        settings = _api("GET", "/orchestrator/settings").json()
        strategy = settings.get("strategy", "cruise")
        approval = settings.get("approval_mode", "interactive")
    except Exception:
        strategy, approval = "?", "?"
    _console.print(f"  [dim]strategy  [/dim] {strategy}   [bright_black](approval: {approval})[/bright_black]")

    # ── Inference models per pool ──
    try:
        pools = _api("GET", "/orchestrator/inference/models").json()
    except Exception:
        pools = {}
    if pools:
        parts = []
        for pool_name, pool_data in pools.items():
            count = len(pool_data.get("models", []))
            style = "white" if count else "bright_black"
            parts.append(f"[{style}]{pool_name}={count}[/{style}]")
        _console.print("  [dim]models    [/dim] " + "  ".join(parts))

    # ── Providers (from settings, which keys are present) ──
    try:
        from config.settings import settings as cfg

        providers = []
        for key, label in (
            ("openrouter_api_key", "openrouter"),
            ("groq_api_key", "groq"),
            ("gemini_api_key", "gemini"),
            ("opencode_zen_api_key", "opencode_zen"),
        ):
            if getattr(cfg, key, "").strip():
                providers.append(f"[green]{label}[/green]")
            else:
                providers.append(f"[bright_black]{label}[/bright_black]")
        _console.print("  [dim]providers [/dim] " + "  ".join(providers))
    except Exception:
        pass

    # ── Browser ──
    # The one capability that depends on something north cannot install for you,
    # and whose absence is otherwise silent until an agent tries to browse.
    try:
        from tools.universal.browser import browser_availability

        state, detail = browser_availability()
        style = {"available": "green", "on demand": "yellow"}.get(state, "bright_black")
        _console.print(f"  [dim]browser   [/dim] [{style}]{state}[/{style}]  [bright_black]{detail}[/bright_black]")
    except Exception:
        pass

    # ── Agents ──
    try:
        agents = _api("GET", "/orchestrator/agents").json()
        names = ", ".join(a["name"] for a in agents)
        _console.print(f"  [dim]agents    [/dim] [white]{len(agents)}[/white]")
        _console.print(f"             [bright_black]{names}[/bright_black]")
    except Exception:
        pass

    _console.print()


# ── dictate ───────────────────────────────────────────────────────────────────


_DICTATE_DEPENDENCIES = ("numpy", "sounddevice", "pynput")
_TRANSCRIBE_TIMEOUT_SECONDS = 60.0


class _PushToTalk:
    """Hold-to-talk capture: records while the hotkey is held, sends on release."""

    def __init__(self, hotkey: str, sample_rate: int) -> None:
        self._required_keys = _parse_hotkey(hotkey)
        self._sample_rate = sample_rate
        self._held: set = set()
        self._frames: list = []
        self._recording = False

    def listen(self) -> None:
        """Capture and submit until the user interrupts."""
        import sounddevice as sd
        from pynput import keyboard as kb

        stream = sd.InputStream(
            samplerate=self._sample_rate,
            channels=1,
            dtype="int16",
            callback=self._on_audio,
        )
        stream.start()
        listener = kb.Listener(on_press=self._on_press, on_release=self._on_release)
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

    def _on_audio(self, indata, frame_count, time_info, status) -> None:  # type: ignore[no-untyped-def]
        if self._recording:
            self._frames.append(indata.copy())

    def _on_press(self, key: object) -> None:
        self._held.add(key)
        if self._required_keys.issubset(self._held) and not self._recording:
            self._frames.clear()
            self._recording = True
            typer.secho("  ● Recording…", fg=typer.colors.RED)

    def _on_release(self, key: object) -> None:
        self._held.discard(key)
        if self._recording and not self._required_keys.issubset(self._held):
            self._recording = False
            typer.secho("  ■ Processing…", fg=typer.colors.YELLOW)
            self._submit(self._frames[:])

    def _submit(self, captured: list) -> None:
        if not captured:
            typer.echo("  (nothing recorded)")
            return
        text = self._transcribe(_wav_bytes(captured, self._sample_rate))
        if not text:
            return
        typer.secho(f"  ✎ {text}", fg=typer.colors.CYAN)
        try:
            response = _api("POST", "/orchestrator/task", json={"prompt": text})
            typer.secho(f"  ✓ Task submitted: {response.json().get('task_id', '?')}", fg=typer.colors.GREEN)
        except Exception as exc:
            typer.secho(f"  ERROR submitting task: {exc}", fg=typer.colors.RED, err=True)

    def _transcribe(self, wav_bytes: bytes) -> str:
        """The spoken text, or "" when nothing usable came back."""
        # Raw bytes, so this bypasses the _api helper.
        try:
            with httpx.Client(timeout=_TRANSCRIBE_TIMEOUT_SECONDS) as client:
                response = client.post(
                    f"{_BASE_URL}/orchestrator/transcribe",
                    content=wav_bytes,
                    headers={**_headers(), "Content-Type": "audio/wav"},
                )
                response.raise_for_status()
            text = str(response.json().get("text", "")).strip()
        except Exception as exc:
            typer.secho(f"  ERROR transcribing: {exc}", fg=typer.colors.RED, err=True)
            return ""
        if not text:
            typer.echo("  (empty transcript)")
        return text


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
    missing = [name for name in _DICTATE_DEPENDENCIES if importlib.util.find_spec(name) is None]
    if missing:
        typer.secho(
            f"ERROR: Missing dependency: {', '.join(missing)}. Install with: uv add sounddevice pynput",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1) from None

    typer.secho(
        f"★ north dictate  (hold {hotkey} to record, Ctrl+C to quit)",
        fg=typer.colors.BRIGHT_WHITE,
        bold=True,
    )
    _PushToTalk(hotkey, sample_rate).listen()


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

    field_name, cast = _CONFIG_KEYS[key]
    try:
        cast(value)
    except (ValueError, TypeError):
        typer.secho(
            f"Invalid value {value!r} for {key}: expected {getattr(cast, '__name__', 'valid value')}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1) from None

    env_file = settings.north_home / ".env"
    settings.north_home.mkdir(parents=True, exist_ok=True)
    env_key = f"NORTH_{field_name.upper()}"
    _update_env_file(env_file, env_key, value)

    # Mask sensitive credentials in feedback
    if "key" in field_name or "token" in field_name or "secret" in field_name:
        display_val = f"{value[:8]}...{value[-4:]}" if len(value) > 12 else "(hidden)"
    else:
        display_val = value

    typer.secho(f"✓ {env_key}={display_val}", fg=typer.colors.GREEN)
    typer.echo("  Saved to ~/.north/.env (restart north to apply).")


@config_app.command("get")
def config_get(
    key: str = typer.Argument(..., help=f"Config key. Valid: {', '.join(_CONFIG_KEYS)}"),
) -> None:
    """Read a configuration value from the current environment / .env."""
    from config.settings import settings

    if key not in _CONFIG_KEYS:
        typer.secho(
            f"Unknown key {key!r}. Valid keys: {', '.join(_CONFIG_KEYS)}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1) from None

    field_name, _ = _CONFIG_KEYS[key]
    val = getattr(settings, field_name, None)
    if "key" in field_name or "token" in field_name or "secret" in field_name:
        display_val = (
            f"{str(val)[:8]}...{str(val)[-4:]}" if val and len(str(val)) > 12 else ("(set)" if val else "(not set)")
        )
    else:
        display_val = str(val) if val is not None and str(val) else "(not set)"
    typer.echo(f"{key} = {display_val}")


@config_app.command("list")
def config_list() -> None:
    """List all supported configuration keys and their current values."""
    from config.settings import settings

    _console.print()
    _console.print("  [bold white]north configuration[/bold white]")
    _console.print(f"  [bright_black]{'─' * 58}[/bright_black]")
    for key, (field_name, _) in sorted(_CONFIG_KEYS.items()):
        val = getattr(settings, field_name, None)
        if "key" in field_name or "token" in field_name or "secret" in field_name:
            display_val = (
                f"[yellow]{str(val)[:8]}...{str(val)[-4:]}[/yellow]"
                if val and len(str(val)) > 12
                else ("[yellow](set)[/yellow]" if val else "[dim](not set)[/dim]")
            )
        else:
            display_val = f"[green]{val}[/green]" if val not in ("", None, ()) else "[dim](not set)[/dim]"
        _console.print(f"  [cyan]{key:<28}[/cyan] [bright_black]→[/bright_black]  {display_val}")
    _console.print()


app.add_typer(config_app, name="config")


@app.command("setup")
def setup_interactive() -> None:
    """Interactive setup wizard to configure API keys, Telegram, and system settings."""
    from config.settings import settings

    env_file = settings.north_home / ".env"
    settings.north_home.mkdir(parents=True, exist_ok=True)

    _console.print()
    _console.print("  [bold cyan]✨ North Configuration Setup[/bold cyan]")
    _console.print(f"  [bright_black]{'─' * 55}[/bright_black]")
    _console.print("  Press [bold]Enter[/bold] to keep existing values or skip optional settings.\n")

    def _hint(val: str | None) -> str:
        if not val:
            return ""
        if len(val) > 12:
            return f" [dim](current: {val[:6]}...{val[-4:]})[/dim]"
        return f" [dim](current: {val})[/dim]"

    # 1. Inference Provider Keys
    openrouter = typer.prompt(
        f"  OpenRouter API Key{_hint(settings.openrouter_api_key)}",
        default=settings.openrouter_api_key or "",
        show_default=False,
    ).strip()
    if openrouter:
        _update_env_file(env_file, "NORTH_OPENROUTER_API_KEY", openrouter)

    groq = typer.prompt(
        f"  Groq API Key{_hint(settings.groq_api_key)}", default=settings.groq_api_key or "", show_default=False
    ).strip()
    if groq:
        _update_env_file(env_file, "NORTH_GROQ_API_KEY", groq)

    gemini = typer.prompt(
        f"  Google Gemini API Key{_hint(settings.gemini_api_key)}",
        default=settings.gemini_api_key or "",
        show_default=False,
    ).strip()
    if gemini:
        _update_env_file(env_file, "NORTH_GEMINI_API_KEY", gemini)

    # 2. Telegram Gateway Setup
    _console.print()
    setup_tg = typer.confirm("  Configure Telegram Bot integration?", default=bool(settings.telegram_bot_token))
    if setup_tg:
        tg_token = typer.prompt(
            f"  Telegram Bot Token (from @BotFather){_hint(settings.telegram_bot_token)}",
            default=settings.telegram_bot_token or "",
            show_default=False,
        ).strip()
        if tg_token:
            _update_env_file(env_file, "NORTH_TELEGRAM_BOT_TOKEN", tg_token)

        tg_chat = typer.prompt(
            f"  Allowed Telegram Chat/User ID{_hint(settings.telegram_allowed_chat_ids)}",
            default=settings.telegram_allowed_chat_ids or "",
            show_default=False,
        ).strip()
        if tg_chat:
            _update_env_file(env_file, "NORTH_TELEGRAM_ALLOWED_CHAT_IDS", tg_chat)

    _console.print()
    typer.secho("  ✓ Configuration saved to ~/.north/.env", fg=typer.colors.GREEN, bold=True)
    _console.print("  Run [bold green]north start[/bold green] to launch North.\n")


# ── port helpers ──────────────────────────────────────────────────────────────


def _wait_for_server(
    host: str,
    port: int,
    timeout: int = 90,
    proc: subprocess.Popen | None = None,
) -> None:
    """Poll until the server responds or timeout expires.

    When *proc* is supplied the loop also checks for early process exit so a
    crash during startup is surfaced immediately rather than waiting the full
    *timeout* seconds.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            _report_startup_failure(f"server process exited unexpectedly (code {proc.returncode})")
        if _is_north_server(host, port):
            _console.print("  [dim green]✓[/dim green]  server ready")
            return
        time.sleep(0.25)
    _report_startup_failure("server did not respond in time")


def _report_startup_failure(what: str) -> None:
    """Say the server failed, and where the reason is written down.

    The traceback goes to the log, not to this terminal - the server is a
    subprocess with its output redirected there. Without this pointer the CLI
    reports that something failed and gives no way to find out what, which is a
    long way to walk for a one-line error.
    """
    from config.settings import settings

    _err_console.print(f"  [red]{what}[/red]")
    log = settings.north_home / "north.log"
    if log.exists():
        _err_console.print(f"  [dim]the reason is at the end of {log}[/dim]")
        for line in _last_error_lines(log):
            _err_console.print(f"  [red]{line}[/red]")
    raise typer.Exit(1) from None


@dataclass(frozen=True)
class _StartOptions:
    """How north should come up, as the start command's flags asked for it."""

    host: str
    port: int
    workspace: str
    reload: bool = False
    chat: bool = True


def _print_start_header(mode: str, rows: list[tuple[str, str]]) -> None:
    _console.print()
    _console.print(f"  [bold white]north[/bold white]  [bright_black]{mode}[/bright_black]")
    _console.print(f"  [bright_black]{'─' * 44}[/bright_black]")
    for label, value in rows:
        _console.print(f"  [dim]{label:<11}[/dim]  {value}")
    _console.print()


def _start_with_docker(options: _StartOptions, compose_file: Path) -> None:
    _print_start_header(
        "docker",
        [
            ("compose", str(compose_file)),
            ("address", f"http://127.0.0.1:{options.port}"),
            ("workspace", options.workspace),
        ],
    )
    result = subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "up", "--build", "--detach"],
        env={**os.environ, "NORTH_NORTH_WORKSPACE": options.workspace},
        check=False,
    )
    if result.returncode != 0:
        typer.secho("Docker Compose failed to start.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None

    _wait_for_server("127.0.0.1", options.port)
    _sync_docker_secret(compose_file)
    if options.chat:
        _launch_tui(host="127.0.0.1", port=options.port, workspace=options.workspace)


def _free_the_port(options: _StartOptions) -> bool:
    """Make the port available. False when north is already serving on it."""
    if not _port_in_use(options.host, options.port):
        return True
    if _is_north_server(options.host, options.port):
        return False

    typer.secho(f"Port {options.port} is in use by another application.", fg=typer.colors.YELLOW)
    if not typer.confirm("Kill the existing process and restart?", default=False):
        raise typer.Exit(0)
    typer.echo(f"Stopping process on port {options.port}…")
    if not _kill_port(options.host, options.port):
        typer.secho(
            f"Could not stop the existing process. Try killing port {options.port} manually.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1) from None
    time.sleep(1)
    return True


def _start_locally(options: _StartOptions) -> None:
    from config.settings import settings

    settings.north_home.mkdir(parents=True, exist_ok=True)
    (settings.north_home / "tasks").mkdir(parents=True, exist_ok=True)
    (settings.north_home / "context").mkdir(parents=True, exist_ok=True)
    _ensure_api_keys()
    load_secret()

    if not _free_the_port(options):
        typer.secho(f"north is already running on port {options.port}.", fg=typer.colors.YELLOW)
        if options.chat:
            typer.echo("")
            _launch_tui(host=options.host, port=options.port, workspace=options.workspace)
        return

    _print_start_header(
        "local",
        [
            ("address", f"http://{options.host}:{options.port}"),
            ("workspace", options.workspace),
            ("home", str(settings.north_home)),
            ("logs", str(settings.north_home / "north.log")),
        ],
    )
    proc = _start_server_process(options.port, options.workspace, host=options.host, reload=options.reload)
    _wait_for_server(options.host, options.port, proc=proc)

    if not options.chat:
        typer.secho(f"north running (pid {proc.pid}). Stop with: north stop", fg=typer.colors.GREEN)
        return
    _launch_tui(host=options.host, port=options.port, workspace=options.workspace)


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
        help="Root directory agents can read/write. Defaults to the git root of the current directory.",
    ),
) -> None:
    """Start north, then drop into interactive chat.

    Runs locally with uvicorn by default - the right choice for personal use
    on your own machine. Pass --docker for server or headless deployments.
    Pass --no-chat to start the server without entering the chat REPL.
    """
    options = _StartOptions(
        host=host,
        port=port,
        workspace=_resolve_workspace(workspace),
        reload=reload,
        chat=not no_chat,
    )
    if not docker:
        _start_locally(options)
        return

    if not _docker_available():
        typer.secho("Docker not found. Install Docker or run without --docker.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None
    compose_file = _find_compose_file()
    if compose_file is None:
        typer.secho(
            "No docker-compose.yml found. Run from the project root or omit --docker.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1) from None
    _start_with_docker(options, compose_file)


def _ensure_web_build() -> None:
    """Build the frontend automatically when running from a source checkout."""
    web_dir = Path(__file__).resolve().parents[1] / "web"
    if not _web_build_is_stale(web_dir):
        return
    if not (web_dir / "package.json").is_file():
        if (web_dir / "dist" / "index.html").is_file():
            return  # Installed package: compiled assets are present, sources are not.
        typer.secho("Web assets are missing and the frontend source is unavailable.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    npm = shutil.which("npm")
    if npm is None:
        typer.secho(
            "Web assets are stale, but npm is not installed. Install Node.js/npm and retry.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)
    _console.print("  [dim]web assets stale; rebuilding frontend…[/dim]")
    result = subprocess.run([npm, "run", "build"], cwd=web_dir, check=False)
    if result.returncode != 0:
        typer.secho("Frontend build failed; see the output above.", fg=typer.colors.RED, err=True)
        raise typer.Exit(result.returncode or 1)


@app.command("web")
def web(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind host."),
    port: int = typer.Option(8000, "--port", "-p", help="North server port."),
    workspace: str | None = typer.Option(
        None,
        "--workspace",
        "-w",
        help="Root directory agents can read/write. Defaults to the current project root.",
    ),
    docker: bool = typer.Option(False, "--docker", help="Start the server through Docker Compose."),
    no_open: bool = typer.Option(False, "--no-open", help="Start the server without opening a browser."),
) -> None:
    """Start North's web cockpit and open it in the default browser."""
    _ensure_web_build()
    if not _port_in_use(host, port) or not _is_north_server(host, port):
        start(host=host, port=port, docker=docker, no_chat=True, workspace=workspace)
    url = f"http://{host}:{port}/app/"
    _console.print(f"  [dim]web cockpit [/dim]  {url}")
    if not no_open:
        webbrowser.open(url)


@app.command("stop")
def stop(
    port: int = typer.Option(8000, "--port", "-p", help="Port to stop."),
    all: bool = typer.Option(False, "--all", "-a", help="Kill all running north processes and background jobs."),
    docker: bool = typer.Option(False, "--docker", help="Stop Docker Compose deployment instead of a local process."),
) -> None:
    """Stop north."""
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

    if all:
        count = _stop_all_north_processes(port)
        _stop_server(port)
        # Whether anything is still serving is the only answer that matters, and
        # it is checked rather than assumed: "✓ Stopped all north processes (0
        # process(es) terminated)" was printed in green over a server that went
        # on running, and on to serve stale code through an update.
        if _port_in_use("127.0.0.1", port):
            typer.secho(
                f"Something is still listening on port {port} after stopping {count} process(es). "
                f"Find it with: lsof -nP -iTCP:{port} -sTCP:LISTEN",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(1)
        if count:
            typer.secho(f"✓ Stopped all north processes ({count} terminated).", fg=typer.colors.GREEN)
        else:
            typer.secho("north was not running.", fg=typer.colors.YELLOW)
        return

    from config.settings import settings

    pid_path = settings.north_home / "north.pid"
    if not pid_path.exists() and not _port_in_use("127.0.0.1", port):
        typer.echo("north is not running.")
        return

    _stop_server(port)
    typer.secho("✓ Stopped.", fg=typer.colors.GREEN)


def _move_entries(source: Path, names: tuple[str, ...], destination: Path) -> tuple[str, ...]:
    """Move each named entry from *source* into *destination*; return those moved.

    Moved rather than copied so restrictive file permissions - credentials are
    written 0600 - survive the round trip. Names that are not present are
    skipped, so a fresh install with no credentials is not an error.
    """
    destination.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    for name in names:
        entry = source / name
        if not entry.exists():
            continue
        shutil.move(str(entry), str(destination / name))
        moved.append(name)
    return tuple(moved)


@app.command("reset")
def reset(
    all: bool = typer.Option(False, "--all", help="Also remove your API keys and every provider login."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """Wipe north's data and start fresh.

    Stops the server and deletes all local state - ledger, context, tasks,
    logs, learned preferences, and the secret key. Your API keys in .env and
    the providers you have logged into (e.g. OpenAI Codex) are kept unless you
    pass --all.
    """
    from config.settings import settings
    from inference.codex_auth import CREDENTIALS_DIR_NAME

    north_home = settings.north_home

    # What a plain reset keeps: the things that identify you to a provider.
    # Everything else under north's home is data, which is what reset exists to
    # wipe. `credentials/` is kept whole, so a provider added later survives
    # without anyone having to remember to update this list.
    preserved = (".env", CREDENTIALS_DIR_NAME)

    # What gets wiped
    if all:
        scope = f"{north_home}/ (everything, including API keys and provider logins)"
    else:
        scope = f"{north_home}/ (data only - API keys and provider logins are kept)"

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

    # Selective wipe: keep what identifies you to a provider, delete the data.
    staging = Path(tempfile.mkdtemp(dir=north_home.parent, prefix=".north-reset-"))
    try:
        moved = _move_entries(north_home, preserved, staging)
        shutil.rmtree(north_home, ignore_errors=True)
        north_home.mkdir(parents=True, exist_ok=True)
        _move_entries(staging, moved, north_home)
    except OSError as exc:
        typer.secho(f"Reset failed: {exc}", fg=typer.colors.RED, err=True)
        typer.secho(f"Your credentials are safe in {staging} - move them back to {north_home}.", fg=typer.colors.YELLOW)
        raise typer.Exit(1) from exc
    shutil.rmtree(staging, ignore_errors=True)

    typer.secho(
        "✓ Data wiped. API keys and provider logins kept. Run north start to begin fresh.", fg=typer.colors.GREEN
    )


_NORTH_GIT_URL = "https://github.com/Kushagrabainsla/north.git"
_INSTALL_SCRIPT_URL = "https://raw.githubusercontent.com/Kushagrabainsla/north/main/scripts/install.sh"


@dataclass(frozen=True)
class _UpdateOptions:
    """How an update should run, as the command's flags asked for it."""

    port: int
    restart: bool = True
    assume_yes: bool = False


def _confirm_update(options: _UpdateOptions) -> None:
    if not options.assume_yes:
        typer.confirm("Proceed with update?", default=True, abort=True)


def _stop_server_if_running(port: int) -> bool:
    """Stop a running north server. True when one was actually running."""
    if not (_port_in_use("127.0.0.1", port) and _is_north_server("127.0.0.1", port)):
        return False
    _console.print("  [dim]→[/dim]  stopping server…")
    _stop_server(port)
    return True


def _install_helper_binaries() -> None:
    """Optional companions north can use but does not ship (currently chrome-agent)."""
    if shutil.which("chrome-agent"):
        return
    if shutil.which("cargo"):
        _console.print("  [dim]→[/dim]  installing chrome-agent via cargo…")
        subprocess.run(["cargo", "install", "chrome-agent", "-q"], capture_output=True)
    elif shutil.which("npm"):
        _console.print("  [dim]→[/dim]  installing chrome-agent via npm…")
        subprocess.run(["npm", "install", "-g", "chrome-agent", "-q"], capture_output=True)


def _update_docker_deployment(options: _UpdateOptions) -> None:
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
    _wait_for_server("127.0.0.1", options.port)
    _console.print()
    typer.secho("✓ north updated and restarted via Docker.", fg=typer.colors.GREEN)


def _update_local_checkout(project_root: Path, options: _UpdateOptions) -> None:
    _console.print(f"  [dim]source     [/dim]  {project_root} (local git repository)")
    _stop_server_if_running(options.port)
    _console.print()
    _confirm_update(options)

    _console.print("  [dim]→[/dim]  pulling latest changes from git…")
    _run_command(["git", "pull"], cwd=project_root)
    _console.print("  [dim]→[/dim]  reinstalling dependencies…")
    subprocess.run(["uv", "pip", "install", "-e", "."], cwd=project_root, check=False)
    subprocess.run(
        ["uv", "tool", "install", "--editable", "--force", str(project_root)], capture_output=True, check=False
    )
    _console.print()
    typer.secho("✓ north updated and synced.", fg=typer.colors.GREEN)
    if options.restart:
        _start_server_process(options.port)


def _update_from_git(install_url: str, options: _UpdateOptions) -> None:
    _console.print(f"  [dim]source     [/dim]  {install_url}")
    was_running = _stop_server_if_running(options.port)
    _console.print()
    _confirm_update(options)

    if not shutil.which("uv"):
        typer.secho(
            f"ERROR: uv not found. Re-run the install script:\n  curl -fsSL {_INSTALL_SCRIPT_URL} | bash",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1) from None

    _console.print("  [dim]→[/dim]  reinstalling from git…")
    result = subprocess.run(
        ["uv", "tool", "install", _pinned_git_spec(install_url), "--force", "--no-cache", "-q"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        typer.secho(
            f"Install failed:\n{(result.stdout + result.stderr).strip()}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1) from None
    _console.print("  [dim green]✓[/dim green]  updated python dependencies and cli")

    _install_helper_binaries()
    _console.print()
    if options.restart and (was_running or typer.confirm("Start north now?", default=True)):
        _console.print("  [dim]→[/dim]  restarting…")
        proc = _start_server_process(options.port)
        _wait_for_server("127.0.0.1", options.port, proc=proc)
        typer.secho(f"✓ north updated and restarted (pid {proc.pid}).", fg=typer.colors.GREEN)
    else:
        typer.secho("✓ north updated. Run north start to restart.", fg=typer.colors.GREEN)


@app.command("update")
def update(
    port: int = typer.Option(8000, "--port", "-p", help="Port the server is running on."),
    docker: bool = typer.Option(False, "--docker", help="Update a Docker Compose deployment."),
    restart: bool = typer.Option(True, "--restart/--no-restart", help="Restart the server after updating."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """Update north to the latest version.

    Mirrors the install script: pulls the latest code from GitHub and
    reinstalls. Pass --docker to update a Docker Compose deployment instead.
    """
    _console.print()
    _console.print("  [bold white]north update[/bold white]")
    _console.print(f"  [bright_black]{'─' * 44}[/bright_black]")

    options = _UpdateOptions(port=port, restart=restart, assume_yes=yes)
    if docker:
        _update_docker_deployment(options)
        return

    install_url, is_git_url = _get_install_url()
    project_root = _find_project_root()
    if not is_git_url and project_root and (project_root / ".git").exists():
        _update_local_checkout(project_root, options)
        return

    _update_from_git(install_url if is_git_url and install_url else _NORTH_GIT_URL, options)


def _listens_on(proc, port: int) -> bool:
    """Whether *proc* holds a listening socket on *port*.

    Asked per process rather than through ``psutil.net_connections()``, which
    needs root on macOS and fails with AccessDenied for everyone else - silently,
    if the caller suppresses it. A process owned by the current user answers for
    itself without privileges.
    """
    import psutil

    try:
        return any(
            conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port
            for conn in proc.net_connections(kind="inet")
        )
    except (psutil.Error, OSError):
        return False


def _north_processes(port: int = 8000) -> list:
    """Every process that is part of a running north, however it was spawned.

    Three ways in, because one is not enough. The command line finds a server
    started as `north start` or `uvicorn orchestrator.app:app`. The pid file
    finds one whose command line has been rewritten. And whoever is *listening
    on the port* finds the one that matters most: north's server runs as a
    multiprocessing spawn child, whose argv is only

        python -c from multiprocessing.spawn import spawn_main; ...

    and so matches none of the command-line keywords. That process held port
    8000 through two `north stop --all` runs and an update, each of which
    reported success, while the update's new server died on "address already in
    use" and the old code kept serving.

    Children are included: killing a supervisor that has already forked leaves
    the fork holding the port.
    """
    import psutil

    from config.settings import settings

    current_pid = os.getpid()
    found: dict[int, object] = {}

    def remember(proc) -> None:
        try:
            if proc.pid != current_pid and proc.is_running():
                found[proc.pid] = proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    me = psutil.Process().username()
    for proc in psutil.process_iter(["pid", "name", "cmdline", "username"]):
        try:
            if proc.pid == current_pid:
                continue
            cmdline = " ".join(proc.info.get("cmdline") or []).lower()
            # The keywords only count for a process that could actually *be* a
            # north server. Matched against any command line, they also match a
            # shell, an editor or a grep that merely mentions one - and this
            # function's whole purpose is to send SIGKILL to what it matches.
            name = (proc.info.get("name") or "").lower()
            looks_like_north = name.startswith("python") or name in {"north", "uvicorn"}
            if (
                looks_like_north
                and any(k in cmdline for k in ("north start", "orchestrator.app:app", "bin/north"))
                or proc.info.get("username") == me
                and _listens_on(proc, port)
            ):
                remember(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    pid_path = settings.north_home / "north.pid"
    if pid_path.exists():
        with contextlib.suppress(ValueError, OSError, psutil.Error):
            remember(psutil.Process(int(pid_path.read_text(encoding="utf-8").strip())))

    for proc in list(found.values()):
        with contextlib.suppress(psutil.Error):
            for child in proc.children(recursive=True):
                remember(child)
    return list(found.values())


def _stop_all_north_processes(port: int = 8000) -> int:
    """Terminate every running north process. Returns how many actually stopped."""
    import signal

    import psutil

    from config.settings import settings

    stopped = 0
    procs_to_kill = _north_processes(port)

    for proc in procs_to_kill:
        try:
            proc.send_signal(signal.SIGTERM)
            stopped += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    if procs_to_kill:
        time.sleep(1)
        for proc in procs_to_kill:
            try:
                if proc.is_running():
                    proc.send_signal(signal.SIGKILL)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    # The pid file is the last handle on a process whose command line matches
    # nothing. Deleting it after a stop that stopped nothing threw that handle
    # away and left the orphan unreachable.
    if not _port_in_use("127.0.0.1", port):
        (settings.north_home / "north.pid").unlink(missing_ok=True)
    return stopped


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


def _run_command(cmd: list[str], *, cwd: Path) -> bool:
    """Run a subprocess, printing its output only on failure. Returns True on success."""
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0 and (result.stdout or result.stderr):
        output = (result.stdout + result.stderr).strip()
        _console.print(f"  [dim red]{output}[/dim red]")
    return result.returncode == 0


if __name__ == "__main__":
    app()
