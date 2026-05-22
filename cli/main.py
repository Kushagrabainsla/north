"""Typer CLI for north.

Commands talk exclusively to the Orchestrator API on port 8000.

Usage:
    north task "Help me plan my week"
    north context show north_stars
    north context edit judgement_rules
    north ledger
    north jobs
    north agents

See docs/CODING_STYLE.md Section 8 and README Section 12.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Optional

import httpx
import typer

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

@app.command("task")
def submit_task(
    prompt: str = typer.Argument(..., help="The task prompt to send to north."),
) -> None:
    """Submit a task to north for processing."""
    response = _api("POST", "/orchestrator/task", json={"prompt": prompt})
    data = response.json()
    task_id = data["task_id"]
    typer.secho(f"✓ Task submitted: {task_id}", fg=typer.colors.GREEN)
    typer.echo(f"  Status : {data['status']}")
    typer.echo(f"  Created: {data['created_at']}")
    typer.echo(f"\nStream events with:")
    typer.secho(f"  north stream {task_id}", fg=typer.colors.CYAN)


@app.command("stream")
def stream_task(
    task_id: str = typer.Argument(..., help="Task ID to stream events for."),
) -> None:
    """Stream real-time events for a task via SSE."""
    url = f"{_BASE_URL}/orchestrator/stream/{task_id}"
    typer.echo(f"Streaming events for {task_id} (Ctrl+C to stop)…\n")
    try:
        with httpx.stream("GET", url, headers=_headers(), timeout=None) as response:
            for line in response.iter_lines():
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    try:
                        data = json.loads(payload)
                        event = data.get("event", "event")
                        typer.secho(f"[{event}] ", fg=typer.colors.CYAN, nl=False)
                        typer.echo(json.dumps({k: v for k, v in data.items() if k != "event"}, indent=2))
                    except json.JSONDecodeError:
                        typer.echo(payload)
    except KeyboardInterrupt:
        typer.echo("\nStream closed.")


# ── tasks ─────────────────────────────────────────────────────────────────────

@app.command("tasks")
def list_tasks() -> None:
    """List all currently pending tasks."""
    response = _api("GET", "/orchestrator/tasks")
    tasks = response.json()
    if not tasks:
        typer.echo("No pending tasks.")
        return
    for task in tasks:
        typer.secho(f"  {task['task_id']}", fg=typer.colors.CYAN, nl=False)
        typer.echo(f"  {task['status']}  {task['created_at']}")


# ── context ──────────────────────────────────────────────────────────────────

context_app = typer.Typer(help="Manage context documents.", no_args_is_help=True)
app.add_typer(context_app, name="context")

_VALID_DOCS = ["public", "private", "privacy_rules", "judgement_rules", "north_stars"]


@context_app.command("show")
def context_show(
    document: str = typer.Argument(..., help=f"Document name: {', '.join(_VALID_DOCS)}"),
) -> None:
    """Print the contents of a context document."""
    import asyncio
    from config.dependencies import build_production_dependencies
    from context.models import ContextDocument

    doc_name = f"{document}.md" if not document.endswith(".md") else document
    try:
        doc = ContextDocument(doc_name)
    except ValueError:
        typer.secho(f"Unknown document '{document}'. Valid: {', '.join(_VALID_DOCS)}", err=True, fg=typer.colors.RED)
        raise typer.Exit(1)

    deps = build_production_dependencies()

    async def _read() -> str:
        return await deps.context_store.read(doc)

    content = asyncio.run(_read())
    typer.echo(content or "(empty)")


@context_app.command("edit")
def context_edit(
    document: str = typer.Argument(..., help=f"Document name: {', '.join(_VALID_DOCS)}"),
) -> None:
    """Open a context document in $EDITOR."""
    import os
    from config.settings import settings

    doc_name = f"{document}.md" if not document.endswith(".md") else document
    doc_path = settings.north_home / "context" / doc_name
    doc_path.parent.mkdir(parents=True, exist_ok=True)
    if not doc_path.exists():
        doc_path.touch()

    editor = os.environ.get("EDITOR", "nano")
    subprocess.call([editor, str(doc_path)])


# ── ledger ───────────────────────────────────────────────────────────────────

@app.command("ledger")
def show_ledger(
    limit: int = typer.Option(20, "--limit", "-n", help="Number of entries to show."),
    task_id: Optional[str] = typer.Option(None, "--task", help="Filter by task ID."),
) -> None:
    """Show recent ledger entries."""
    import asyncio
    from config.dependencies import build_production_dependencies
    from ledger import LedgerFilters, LedgerSource

    deps = build_production_dependencies()
    filters = LedgerFilters(task_id=task_id, limit=limit)

    async def _query() -> list:
        return await deps.ledger.query(filters)

    entries = asyncio.run(_query())
    if not entries:
        typer.echo("No ledger entries found.")
        return

    for entry in entries:
        ts = entry.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        agent_str = f"  agent={entry.agent}" if entry.agent else ""
        typer.secho(f"[{ts}] ", fg=typer.colors.BRIGHT_BLACK, nl=False)
        typer.secho(f"{entry.source.value:<18}", fg=typer.colors.CYAN, nl=False)
        typer.secho(f" {entry.action or '':<40}", nl=False)
        if entry.status:
            colour = typer.colors.GREEN if entry.status.value == "completed" else typer.colors.RED
            typer.secho(f" {entry.status.value}", fg=colour, nl=False)
        typer.echo(agent_str)


# ── jobs ──────────────────────────────────────────────────────────────────────

@app.command("jobs")
def show_jobs(
    limit: int = typer.Option(20, "--limit", "-n", help="Number of jobs to show."),
) -> None:
    """Show recent scheduled jobs."""
    import asyncio
    from config.dependencies import build_production_dependencies

    deps = build_production_dependencies()

    async def _list() -> list:
        return await deps.job_processor.list_jobs(limit=limit)

    jobs = asyncio.run(_list())
    if not jobs:
        typer.echo("No jobs found.")
        return

    for job in jobs:
        typer.secho(f"  {job.id:<36}", fg=typer.colors.CYAN, nl=False)
        typer.echo(f"  {job.status.value:<12}  {job.type.value}")


# ── agents ────────────────────────────────────────────────────────────────────

@app.command("agents")
def list_agents() -> None:
    """List all registered domain-specialist agents."""
    from pathlib import Path
    import yaml

    agents_dir = Path(__file__).parent.parent / "agents"
    found = False
    for entry in sorted(agents_dir.iterdir()):
        config_path = entry / "config.yaml"
        if not (entry.is_dir() and config_path.exists()):
            continue
        with config_path.open() as f:
            cfg = yaml.safe_load(f)
        typer.secho(f"  {cfg.get('agent', entry.name):<16}", fg=typer.colors.CYAN, nl=False)
        typer.echo(f"  domain={cfg.get('domain', '?')}  pool={cfg.get('model_pool', '?')}")
        found = True

    if not found:
        typer.echo("No agents found in agents/")


if __name__ == "__main__":
    app()
