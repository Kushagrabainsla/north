"""Measure one run of the Kasa rebuild: did north make the tool and the panel from one prompt, and at what cost?

Usage: python measure.py <label> [<task_id>]
Without a task id, the newest task whose prompt starts "Add my Kasa lights" is measured.

Reads north's home read-only (tasks.db, ledger.db, approvals.db, the learned tools and the views) and writes
results/<label>.json with counts, names and booleans only: no prompt text, no secret, no device data.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

HOME = Path.home() / ".north"
HERE = Path(__file__).parent
PROMPT_START = "Add my Kasa lights"
META = {"create_tool", "create_view", "create_skill", "create_flow"}
PANEL_PARTS = {"Switch", "Slider", "ColorPicker", "Microphone"}


def _ro(name: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{HOME / name}?mode=ro", uri=True)


def _task(task_id: str | None) -> str:
    if task_id:
        return task_id
    with _ro("tasks/tasks.db") as conn:
        row = conn.execute(
            "SELECT task_id FROM agent_runs WHERE prompt LIKE ? ORDER BY started_at DESC LIMIT 1", (f"{PROMPT_START}%",)
        ).fetchone()
    if row is None:
        raise SystemExit(f"No task whose prompt starts {PROMPT_START!r}.")
    return row[0]


def _runs(task_id: str) -> dict:
    with _ro("tasks/tasks.db") as conn:
        runs = conn.execute(
            "SELECT run_id, status, started_at, completed_at, tokens_in, tokens_out, cost_usd, models_used "
            "FROM agent_runs WHERE task_id = ? ORDER BY started_at",
            (task_id,),
        ).fetchall()
        events = conn.execute(
            "SELECT event, data FROM agent_run_events WHERE task_id = ? AND event IN "
            "('tool_called', 'tool_result', 'prompt_profile') ORDER BY id",
            (task_id,),
        ).fetchall()
    calls: Counter[str] = Counter()
    failed: Counter[str] = Counter()
    turns = input_tokens = cached = 0
    tool_names_called: list[str] = []
    for event, raw in events:
        data = json.loads(raw)
        if event == "tool_called":
            calls[str(data.get("tool"))] += 1
            tool_names_called.append(str(data.get("tool")))
        elif event == "tool_result" and data.get("success") is False:
            failed[str(data.get("tool"))] += 1
        elif event == "prompt_profile":
            turns += 1
            input_tokens += data.get("actual_input_tokens") or 0
            cached += data.get("cached_tokens") or 0
    starts = [r[2] for r in runs if r[2]]
    ends = [r[3] for r in runs if r[3]]
    minutes = None
    if starts and ends:
        elapsed = datetime.fromisoformat(max(ends)) - datetime.fromisoformat(min(starts))
        minutes = round(elapsed.total_seconds() / 60, 1)
    return {
        "runs": len(runs),
        "run_statuses": dict(Counter(r[1] for r in runs)),
        "models": sorted({m for r in runs for m in json.loads(r[7] or "[]")}),
        "minutes": minutes,
        "model_turns": turns,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "output_tokens": sum(r[5] or 0 for r in runs),
        "cost_usd": round(sum(r[6] or 0 for r in runs), 4),
        "tool_calls": dict(calls),
        "failed_tool_calls": dict(failed),
        "meta_calls": sum(n for tool, n in calls.items() if tool in META),
        "failed_meta_calls": sum(n for tool, n in failed.items() if tool in META),
        "asked_in_chat_or_ask_user": calls.get("ask_user", 0),
        "request_secret_calls": calls.get("request_secret", 0),
    }


def _cards(task_id: str) -> dict:
    with _ro("approvals.db") as conn:
        rows = conn.execute("SELECT status, payload FROM approval_cards").fetchall()
    mine = [(status, json.loads(payload)) for status, payload in rows if json.loads(payload).get("task_id") == task_id]
    return {
        "cards": len(mine),
        "cards_by_title": dict(Counter(payload.get("title", "")[:60] for _, payload in mine)),
        "cards_by_status": dict(Counter(status for status, _ in mine)),
        "secret_cards": sum(1 for _, p in mine if any(f.get("type") == "secret" for f in p.get("fields") or [])),
    }


def _stages() -> dict:
    tools_dir = HOME / "learned" / "tools"
    sidecars = [p for p in tools_dir.rglob("*.json") if "candidates" not in p.parts]
    kasa = [p for p in sidecars if "kasa" in p.stem or "kasa" in p.read_text(encoding="utf-8").lower()]
    tool: dict = {"made": bool(kasa)}
    if kasa:
        saved = json.loads(kasa[0].read_text(encoding="utf-8"))
        meta = saved.get("meta", {})
        tool |= {
            "name": meta.get("name"),
            "packages": saved.get("packages"),
            "pinned_python_kasa": any(p.startswith("python-kasa==") for p in saved.get("packages") or []),
            "read_only_when": meta.get("read_only_when"),
            "uses_secrets": meta.get("uses_secrets"),
            "own_environment": any((HOME / "learned" / "envs").glob(f"{meta.get('name')}-*")),
        }
    views = list((HOME / "views").glob("*.json"))
    panel: dict = {"added": False}
    for path in views:
        view = json.loads(path.read_text(encoding="utf-8"))
        names = {source.get("tool") for source in view.get("data", [])} | {
            action.get("tool") for action in view.get("actions", {}).values()
        }
        if tool.get("name") in names:
            parts = {component.get("component") for component in view.get("components", [])}
            panel = {
                "added": True,
                "id": view.get("id"),
                "parts_present": sorted(parts & PANEL_PARTS),
                "has_all_parts": parts >= PANEL_PARTS,
                "tool_actions": sum(1 for a in view.get("actions", {}).values() if "tool" in a),
                "refresh": view.get("refresh", 0),
            }
    drafts = sorted(p.name for p in (HOME / "views" / "drafts").glob("*.json"))
    return {"tool": tool, "panel": panel, "panel_drafts_left": drafts}


def _list_worked(task_id: str, tool_name: str | None) -> bool:
    if not tool_name:
        return False
    with _ro("tasks/tasks.db") as conn:
        rows = conn.execute(
            "SELECT data FROM agent_run_events WHERE task_id = ? AND event = 'tool_result'", (task_id,)
        ).fetchall()
    return any((d := json.loads(raw)).get("tool") == tool_name and d.get("success") is True for (raw,) in rows)


def main() -> None:
    label = sys.argv[1]
    task_id = _task(sys.argv[2] if len(sys.argv) > 2 else None)
    stages = _stages()
    result = {
        "label": label,
        "task_id": task_id,
        "stages": stages,
        "tool_call_succeeded_in_task": _list_worked(task_id, stages["tool"].get("name")),
        "task": _runs(task_id),
        "approvals": _cards(task_id),
    }
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / f"{label}.json").write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
