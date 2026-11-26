"""Issue #94: does a model's reasoning strength (effort) change north's answers enough to need a setting?

Takes 30 past chat prompts of the north agent that it answered with at most one tool
call (~/.north/tasks/tasks.db, read-only), and answers each one with Opus 5.5
(the user's model for tasks) at effort low, medium (its default) and high. One turn,
no tools: this measures the model, not north's loop.

A Sonnet 5.5 judge compares each low and high answer with the medium one, in
random order, and says which is better or that they are equal. Time and output tokens
are recorded for each call.

Prompts and answers stay in ~/.north/experiments/model_contention; only counts go to results/.

Run: .venv/bin/python -m experiments.model_contention.e94_effort
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Any

import anthropic

HOME = Path.home() / ".north"
DATA = HOME / "experiments" / "model_contention"
RESULTS = Path(__file__).resolve().parent / "results"
MODEL = "claude-opus-5-5"
JUDGE = "claude-sonnet-5-5"
EFFORTS = ("low", "medium", "high")
CASES = 30
SYSTEM = (
    "You are north, the user's personal assistant. Answer the request directly. "
    "You have no tools in this conversation: when the request needs an action, say briefly what you would do."
)
JUDGE_SYSTEM = (
    "You compare two answers to the same request from a personal assistant. Judge correctness, completeness "
    "and usefulness for the user. Length is not quality. Reply with JSON only."
)
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {"better": {"type": "string", "enum": ["A", "B", "equal"]}, "reason": {"type": "string"}},
    "required": ["better", "reason"],
    "additionalProperties": False,
}


def _key() -> str:
    for line in (HOME / ".env").read_text().splitlines():
        if line.startswith("NORTH_ANTHROPIC_API_KEY="):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise RuntimeError("NORTH_ANTHROPIC_API_KEY is not set")


CLIENT = anthropic.AsyncAnthropic(api_key=_key(), max_retries=4)
LIMIT = asyncio.Semaphore(6)


async def _cached(name: str, request: dict[str, Any]) -> dict[str, Any]:
    path = DATA / "cache" / (hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest() + ".json")
    if path.exists():
        return json.loads(path.read_text())
    path.parent.mkdir(parents=True, exist_ok=True)
    async with LIMIT:
        started = time.monotonic()
        response = await CLIENT.messages.create(**request)
        seconds = time.monotonic() - started
    text = "\n".join(b.text for b in response.content if b.type == "text")
    result = {"text": text, "seconds": seconds, "output_tokens": response.usage.output_tokens, "name": name}
    path.write_text(json.dumps(result))
    return result


def prompts() -> list[str]:
    with sqlite3.connect(f"file:{HOME / 'tasks' / 'tasks.db'}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT r.prompt, (SELECT COUNT(*) FROM agent_run_events e WHERE e.run_id = r.run_id "
            "AND e.event = 'tool_called') FROM agent_runs r WHERE r.agent IN ('general', 'north') "
            "AND r.status = 'completed' AND r.parent_run_id IS NULL AND r.prompt NOT LIKE 'Execute flow%'"
        ).fetchall()
    unique = sorted({p.strip() for p, calls in rows if calls <= 1 and 10 < len(p.strip()) < 3000})
    random.Random(94).shuffle(unique)
    return unique[:CASES]


async def answer(prompt: str, effort: str) -> dict[str, Any]:
    return await _cached(
        effort,
        {
            "model": MODEL,
            "max_tokens": 16000,
            "system": SYSTEM,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": {"effort": effort},
        },
    )


async def judge(prompt: str, base: str, other: str, salt: str) -> str:
    """'better', 'worse' or 'equal' for *other* against *base*."""
    flip = random.Random(salt).random() < 0.5
    first, second = (other, base) if flip else (base, other)
    result = await _cached(
        "judge",
        {
            "model": JUDGE,
            "max_tokens": 4000,
            "system": JUDGE_SYSTEM,
            "messages": [
                {"role": "user", "content": f"## Request\n{prompt}\n\n## Answer A\n{first}\n\n## Answer B\n{second}"}
            ],
            "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": JUDGE_SCHEMA}},
        },
    )
    verdict = json.loads(result["text"])["better"]
    if verdict == "equal":
        return "equal"
    other_is = "A" if flip else "B"
    return "better" if verdict == other_is else "worse"


async def main() -> None:
    cases = prompts()
    answers = await asyncio.gather(*(answer(p, e) for p in cases for e in EFFORTS))
    by = {(p, e): a for (p, e), a in zip(((p, e) for p in cases for e in EFFORTS), answers, strict=True)}
    report: dict[str, Any] = {"model": MODEL, "judge": JUDGE, "prompts": len(cases), "efforts": {}}
    for effort in EFFORTS:
        rows = [by[(p, effort)] for p in cases]
        report["efforts"][effort] = {
            "median_seconds": round(statistics.median(r["seconds"] for r in rows), 1),
            "mean_output_tokens": round(statistics.mean(r["output_tokens"] for r in rows)),
        }
    for effort in ("low", "high"):
        verdicts = await asyncio.gather(
            *(judge(p, by[(p, "medium")]["text"], by[(p, effort)]["text"], f"{p}{effort}") for p in cases)
        )
        report["efforts"][effort]["against_medium"] = {v: verdicts.count(v) for v in ("better", "equal", "worse")}
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "e94_effort.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
