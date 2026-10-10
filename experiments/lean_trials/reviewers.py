"""Optional bounded live comparison. Only synthetic source is sent to Claude.

36 calls maximum, $0.05/call ceiling, no tools/customizations/session persistence.
First provider error stops the experiment. No fallback or retry consumes quota.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import random
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CASES = [
    {
        "id": "scope_leak",
        "bug": True,
        "contract": "A grant permits only its operation AND account. The caller supplies an untrusted account.",
        "code": "def allowed(grant, op, account):\n    return op == grant['operation']\n",
        "args": [{"operation": "submit", "account": "A"}, "submit", "B"],
        "expected": False,
    },
    {
        "id": "retry_key",
        "bug": True,
        "contract": "The receiver deduplicates by key. Retries of one logical run must have the same key.",
        "code": "def key(run, attempt):\n    return f'{run}:{attempt}'\n",
        "args": ["run-1", 2],
        "expected": "run-1",
    },
    {
        "id": "stale_review",
        "bug": True,
        "contract": (
            "Landing requires review of the current code hash AND current attempt. "
            "Old attempts cannot authorize landing."
        ),
        "code": "def ready(review, code_hash, attempt):\n    return review['hash'] == code_hash and review['ok']\n",
        "args": [{"hash": "abc", "attempt": 1, "ok": True}, "abc", 2],
        "expected": False,
    },
    {
        "id": "owner_yolo",
        "bug": False,
        "contract": (
            "The authenticated owner deliberately chooses YOLO: allow all actions, even destructive ones. "
            "Other modes refuse destructive actions. There is intentionally no universal hard-danger floor."
        ),
        "code": (
            "def allowed(mode, destructive):\n    if mode == 'yolo':\n        return True\n    return not destructive\n"
        ),
        "args": ["yolo", True],
        "expected": True,
    },
    {
        "id": "parked_slot",
        "bug": False,
        "contract": (
            "Only running jobs consume worker slots. Paused jobs and jobs awaiting approval "
            "retain their durable state but release the worker slot."
        ),
        "code": "def slots(jobs):\n    return sum(job['state'] == 'running' for job in jobs)\n",
        "args": [[{"state": "running"}, {"state": "waiting_for_approval"}]],
        "expected": 1,
    },
    {
        "id": "cache_miss",
        "bug": False,
        "contract": (
            "A cached result is usable only with matching code hash and test-command hash. "
            "A miss returns None and triggers fresh tests; it must not mean success."
        ),
        "code": (
            "def cached(row, code_hash, command_hash):\n"
            "    if row is None or (row['code'], row['command']) != (code_hash, command_hash):\n"
            "        return None\n    return row['passed']\n"
        ),
        "args": [{"code": "old", "command": "cmd", "passed": True}, "new", "cmd"],
        "expected": None,
    },
]
SYSTEM = (
    "You are reviewing a small synthetic function against its explicit contract. "
    "Return ONLY JSON with keys bug (boolean) and reason (short string). "
    "A bug must violate the stated contract, not an unstated product preference. No tools are available."
)
STRATEGIES = {
    "plain": "Review this code for a concrete contract violation.",
    "invariant": (
        "Check the contract's invariants against each branch. Try a counterexample, "
        "including retries, stale state and authorization where relevant. "
        "Do not invent requirements or flag intentional behavior."
    ),
}


def validate_cases() -> None:
    for case in CASES:
        namespace = {}
        exec(compile(case["code"], "<synthetic-fixture>", "exec"), namespace)
        function = next(value for name, value in namespace.items() if name != "__builtins__")
        assert (function(*case["args"]) != case["expected"]) == case["bug"], case["id"]


def call(case: dict, strategy: str, repeat: int, cwd: str) -> dict:
    prompt = f"{STRATEGIES[strategy]}\nCONTRACT: {case['contract']}\nCODE:\n{case['code']}"
    argv = [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--model",
        "sonnet",
        "--effort",
        "low",
        "--safe-mode",
        "--restricted",
        "--setting-sources",
        "",
        "--tools",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--no-chrome",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--system-prompt",
        SYSTEM,
        "--max-budget-usd",
        "0.05",
    ]
    start = time.monotonic()
    row = {
        "case": case["id"],
        "strategy": strategy,
        "repeat": repeat,
        "expected_bug": case["bug"],
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
    }
    try:
        response = subprocess.run(argv, input=prompt, capture_output=True, text=True, cwd=cwd, timeout=45)
        payload = json.loads(response.stdout)
        row.update(
            {
                "cost_usd": payload.get("total_cost_usd"),
                "usage": payload.get("usage"),
                "models": list(payload.get("modelUsage", {})),
                "turns": payload.get("num_turns"),
            }
        )
        answer = payload.get("result", "")
        if response.returncode or payload.get("is_error"):
            row["error"] = answer[:400] or payload.get("subtype", "provider error")
        else:
            row["answer"] = answer[:1000]
            text = answer.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            decoded = json.loads(text)
            assert isinstance(decoded["bug"], bool)
            row["predicted_bug"] = decoded["bug"]
            row["correct"] = decoded["bug"] == case["bug"]
    except (subprocess.TimeoutExpired, ValueError, KeyError, AssertionError) as exc:
        row["error"] = f"{type(exc).__name__}: {str(exc)[:250]}"
    row["seconds"] = round(time.monotonic() - start, 3)
    return row


def summary(rows: list) -> dict:
    result = {}
    for strategy in STRATEGIES:
        valid = [r for r in rows if r["strategy"] == strategy and "correct" in r]
        result[strategy] = {
            "completed": len(valid),
            "correct": sum(r["correct"] for r in valid),
            "missed_bugs": sum(r["expected_bug"] and not r["predicted_bug"] for r in valid),
            "false_alarms": sum(not r["expected_bug"] and r["predicted_bug"] for r in valid),
            "median_seconds": statistics.median(r["seconds"] for r in valid) if valid else None,
            "reported_cost_usd": round(sum(r.get("cost_usd") or 0 for r in rows if r["strategy"] == strategy), 6),
            "errors": sum("error" in r for r in rows if r["strategy"] == strategy),
        }
    pairs = []
    for case in CASES:
        for repeat in range(3):
            matched = {r["strategy"]: r for r in rows if r.get("case") == case["id"] and r.get("repeat") == repeat}
            if len(matched) == 2 and all("correct" in r for r in matched.values()):
                pairs.append((matched["plain"]["correct"], matched["invariant"]["correct"]))
    result["paired_accuracy"] = {
        "invariant_wins": sum(not a and b for a, b in pairs),
        "plain_wins": sum(a and not b for a, b in pairs),
        "ties": sum(a == b for a, b in pairs),
        "pairs": len(pairs),
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=HERE / "reviewer_results.json")
    args = parser.parse_args()
    validate_cases()
    version = subprocess.check_output(["claude", "--version"], text=True).strip()
    jobs = [(case, strategy, repeat) for case in CASES for strategy in STRATEGIES for repeat in range(3)]
    random.Random(63165).shuffle(jobs)
    result = {
        "kind": "live Claude, synthetic fixtures, equal information and effort in both arms",
        "issues": [63, 65, 67],
        "cli_version": version,
        "cases": CASES,
        "system_prompt": SYSTEM,
        "strategies": STRATEGIES,
        "seed": 63165,
        "limits": {"calls": 36, "dollars_per_call": 0.05, "seconds_per_call": 45},
        "rows": [],
    }

    def save() -> None:
        result["summary"] = summary(result["rows"])
        args.output.write_text(json.dumps(result, indent=2) + "\n")

    with tempfile.TemporaryDirectory(prefix="north-reviewer-trials-") as cwd:
        # Probe first so quota/auth failure does not produce a storm of retries.
        case, strategy, repeat = jobs.pop(0)
        row = call(case, strategy, repeat, cwd)
        result["rows"].append(row)
        save()
        print(json.dumps(row), flush=True)
        if "error" not in row:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                while jobs:
                    batch = [jobs.pop(0) for _ in range(min(2, len(jobs)))]
                    futures = [executor.submit(call, case, strategy, repeat, cwd) for case, strategy, repeat in batch]
                    batch_rows = [future.result() for future in futures]
                    result["rows"].extend(batch_rows)
                    save()
                    for row in batch_rows:
                        print(
                            json.dumps(
                                {
                                    k: row[k]
                                    for k in ("case", "strategy", "repeat", "seconds", "correct", "error")
                                    if k in row
                                }
                            ),
                            flush=True,
                        )
                    if any("error" in row for row in batch_rows):
                        break
    print(json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    main()
