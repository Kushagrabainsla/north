"""The three setups. Each gets one task's prompt in a prepared copy and reports what it did.

claude  plain Claude Code, headless. Only the project's own settings: the user's global rules are theirs, not Claude's.
codex   plain Codex, headless, in its usual unattended sandbox.
north   a real north server with its real model: north reads the prompt and decides what to do. It has its own
        empty home, so nothing it remembers about the user or an earlier task helps it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from catalog import Task
from checkouts import Checkout

_REPO = Path(__file__).parents[2]
sys.path.insert(0, str(_REPO / "experiments" / "coding_paths_comparison"))

from arms import ArmResult, raw_claude, raw_codex  # noqa: E402

_REAL_HOME = Path.home() / ".north"
_LOGIN = Path("credentials") / "openai_codex.json"
_DONE = {"completed", "failed", "cancelled", "dead", "timed_out", "error"}
_BOOT_TIMEOUT_S = 180
_POLL_S = 3


@dataclass
class _Prompted:
    """What the raw arms read from a task: the prompt with this copy's paths, and the time limit."""

    prompt: str
    timeout_s: int


def run_claude(task: Task, checkout: Checkout, log: Path) -> ArmResult:
    prompted = _Prompted(checkout.prompt, task.timeout_s)
    return raw_claude(prompted, checkout, log, extra=("--setting-sources", "project,local"))  # type: ignore[arg-type]


def run_codex(task: Task, checkout: Checkout, log: Path) -> ArmResult:
    return raw_codex(_Prompted(checkout.prompt, task.timeout_s), checkout, log)  # type: ignore[arg-type]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def provider_keys(env_text: str) -> str:
    """Only the lines that let north reach a model. A copied Telegram token would fight the user's own north."""
    return "".join(f"{line}\n" for line in env_text.splitlines() if re.match(r"NORTH_\w+_API_KEY=", line.strip()))


def make_home(home: Path, real_home: Path = _REAL_HOME) -> None:
    """A north home with what picks and reaches its model, and nothing else: no memory, flows or chats."""
    (home / "credentials").mkdir(parents=True)
    shutil.copy2(real_home / _LOGIN, home / _LOGIN)
    shutil.copy2(real_home / "settings.json", home / "settings.json")
    if (real_home / ".env").exists():
        (home / ".env").write_text(provider_keys((real_home / ".env").read_text()))
        (home / ".env").chmod(0o600)


def return_login(home: Path, real_home: Path = _REAL_HOME) -> bool:
    """If this run renewed the login, hand the new one back: the old refresh token may no longer work."""
    used, real = home / _LOGIN, real_home / _LOGIN
    if not used.exists() or used.read_bytes() == real.read_bytes():
        return False
    if used.stat().st_mtime <= real.stat().st_mtime:
        return False
    os.replace(shutil.copy2(used, real.with_suffix(".bench-new")), real)
    return True


def _usage(client: httpx.Client, task_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Every agent run in this task and its recorded events, from north's own API."""
    runs = client.get(f"/orchestrator/tasks/{task_id}/runs").raise_for_status().json()
    events: list[dict[str, Any]] = []
    for run in runs:
        events += client.get(f"/orchestrator/runs/{run['run_id']}/events").raise_for_status().json()
    return runs, events


def coding_agents(runs: list[dict[str, Any]]) -> list[str]:
    """Which coding agents north really called, in order: the point of this experiment."""
    return [r["agent"].removeprefix("coding:") for r in runs if str(r.get("agent", "")).startswith("coding:")]


def _sum(rows: list[dict[str, Any]], key: str) -> float:
    return sum(float(row.get(key) or 0) for row in rows)


def _server_env(home: Path, workspace: Path, port: int) -> dict[str, str]:
    return {
        **os.environ,
        "NORTH_HOME": str(home),
        "NORTH_NORTH_WORKSPACE": str(workspace),
        "NORTH_NORTH_ORCHESTRATOR_URL": f"http://127.0.0.1:{port}",
        "NORTH_LOG_TO_HOME": "1",
    }


def _wait_up(client: httpx.Client, process: subprocess.Popen, deadline: float) -> None:
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"north exited at start with code {process.returncode}")
        try:
            if client.get("/orchestrator/tasks").status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(1)
    raise TimeoutError("north did not start")


