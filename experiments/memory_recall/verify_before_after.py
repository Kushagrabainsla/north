"""Bucket 1, before and after: the product code of the current checkout on a copy of the memory.

Run it on main (before) and on the PR branch (after). It uses only functions that
exist in both, and reports what each version does on the same data:

1. The profile: rebuilt by `memory.profile.rebuild_profile`, how much of each section
   the 12,000-character recall cap cuts.
2. Recall (`LocalMemoryGateway.recall`) for the 196 replayed prompts: facts given, how
   many of them the profile already shows, memory tokens, and whether ids come back.
3. #85, after only: `memory.memory_use.judge`, with north's own AnthropicProvider and
   Haiku 5.5, on 20 replayed tasks that have reference labels. Agreement with the reference.
4. #91, after only: for every replayed task, the facts that the product cutoff
   (`_apply_recall_cutoff` with boosts) keeps against the policy that the replay measured.

Writes results/verify_<label>.json next to this file. Run it against a checkout:
    .venv/bin/python experiments/memory_recall/verify_before_after.py <label> [checkout]
The checkout defaults to the one that holds this file; give another one (for example
main, with this file copied outside it) to measure that code.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import statistics
import sys
from pathlib import Path
from typing import Any

REPO = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from memory import LocalMemoryGateway, SQLiteContextStore  # noqa: E402
from memory.episodic import EpisodicStore  # noqa: E402
from memory.facts import FactStore  # noqa: E402
from memory.models import ContextDocument  # noqa: E402
from memory.profile import rebuild_profile  # noqa: E402

HOME = Path.home() / ".north"
DATA = HOME / "experiments" / "memory_recall"
WORK = DATA / "verify"
CAP = 12_000
OUT = Path(__file__).resolve().parent / "results"

_ENCODER: Any = None


async def embed_fn(texts: list[str]) -> list[list[float]]:
    global _ENCODER
    if _ENCODER is None:
        from inference.providers.local_embeddings import DEFAULT_MODEL_ID
        from inference.providers.onnx_encoder import OnnxTextEncoder

        _ENCODER = OnnxTextEncoder(DEFAULT_MODEL_ID)
    return [[float(v) for v in row] for row in await asyncio.to_thread(_ENCODER.encode, texts)]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def sections(text: str) -> list[tuple[str, int, int]]:
    """(heading, start, length) of each '## ' section."""
    out, start = [], 0
    for line_start in [0, *[i + 1 for i, c in enumerate(text) if c == "\n"]]:
        if text.startswith("## ", line_start):
            out.append(line_start)
    bounds = [*out, len(text)]
    result = []
    for begin, end in zip(bounds, bounds[1:], strict=False):
        heading = text[begin:end].splitlines()[0].lstrip("# ").strip()
        result.append((heading, begin, end - begin))
    del start
    return result


def cut_by_cap(text: str) -> dict[str, int]:
    return {h: min(n, b + n - CAP) for h, b, n in sections(text) if b + n > CAP}


async def judge_check(gateway: LocalMemoryGateway) -> dict[str, Any] | None:
    try:
        from memory import memory_use
        from memory.models import MemoryItem
    except ImportError:
        return None
    from inference.providers.anthropic_provider import AnthropicProvider

    key = next(
        line.split("=", 1)[1].strip().strip("\"'")
        for line in (HOME / ".env").read_text().splitlines()
        if line.startswith("NORTH_ANTHROPIC_API_KEY=")
    )
    provider = AnthropicProvider(key)

    class Router:
        async def complete(self, request):  # type: ignore[no-untyped-def]
            return await provider.complete("claude-haiku-5-5", request)

    class Runs:
        def __init__(self, calls: list[dict[str, Any]]) -> None:
            self.calls = calls
            self.events: list[dict[str, Any]] = []

        async def list_events(self, run_id: str) -> list[dict[str, Any]]:
            return [{"event": "tool_called", "data": {"tool": c["tool"], "params": c["params"]}} for c in self.calls]

        async def record_event(self, run_id: str, task_id: str, event: str, data: dict[str, Any]) -> None:
            self.events.append({"event": event, **data})

    labels = {r["run_id"]: r["reference"] for r in read_jsonl(DATA / "e85_labels.jsonl")}
    cases = [c for c in read_jsonl(DATA / "cases.jsonl") if c["run_id"] in labels][-20:]
    agree = total = tp = fp = fn = 0
    recorded = 0
    for case in cases:
        items = [MemoryItem(i["id"], i["kind"], i["text"]) for i in case["facts"] + case["episodes"] + case["profile"]]
        runs = Runs(case["tool_calls"])
        body = await memory_use.judge(
            Router(),
            runs,
            run_id=case["run_id"],
            task_id="t",
            task=case["prompt"],
            answer=case["output"],
            items=items,
        )
        recorded += any(e["event"] == memory_use.EVENT for e in runs.events)
        used = set(body["used"]) if body else set()
        for item in items:
            ref = labels[case["run_id"]].get(item.id, False)
            got = item.id in used
            total += 1
            agree += ref == got
            tp += ref and got
            fp += got and not ref
            fn += ref and not got
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    del gateway
    return {
        "tasks": len(cases),
        "items": total,
        "memory_used_events_recorded": recorded,
        "agreement_with_reference": round(agree / total, 3),
        "precision": round(precision, 3),
        "recall": round(recall, 3),
    }


def ranking_check() -> dict[str, Any] | None:
    """The product cutoff with boosts against the cutoff the #91 replay measured, on its own candidate pools."""
    import inspect

    from memory import facts as facts_module

    if "boosts" not in inspect.signature(facts_module._apply_recall_cutoff).parameters:
        return None
    pools_path = DATA / "verify_pools.json"
    if not pools_path.exists():
        return {"skipped": "no pools file; run experiments.memory_recall.e91_fading with --dump-pools first"}
    pools = json.loads(pools_path.read_text())
    same = 0
    for pool in pools:
        scored = sorted(((f["id"], f["id"], f["score"]) for f in pool["facts"]), key=lambda r: r[2], reverse=True)
        boosts = {f["id"]: f["boost"] for f in pool["facts"]}
        product = [fid for fid, _ in facts_module._apply_recall_cutoff(scored, 15, boosts)]
        same += product == pool["replay_pick"]
    return {"tasks": len(pools), "same_pick_as_replay": same}


