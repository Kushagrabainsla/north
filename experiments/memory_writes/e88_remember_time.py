"""Issue #88, before and after: how long a task waits for a `remember` call, with the code of a checkout.

Run it on main (before) and on the PR branch (after). It uses only code that exists in both.

Inputs: 40 facts of the user's real fact store (seed 0): 20 the user said in chat, 20 found in
files. For each one, a fresh copy of the real store (read with the sqlite backup API, so
~/.north never changes) has that fact removed, and the checkout's own `RememberTool.run` then
writes it again: a chat fact with said_by_user, a found fact with its source. The `about` label
comes from Haiku 5.5, as a task model would give it (cached on disk).

The parts of the call are timed by wrapping what the store is given:
- embed: north's local embedding model (the same ONNX encoder north uses);
- check: the contradiction check (`fact_supersede`), a real Haiku 5.5 call with north's prompt;
- profile: `rebuild_profile`.
`wait` is how long `RememberTool.run` takes to return: what the task waits for. `done` is how
long until the checkout's background work, if any, has finished (`utils.tasks.drain`). The
fact must be active in the store when the call returns, and the check's result must be the
same before and after.

Writes results/e88_<label>.json next to this file:
    .venv/bin/python experiments/memory_writes/e88_remember_time.py <label> [checkout]
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import anthropic

REPO = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import tools.universal.remember as remember_module  # noqa: E402
from memory import FileContextStore  # noqa: E402
from memory.facts import FactStore  # noqa: E402
from tools.models import ToolInput  # noqa: E402
from tools.universal.remember import RememberTool  # noqa: E402
from utils import tasks  # noqa: E402

HOME = Path.home() / ".north"
CACHE = HOME / "experiments" / "memory_writes" / "cache"
OUT = Path(__file__).resolve().parent / "results"
LABELLER = "claude-haiku-5-5"
CHECKER = "claude-haiku-5-5"
PER_KIND = 20

_ENCODER: Any = None


def _key() -> str:
    for line in (HOME / ".env").read_text().splitlines():
        if line.startswith("NORTH_ANTHROPIC_API_KEY="):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise RuntimeError("NORTH_ANTHROPIC_API_KEY is not set")


CLIENT = anthropic.AsyncAnthropic(api_key=_key(), max_retries=4)


async def _encode(texts: list[str]) -> list[list[float]]:
    global _ENCODER
    if _ENCODER is None:
        from inference.providers.local_embeddings import DEFAULT_MODEL_ID
        from inference.providers.onnx_encoder import OnnxTextEncoder

        _ENCODER = OnnxTextEncoder(DEFAULT_MODEL_ID)
    return [[float(v) for v in row] for row in await asyncio.to_thread(_ENCODER.encode, texts)]


async def label(fact: str) -> str:
    """The `about` label a task model would give the fact (cached on disk)."""
    prompt = (
        "A personal assistant saves this fact about its user with a tool that needs `about`: a short label, "
        "a few words, for the one thing the fact gives the current value of ('current job', 'home city', "
        "'resume'). Reply with the label only.\n\nFact: " + fact
    )
    path = CACHE / ("label-" + hashlib.sha256(f"{LABELLER}\n{prompt}".encode()).hexdigest() + ".json")
    if path.exists():
        return json.loads(path.read_text())["about"]
    response = await CLIENT.messages.create(
        model=LABELLER, max_tokens=2000, messages=[{"role": "user", "content": prompt}]
    )
    about = next((b.text for b in response.content if b.type == "text"), "").strip().strip("'\".")[:60]
    if not about:
        raise RuntimeError(f"no label for {fact!r}: {response.stop_reason}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"about": about}))
    return about


def facts() -> list[dict[str, Any]]:
    with sqlite3.connect(f"file:{HOME / 'facts.db'}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute("SELECT * FROM context_facts WHERE status = 'active' ORDER BY id")]
    rng = random.Random(0)
    said = [r for r in rows if not r["source_path"]]
    found = [r for r in rows if r["source_path"]]
    return rng.sample(said, min(PER_KIND, len(said))) + rng.sample(found, min(PER_KIND, len(found)))


def copy_store(target: Path, drop_id: str) -> None:
    source = sqlite3.connect(f"file:{HOME / 'facts.db'}?mode=ro", uri=True)
    dest = sqlite3.connect(target)
    source.backup(dest)
    source.close()
    dest.execute("DELETE FROM context_facts WHERE id = ?", (drop_id,))
    dest.commit()
    dest.close()


class Timer:
    def __init__(self) -> None:
        self.parts: dict[str, float] = {"embed": 0.0, "check": 0.0, "profile": 0.0}
        self.checks = 0
        self.retired: list[str] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        start = time.perf_counter()
        try:
            return await _encode(texts)
        finally:
            self.parts["embed"] += time.perf_counter() - start

    async def supersede(self, new_fact: str, candidates: list[str]) -> list[int]:
        from utils.prompts import load_prompt

        start = time.perf_counter()
        self.checks += 1
        numbered = "\n".join(f"{i}. {c}" for i, c in enumerate(candidates))
        prompt = load_prompt("prompts/fact_supersede.md").format(new_fact=new_fact, numbered=numbered)
        # The first run calls the model and keeps its answer and its measured time. A later run
        # waits that same time and gets that same answer, so before and after differ only in code.
        path = CACHE / ("check-" + hashlib.sha256(f"{CHECKER}\n{prompt}".encode()).hexdigest() + ".json")
        try:
            if path.exists():
                cached = json.loads(path.read_text())
                await asyncio.sleep(cached["seconds"])
                text = cached["text"]
            else:
                response = await CLIENT.messages.create(
                    model=CHECKER, max_tokens=2000, messages=[{"role": "user", "content": prompt}]
                )
                text = next((b.text for b in response.content if b.type == "text"), "{}")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"text": text, "seconds": time.perf_counter() - start}))
            raw = json.loads(text[text.find("{") : text.rfind("}") + 1] or "{}").get("superseded") or []
            stale = [int(i) for i in raw if str(i).lstrip("-").isdigit()]
            self.retired += [candidates[i] for i in stale if 0 <= i < len(candidates)]
            return stale
        finally:
            self.parts["check"] += time.perf_counter() - start


async def one(row: dict[str, Any], tmp: Path) -> dict[str, Any]:
    db = tmp / f"{row['id']}.db"
    copy_store(db, row["id"])
    timer = Timer()
    store = FactStore(db, timer.embed, supersede_fn=timer.supersede)
    documents = FileContextStore(tmp / f"{row['id']}-context")
    original = remember_module.rebuild_profile

    async def timed_rebuild(*args: Any, **kwargs: Any) -> None:
        start = time.perf_counter()
        try:
            await original(*args, **kwargs)
        finally:
            timer.parts["profile"] += time.perf_counter() - start

    remember_module.rebuild_profile = timed_rebuild
    params: dict[str, Any] = {"fact": row["content"], "about": await label(row["content"])}
    if row["source_path"]:
        params["source"] = row["source_path"]
    else:
        params["said_by_user"] = True
    start = time.perf_counter()
    try:
        result = await RememberTool(store, documents).run(ToolInput(params=params))
        wait = time.perf_counter() - start
        active = any(f["content"] == row["content"] and f["status"] == "active" for f in await store.all_facts())
        await tasks.drain(timeout=120)
        done = time.perf_counter() - start
    finally:
        remember_module.rebuild_profile = original
    return {
        "kind": "found" if row["source_path"] else "said",
        "success": result.success,
        "error": result.error,
        "active_on_return": active,
        "wait_s": wait,
        "done_s": done,
        "checks": timer.checks,
        # Hashes, not text: the repo keeps no fact text, and before and after still compare.
        "fact": hashlib.sha256(row["content"].encode()).hexdigest()[:12],
        "retired": sorted(hashlib.sha256(t.encode()).hexdigest()[:12] for t in timer.retired),
        **{f"{k}_s": v for k, v in timer.parts.items()},
    }


def summary(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "median": round(statistics.median(ordered), 3),
        "mean": round(statistics.fmean(ordered), 3),
        "p90": round(ordered[int(0.9 * (len(ordered) - 1))], 3),
        "max": round(ordered[-1], 3),
    }


async def main() -> None:
    label_name = sys.argv[1] if len(sys.argv) > 1 else "run"
    await _encode(["warm up"])  # load the model once, outside the timings
    rows = facts()
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        for row in rows:
            results.append(await one(row, Path(tmp)))
    with_check = [r for r in results if r["checks"]]
    report = {
        "label": label_name,
        "calls": len(results),
        "succeeded": sum(r["success"] for r in results),
        "active_on_return": sum(r["active_on_return"] for r in results),
        "calls_with_a_model_check": len(with_check),
        "facts_retired": sum(len(r["retired"]) for r in results),
        "wait_s": summary([r["wait_s"] for r in results]),
        "wait_s_with_a_check": summary([r["wait_s"] for r in with_check]) if with_check else None,
        "done_s": summary([r["done_s"] for r in results]),
        "parts_mean_s": {
            k: round(statistics.fmean(r[f"{k}_s"] for r in results), 3) for k in ("embed", "check", "profile")
        },
        "under_1s": sum(r["wait_s"] < 1.0 for r in results),
        "rows": results,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"e88_{label_name}.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
