"""Replay issue #83 ("why is browser automation so slow, and why so many tokens?") with timing on.

Starts a north server from this checkout in a scratch home, gives it browser tasks on
public sites, and keeps the run's events (tasks.db) for `timeline.py`. The scratch home
has only what picks a model and reaches it, the settings (approval mode, routing,
browser profiles) and a link to one existing profile's bridge folder, so the server
drives the user's own running TaskWindow bridge: the path every real slow run took.

Cards are answered here, never by a person: approved only for a browser card on an
allowed site, rejected otherwise. Nothing is sent or submitted on any site.

    .venv/bin/python experiments/browser_speed/run.py PROFILE_ID [TASK ...]
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
from pathlib import Path

import httpx

_REPO = Path(__file__).parents[2]
_REAL_HOME = Path(os.environ.get("NORTH_HOME") or Path.home() / ".north")
_LOGIN = Path("credentials") / "openai_codex.json"
_RESULTS = Path.home() / ".cache" / "north" / "browser-speed"
_DONE = {"completed", "failed", "cancelled", "dead", "timed_out", "error"}
_TIMEOUT_S = 900

# Read-only work on public pages, with the steps real browser tasks take: open, read,
# inspect, click, fill and press. North closes each task's tab when the task ends, after a screenshot.
TASKS = {
    "hn": (
        "Using my {profile} browser profile, open https://news.ycombinator.com, open the comments page "
        "of the top story, and tell me the story title and one line on each of the top 3 comments."
    ),
    "wiki": (
        "Using my {profile} browser profile, go to https://en.wikipedia.org, type 'Ada Lovelace' in the "
        "search box and search, open her article, and tell me her birth date and one notable work."
    ),
    # A chain of dependent pages, each reached by a search or a click: the case a step list is for.
    "chain": (
        "Using my {profile} browser profile, go to https://en.wikipedia.org, search for 'Ada Lovelace' with the "
        "search box, open her article, then follow its link to the article on Charles Babbage, and from there "
        "the link to the Analytical Engine article. Tell me one fact from each of the three articles."
    ),
    # Several independent pages: the case a batch read is for.
    "three": (
        "Using my {profile} browser profile, read the Wikipedia articles on Ada Lovelace, Alan Turing and "
        "Grace Hopper (https://en.wikipedia.org/wiki/Ada_Lovelace, https://en.wikipedia.org/wiki/Alan_Turing, "
        "https://en.wikipedia.org/wiki/Grace_Hopper) and tell me each one's birth year and best-known work."
    ),
}
_ALLOWED_SITES = ("news.ycombinator.com", "ycombinator.com", "wikipedia.org")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _make_home(home: Path, profile_id: str) -> None:
    """A north home that reaches the user's model and one browser bridge, and nothing else."""
    (home / "credentials").mkdir(parents=True)
    if (_REAL_HOME / _LOGIN).exists():
        shutil.copy2(_REAL_HOME / _LOGIN, home / _LOGIN)
    shutil.copy2(_REAL_HOME / "settings.json", home / "settings.json")
    keys = [
        line for line in (_REAL_HOME / ".env").read_text().splitlines() if re.match(r"NORTH_\w+_API_KEY=", line.strip())
    ]
    (home / ".env").write_text("".join(f"{line}\n" for line in keys))
    (home / ".env").chmod(0o600)
    # The bridge proves the profile by the extension folder Chrome loaded; a link keeps that path.
    bridge = home / "browser" / "taskwindow"
    bridge.mkdir(parents=True)
    (bridge / profile_id).symlink_to(_REAL_HOME / "browser" / "taskwindow" / profile_id)


def _return_login(home: Path) -> None:
    """A login this run renewed goes back: the old refresh token may no longer work."""
    used, real = home / _LOGIN, _REAL_HOME / _LOGIN
    renewed = used.exists() and real.exists() and used.read_bytes() != real.read_bytes()
    if renewed and used.stat().st_mtime > real.stat().st_mtime:
        os.replace(shutil.copy2(used, real.with_suffix(".speed-new")), real)


def _answer_cards(client: httpx.Client, task_id: str, answered: set[str], log: list[dict]) -> None:
    for card in client.get("/web/api/approvals", params={"limit": 50}).json():
        if card.get("task_id") != task_id or card.get("status") != "pending" or card["id"] in answered:
            continue
        text = f"{card.get('title', '')} {card.get('message', '')}"
        allowed = card.get("agent") == "browser" and any(site in text for site in _ALLOWED_SITES)
        decision = "approved" if allowed else "rejected"
        client.post(
            "/orchestrator/approval/respond",
            json={"card_id": card["id"], "decision": decision, "reason": "" if allowed else "not in the test"},
        )
        answered.add(card["id"])
        log.append({"at": time.time(), "card": card["id"], "decision": decision, "title": card.get("title")})


