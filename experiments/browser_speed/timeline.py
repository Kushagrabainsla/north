"""Where a browser task's time and tokens go, from north's own saved run events.

Every agent run saves `tool_called`, `tool_result` and `inference_call` events with
timestamps (tasks.db). This rebuilds each run's timeline from them: model time,
tool time per browser action, what each tool returned (characters), and how input
tokens grow turn by turn. Nothing is re-run; it reads a north home.

    .venv/bin/python experiments/browser_speed/timeline.py [NORTH_HOME] [--run RUN_ID ...]

With no run ids it reads every run that called the browser tool.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

HOME = Path.home() / ".north"
# Calls that wait for a person, not for a machine. Their time is the user's, so it is kept apart.
_WAITS_ON_USER = frozenset({"ask_user", "request_approval", "update_plan"})


def _ts(text: str) -> float:
    return datetime.fromisoformat(text).timestamp()


def _browser_runs(db: sqlite3.Connection) -> list[str]:
    rows = db.execute(
        "SELECT DISTINCT run_id FROM agent_run_events WHERE event='tool_called' AND data LIKE '%\"tool\": \"browser\"%'"
    )
    return [row[0] for row in rows]


def _events(db: sqlite3.Connection, run_id: str) -> list[tuple[str, float, dict]]:
    """A run's events in the order they happened: by their emit time, not their row order.

    Rows are written in the background, so a tool_called can be saved before the
    inference_call that produced it.
    """
    rows = db.execute("SELECT event, timestamp, data FROM agent_run_events WHERE run_id=? ORDER BY id", (run_id,))
    events = []
    for event, saved, raw in rows:
        data = json.loads(raw)
        events.append((event, _ts(str(data.get("timestamp") or saved)), data))
    return sorted(events, key=lambda item: item[1])


def _label(data: dict) -> str:
    tool = str(data.get("tool") or "?")
    action = (data.get("params") or {}).get("action")
    return f"{tool}:{action}" if tool == "browser" and action else tool


def analyse(events: list[tuple[str, float, dict]]) -> dict:
    """One run's split: model time, tool time per label, result sizes, tokens per turn."""
    start = next((ts for event, ts, _ in events if event == "agent_started"), events[0][1])
    end = max(ts for _, ts, _ in events)
    pending: list[tuple[str, float]] = []  # tool calls waiting for their result, in order
    tools: dict[str, list[float]] = defaultdict(list)
    chars: dict[str, list[int]] = defaultdict(list)
    model_ms: list[float] = []
    steps: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    turns: list[dict] = []
    profile: dict | None = None
    last_mark = start  # when the model was last handed something to answer
    for event, ts, data in events:
        if event == "inference_call" and data.get("category") == "agent":
            # Saved since issue #83; older runs are timed from the gap before the call.
            model_ms.append(float(data.get("duration_ms") or (ts - last_mark) * 1000))
            turns.append(
                {
                    "tokens_in": int(data.get("tokens_in") or 0),
                    "cached": int(data.get("cached_tokens") or 0),
                    "tokens_out": int(data.get("tokens_out") or 0),
                    "model": data.get("model"),
                }
            )
            last_mark = ts
        elif event == "tool_called":
            pending.append((_label(data), ts))
        elif event == "tool_result" and pending:
            # Results come back in call order for one batch; match the oldest call of this tool.
            index = next((i for i, (label, _) in enumerate(pending) if label.split(":")[0] == data.get("tool")), 0)
            label, called = pending.pop(index)
            tools[label].append((ts - called) * 1000)
            chars[label].append(int(data.get("chars") or len(json.dumps(data.get("formatted") or data))))
            last_mark = max(last_mark, ts)
        elif event == "tool_timing":
            label = _label({"tool": data.get("tool"), "params": {"action": data.get("action")}})
            for name, step in (data.get("spans") or {}).items():
                steps[label][name].append(float(step["ms"]))
        elif event == "prompt_profile" and profile is None:
            profile = data
    return {
        "wall_s": end - start,
        "model_ms": model_ms,
        "tools": tools,
        "chars": chars,
        "turns": turns,
        "steps": steps,
        "first_profile": profile,
    }


def _split_ms(result: dict) -> tuple[float, float, float]:
    """(waiting on the user, browser, other tools), in milliseconds."""
    waits = sum(sum(v) for k, v in result["tools"].items() if k in _WAITS_ON_USER)
    browser = sum(sum(v) for k, v in result["tools"].items() if k.startswith("browser"))
    other = sum(sum(v) for v in result["tools"].values()) - waits - browser
    return waits, browser, other


