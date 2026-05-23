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
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
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

task_app = typer.Typer(help="Task management.", no_args_is_help=True)
app.add_typer(task_app, name="task")


@task_app.callback(invoke_without_command=True)
def task_default(
    ctx: typer.Context,
    prompt: Optional[str] = typer.Argument(None, help="Prompt to submit as a new task."),
) -> None:
    """Submit a task, or use a subcommand (cancel)."""
    if ctx.invoked_subcommand is not None:
        return
    if prompt is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()
    response = _api("POST", "/orchestrator/task", json={"prompt": prompt})
    data = response.json()
    task_id = data["task_id"]
    typer.secho(f"✓ Task submitted: {task_id}", fg=typer.colors.GREEN)
    typer.echo(f"  Status : {data['status']}")
    typer.echo(f"  Created: {data['created_at']}")
    typer.echo(f"\nStream with:  north stream {task_id}")


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


# ── stream ────────────────────────────────────────────────────────────────────

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
                        typer.echo(json.dumps(
                            {k: v for k, v in data.items() if k != "event"}, indent=2
                        ))
                    except json.JSONDecodeError:
                        typer.echo(payload)
    except KeyboardInterrupt:
        typer.echo("\nStream closed.")


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

    (agent_dir / "agent.py").write_text(
        f'"""{class_name} domain specialist.\n\nSee docs/CODING_STYLE.md Section 15.\n"""\n\n'
        f"from __future__ import annotations\n\n"
        f"from agents.llm_agent import LLMAgent\n\n\n"
        f"class {class_name}(LLMAgent):\n"
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
) -> None:
    """Start north.

    By default uses Docker Compose when a docker-compose.yml is found and
    Docker is available. Pass --local to force direct uvicorn launch.
    """
    compose_file = _find_compose_file()
    use_docker = not local and _docker_available() and compose_file is not None

    if use_docker:
        typer.secho("★ north", fg=typer.colors.BRIGHT_WHITE, bold=True, nl=False)
        typer.echo(f"  Mode         → Docker Compose")
        typer.echo(f"  Compose file → {compose_file}")
        typer.echo(f"  Web UI       → http://127.0.0.1:{port}/ui/")
        typer.echo("")
        subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "up", "--build"],
            check=False,
        )
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
    uvicorn.run(
        "orchestrator.app:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


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