def _run_task(client: httpx.Client, name: str, prompt: str) -> dict:
    body = {"title": f"browser speed {name}", "source": "cli"}
    conversation = client.post("/web/api/conversations", json=body).raise_for_status().json()["id"]
    turn = client.post(f"/web/api/conversations/{conversation}/turns", json={"prompt": prompt})
    turn.raise_for_status()
    task_id = turn.json()["task_id"]
    started, status, answered, cards = time.monotonic(), "pending", set(), []
    while time.monotonic() - started < _TIMEOUT_S:
        _answer_cards(client, task_id, answered, cards)
        status = client.get(f"/orchestrator/task/{task_id}").json()["status"].lower()
        if status in _DONE:
            break
        time.sleep(1)
    else:
        client.delete(f"/orchestrator/task/{task_id}")
        status = "timed_out"
    ledger = client.get("/orchestrator/ledger", params={"task_id": task_id, "limit": 500}).json()
    answer = next((e.get("output") for e in reversed(ledger) if e.get("action") == "agent_completed"), "")
    return {"task": name, "task_id": task_id, "status": status, "seconds": time.monotonic() - started,
            "cards": cards, "answer": answer}  # fmt: skip


def main(argv: list[str]) -> None:
    run_tasks(argv[0], argv[1:] or list(TASKS))


def run_tasks(profile_id: str, names: list[str], results: Path = _RESULTS) -> tuple[list[dict], Path]:
    """Run the named tasks in a scratch north with *profile_id*; their results, and the folder that keeps them.

    Also what the nightly check's live browser test runs (tests/live/test_browser_live.py).
    """
    settings = json.loads((_REAL_HOME / "settings.json").read_text())
    profile = next(p for p in settings["browser_profiles"] if p["id"] == profile_id)
    out = results / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True)
    home = Path(tempfile.mkdtemp(prefix="north-speed-home-")) / "home"
    _make_home(home, profile_id)
    port = _free_port()
    env = {
        **os.environ,
        "NORTH_HOME": str(home),
        "NORTH_LOG_TO_HOME": "1",
        "NORTH_NORTH_ORCHESTRATOR_URL": f"http://127.0.0.1:{port}",
        "NORTH_NORTH_WORKSPACE": str(home.parent),
    }
    command = [sys.executable, "-m", "uvicorn", "orchestrator.app:app", "--host", "127.0.0.1", "--port", str(port)]
    with (out / "server.log").open("w") as server_log:
        server = subprocess.Popen(command, cwd=_REPO, env=env, stdout=server_log, stderr=subprocess.STDOUT)
    done: list[dict] = []
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=60) as client:
            deadline = time.monotonic() + 180
            while not (home / "secret.key").exists() and time.monotonic() < deadline:
                time.sleep(0.5)
            client.headers["X-North-Secret"] = (home / "secret.key").read_text().strip()
            while time.monotonic() < deadline:
                try:
                    if client.get("/orchestrator/tasks").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(1)
            for name in names:
                result = _run_task(client, name, TASKS[name].format(profile=profile["name"]))
                done.append(result)
                print(json.dumps({k: v for k, v in result.items() if k != "answer"}), flush=True)
    finally:
        server.terminate()
        try:
            server.wait(timeout=30)
        except subprocess.TimeoutExpired:
            server.kill()
        _return_login(home)
        # The database with its write-ahead log: rows not yet checkpointed live only there.
        (out / "tasks").mkdir()
        for part in home.joinpath("tasks").glob("tasks.db*"):
            shutil.copy2(part, out / "tasks" / part.name)
        if (home / "north.log").exists():
            shutil.copy2(home / "north.log", out / "north.log")
        # North's screenshot of each task's last page (BrowserTool.end_task).
        for shot in home.joinpath("tasks").glob("*/browser-*.png"):
            shutil.copy2(shot, out / f"{shot.parent.name}-{shot.name}")
        (out / "results.json").write_text(json.dumps(done, indent=2, default=str))
        (home / "browser" / "taskwindow" / profile_id).unlink()  # the link, never the user's folder
        shutil.rmtree(home.parent, ignore_errors=True)
    print(f"results in {out}")
    return done, out


if __name__ == "__main__":
    main(sys.argv[1:])