def _report(run_id: str, result: dict) -> None:
    turns = result["turns"]
    waits, browser_ms, other_ms = _split_ms(result)
    model_total = sum(result["model_ms"])
    print(f"\n=== run {run_id}  model={turns[0]['model'] if turns else '?'}")
    print(
        f"wall {result['wall_s']:.1f}s | user wait {waits / 1000:.1f}s | model {model_total / 1000:.1f}s "
        f"over {len(turns)} turns | browser {browser_ms / 1000:.1f}s | other tools {other_ms / 1000:.1f}s"
    )
    if turns:
        sent = sum(t["tokens_in"] for t in turns)
        cached = sum(t["cached"] for t in turns)
        print(
            f"input tokens {sent:,} (cached {cached:,}) | first turn {turns[0]['tokens_in']:,} "
            f"| last turn {turns[-1]['tokens_in']:,} | output {sum(t['tokens_out'] for t in turns):,}"
        )
    print(f"{'call':<22}{'n':>4}{'mean ms':>10}{'max ms':>10}{'mean chars':>12}{'total chars':>13}")
    for label in sorted(result["tools"], key=lambda k: -sum(result["tools"][k])):
        times, sizes = result["tools"][label], result["chars"][label]
        print(
            f"{label:<22}{len(times):>4}{statistics.mean(times):>10.0f}{max(times):>10.0f}"
            f"{statistics.mean(sizes):>12.0f}{sum(sizes):>13,}"
        )
    for label, named in sorted(result["steps"].items()):
        parts = ", ".join(
            f"{name} {sum(values) / len(result['tools'].get(label) or values):.0f}" for name, values in named.items()
        )
        print(f"  steps of {label}, mean ms per call: {parts}")


def _summary(results: dict[str, dict]) -> None:
    """All runs together: the share of time and the mean cost of each browser action."""
    times: dict[str, list[float]] = defaultdict(list)
    sizes: dict[str, list[int]] = defaultdict(list)
    model, browser, other, waits, wall, tokens, first = 0.0, 0.0, 0.0, 0.0, 0.0, 0, []
    for result in results.values():
        wall += result["wall_s"]
        model += sum(result["model_ms"]) / 1000
        wait_ms, browser_ms, other_ms = _split_ms(result)
        waits, browser, other = waits + wait_ms / 1000, browser + browser_ms / 1000, other + other_ms / 1000
        tokens += sum(t["tokens_in"] for t in result["turns"])
        if result["turns"]:
            first.append(result["turns"][0]["tokens_in"])
        for label, values in result["tools"].items():
            times[label] += values
            sizes[label] += result["chars"][label]
    print(f"\n=== ALL {len(results)} runs")
    busy = wall - waits
    print(
        f"wall {wall:.0f}s, of which user wait {waits:.0f}s | of the rest ({busy:.0f}s): model {model:.0f}s "
        f"({model / busy:.0%}), browser {browser:.0f}s ({browser / busy:.0%}), other tools {other:.0f}s "
        f"({other / busy:.0%})"
    )
    print(f"input tokens {tokens:,} | median first-turn prompt {statistics.median(first):,.0f} tokens")
    print(f"{'call':<22}{'n':>5}{'median ms':>11}{'p90 ms':>9}{'total s':>9}{'median chars':>14}")
    for label in sorted(times, key=lambda k: -sum(times[k])):
        values = sorted(times[label])
        p90 = values[min(len(values) - 1, int(len(values) * 0.9))]
        print(
            f"{label:<22}{len(values):>5}{statistics.median(values):>11.0f}{p90:>9.0f}"
            f"{sum(values) / 1000:>9.1f}{statistics.median(sizes[label]):>14.0f}"
        )


def main(argv: list[str]) -> None:
    home = Path(argv[0]) if argv and not argv[0].startswith("--") else HOME
    run_ids = argv[argv.index("--run") + 1 :] if "--run" in argv else []
    db = sqlite3.connect(home / "tasks" / "tasks.db")
    results = {}
    for run_id in run_ids or _browser_runs(db):
        events = _events(db, run_id)
        if events:
            results[run_id] = analyse(events)
            _report(run_id, results[run_id])
    if len(results) > 1:
        _summary(results)
    first = next((r["first_profile"] for r in results.values() if r["first_profile"]), None)
    if first:
        print("\nfirst-turn prompt sections (estimated tokens):", json.dumps(first.get("sections")))


if __name__ == "__main__":
    main(sys.argv[1:])