async def main() -> None:
    label = sys.argv[1] if len(sys.argv) > 1 else "run"
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    for name in ("facts.db", "memory.db", "episodic.db"):
        with sqlite3.connect(DATA / "snapshot" / name) as src, sqlite3.connect(WORK / name) as dst:
            src.backup(dst)
    context = SQLiteContextStore(WORK / "memory.db")
    facts = FactStore(db_path=WORK / "facts.db", embed_fn=embed_fn)
    episodes = EpisodicStore(db_path=WORK / "episodic.db", embed_fn=embed_fn)
    await rebuild_profile(facts, context)
    profile = await context.read(ContextDocument.USER)

    gateway = LocalMemoryGateway(context, facts, episodes)
    principal = await gateway.principal_for("north", "general")
    cases = read_jsonl(DATA / "cases.jsonl")
    given, repeated, tokens, with_ids = [], 0, [], 0
    shown = profile[:CAP]
    for case in cases:
        recalled = await gateway.recall(principal, case["prompt"])
        given.append(len(recalled.facts))
        repeated += sum(f"- {fact}" in shown for fact in recalled.facts)
        tokens.append((len(recalled.render()) + 3) // 4)
        with_ids += bool(getattr(recalled, "fact_ids", None))
    report = {
        "label": label,
        "profile_chars": len(profile),
        "cut_by_cap": cut_by_cap(profile),
        "prompts": len(cases),
        "facts_given": sum(given),
        "facts_given_that_the_profile_shows": repeated,
        "mean_memory_tokens": round(statistics.mean(tokens)),
        "recalls_with_fact_ids": with_ids,
        "use_judge": await judge_check(gateway),
        "ranking": ranking_check(),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"verify_{label}.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2))
    shutil.rmtree(WORK, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