def _secret(home: Path, deadline: float) -> str:
    while time.monotonic() < deadline:
        file = home / "secret.key"
        if file.exists() and file.read_text().strip():
            return file.read_text().strip()
        time.sleep(0.5)
    raise TimeoutError("north wrote no secret")


def run_north(task: Task, checkout: Checkout, log: Path) -> ArmResult:
    home = Path(tempfile.mkdtemp(prefix="north-bench-home-"))
    shutil.rmtree(home)
    make_home(home)
    port = _free_port()
    started = time.monotonic()
    result = ArmResult("north")
    with (home.parent / f"{home.name}.log").open("w") as server_log:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "orchestrator.app:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=_REPO,
            env=_server_env(home, checkout.path, port),
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + _BOOT_TIMEOUT_S
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=60) as client:
            client.headers["X-North-Secret"] = _secret(home, deadline)
            _wait_up(client, process, deadline)
            _drive(client, task, checkout, log, result, started)
    except (RuntimeError, TimeoutError, httpx.HTTPError) as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        # A server that died before this point left no reason in its output: its exit code
        # (negative: the signal) and its own log are all there is, so keep both.
        if process.poll() is not None:
            result.extra["server_exit_code"] = process.returncode
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
        result.extra["login_returned"] = return_login(home)
        shutil.copy2(home.parent / f"{home.name}.log", log.with_suffix(".server.log"))
        if (home / "north.log").exists():
            shutil.copy2(home / "north.log", log.with_suffix(".north.log"))
        shutil.rmtree(home, ignore_errors=True)
        (home.parent / f"{home.name}.log").unlink(missing_ok=True)
    result.seconds = round(time.monotonic() - started, 1)
    return result


def _drive(client: httpx.Client, task: Task, checkout: Checkout, log: Path, result: ArmResult, started: float) -> None:
    client.post("/orchestrator/settings", json={"autonomy": "yolo"}).raise_for_status()
    body = {"title": f"bench {task.number}", "source": "cli", "workspace": str(checkout.path)}
    conversation = client.post("/web/api/conversations", json=body).raise_for_status().json()["id"]
    turn = client.post(f"/web/api/conversations/{conversation}/turns", json={"prompt": checkout.prompt})
    if turn.status_code != 200 and turn.status_code != 202:
        result.error = f"north refused the prompt: {turn.status_code} {turn.text[:300]}"
        return
    task_id = turn.json()["task_id"]
    status = "pending"
    while time.monotonic() - started < task.timeout_s:
        status = client.get(f"/orchestrator/task/{task_id}").raise_for_status().json()["status"].lower()
        if status in _DONE:
            break
        time.sleep(_POLL_S)
    else:
        client.delete(f"/orchestrator/task/{task_id}")
        result.error = f"timed out after {task.timeout_s}s"
    ledger = client.get("/orchestrator/ledger", params={"task_id": task_id, "limit": 500}).raise_for_status().json()
    runs, events = _usage(client, task_id)
    log.write_text(json.dumps({"status": status, "ledger": ledger, "runs": runs, "events": events}, default=str))
    if status != "completed" and not result.error:
        result.error = f"north ended as {status}"
    result.answer = next(
        (str(e["output"]) for e in reversed(ledger) if e.get("action") == "agent_completed" and e.get("output")), ""
    ) or next((str(e["output"]) for e in ledger if e.get("action") == "agent_completed" and e.get("output")), "")
    # The runs already total north's own model calls; the ledger lists those same calls again.
    result.tokens_in = int(_sum(runs, "tokens_in")) or None
    result.tokens_out = int(_sum(runs, "tokens_out")) or None
    result.cost_usd = _sum(runs, "cost_usd") or None
    result.extra.update(
        {
            "agents": [
                {k: r.get(k) for k in ("agent", "status", "models_used", "tokens_in", "cost_usd")} for r in runs
            ],
            "coding_agents": coding_agents(runs),
            "status": status,
        }
    )


RUNNERS = {"claude": run_claude, "codex": run_codex, "north": run_north}
