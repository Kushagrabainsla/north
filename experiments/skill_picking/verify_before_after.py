"""Issue #87, before and after: how north picks skills and makes them, with the code of a checkout.

Run it on main (before) and on the PR branch (after). It uses only code that exists in both.

Part 1, picks. For each request, the code of the checkout decides what the task sees about
skills: `Agent._select_skills` and `Agent._load_skills_block`, for the north agent (domain
general), with the real skills (built-in and ~/.north/skills, read-only) and north's local
embeddings. The task model (Opus 5.5, medium effort) then sees exactly that text and the
request, and answers which skill it would load with use_skill, or none.
- chat: 168 distinct requests of north's finished chat runs; the truth is the label of a judge
  (Opus 5.5, high effort) that saw every skill in full (cached from e87_list_vs_score)
- drafts: the 64 example requests of create_skill drafts whose skill exists now; the draft is the label

Part 2, drafts. Each real create_skill draft goes through the checkout's own CreateSkillTool.run,
in a temporary skills folder (a copy of the user's skills, without the draft's own skill for a
create). Counts how many are saved, and why the others are refused. Nothing in ~/.north changes.

Part 3, cost: the size of the skills text that every task carries.

The judge must see the same skills in both runs, or the truth changes with the branch (the
branch edits the skill-authoring guide). So it reads the built-in skills of a fixed checkout:
main's for both runs.

Writes results/verify_<label>.json next to this file:
    .venv/bin/python experiments/skill_picking/verify_before_after.py <label> [checkout] [judge checkout]
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic

REPO = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
JUDGE_REPO = Path(sys.argv[3]).resolve() if len(sys.argv) > 3 else REPO

from agents.base import Agent  # noqa: E402
from agents.models import AgentDependencies, AgentPayload  # noqa: E402
from skills.registry import SkillRegistry  # noqa: E402
from skills.selector import SkillSelector  # noqa: E402
from tools.models import ToolInput  # noqa: E402
from tools.universal.create_skill import CreateSkillTool  # noqa: E402
from utils.runtime_resources import builtin_skills_dir  # noqa: E402

HOME = Path.home() / ".north"
CACHE = HOME / "experiments" / "context_learning" / "cache"  # shared with e87_list_vs_score
OUT = Path(__file__).resolve().parent / "results"
DOMAIN = "general"
JUDGE = ("claude-opus-5-5", "high")
PICKER = ("claude-opus-5-5", "medium")
BODY_CHARS = 3000

SCHEMA = {
    "type": "object",
    "properties": {"skill": {"type": "string"}, "reason": {"type": "string"}},
    "required": ["skill", "reason"],
    "additionalProperties": False,
}
# The judge's prompt, word for word as in e87_list_vs_score, so its cached labels are reused.
JUDGE_SYSTEM = """You decide which saved procedure (skill) an AI assistant should follow for a user's request.
Below is every skill in full. Answer with the one skill whose procedure this request needs, or "none"
when no skill fits. A skill fits only when following its steps is the right way to do what the request
asks; a skill on a related topic that would not be followed does not fit.

{skills}"""
PICK_SYSTEM = """You are north, a personal AI assistant, about to work on the user's request. Below is the part
of your context that north gave you about skills (saved procedures you load with the use_skill tool).
Answer with the one skill you would load with use_skill before you work on the request, or "none" if you
would load no skill. Use only what is written below.

{block}"""

_ENCODER: Any = None


async def embed_fn(texts: list[str]) -> list[list[float]]:
    global _ENCODER
    if _ENCODER is None:
        from inference.providers.local_embeddings import DEFAULT_MODEL_ID
        from inference.providers.onnx_encoder import OnnxTextEncoder

        _ENCODER = OnnxTextEncoder(DEFAULT_MODEL_ID)
    return [[float(v) for v in row] for row in await asyncio.to_thread(_ENCODER.encode, texts)]


def _key() -> str:
    for line in (HOME / ".env").read_text().splitlines():
        if line.startswith("NORTH_ANTHROPIC_API_KEY="):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise RuntimeError("NORTH_ANTHROPIC_API_KEY is not set")


CLIENT = anthropic.AsyncAnthropic(api_key=_key(), max_retries=4)
LIMIT = asyncio.Semaphore(6)


async def ask(model: str, effort: str, system: str, request: str) -> str:
    """One structured answer, cached on disk by its request (the same cache format as e87_list_vs_score)."""
    body = {
        "model": model,
        "max_tokens": 4000,
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": f"Request:\n{request}"}],
        "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": SCHEMA}},
    }
    path = CACHE / (hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest() + ".json")
    if path.exists():
        return json.loads(path.read_text())["skill"]
    path.parent.mkdir(parents=True, exist_ok=True)
    async with LIMIT:
        response = await CLIENT.messages.create(**body)
    text = next((b.text for b in response.content if b.type == "text"), '{"skill": "none"}')
    try:
        skill = str(json.loads(text).get("skill", "none")).strip()
    except json.JSONDecodeError:
        skill = "none"
    path.write_text(json.dumps({"skill": skill}))
    return skill


def chat_requests() -> list[str]:
    with sqlite3.connect(f"file:{HOME / 'tasks' / 'tasks.db'}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT DISTINCT prompt FROM agent_runs WHERE agent IN ('general', 'north') AND status = 'completed' "
            "AND parent_run_id IS NULL AND prompt NOT LIKE 'Execute flow%'"
        ).fetchall()
    return sorted({p.strip()[:1500] for (p,) in rows if len(p.strip()) > 10})


def drafts() -> list[dict[str, Any]]:
    """Every create_skill call that carried a draft (name, description, instructions), in order."""
    with sqlite3.connect(f"file:{HOME / 'tasks' / 'tasks.db'}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT data FROM agent_run_events WHERE event = 'tool_called' "
            "AND json_extract(data, '$.tool') = 'create_skill' ORDER BY id"
        ).fetchall()
    out = []
    for (data,) in rows:
        params = json.loads(data).get("params") or {}
        if params.get("instructions") and params.get("description"):
            out.append(params)
    return out


def normal(answer: str, names: set[str]) -> str:
    answer = answer.strip().strip("`'\"").lower()
    return answer if answer in names else "none"


class _Agent(Agent):
    async def _execute(self, payload, context, scored_tools):  # type: ignore[no-untyped-def]
        return {}


async def picks_part(registry: SkillRegistry, selector: SkillSelector) -> dict[str, Any]:
    # main's agents read a selector; the branch's have none.
    has_selector = "skill_selector" in {f.name for f in dataclasses.fields(AgentDependencies)}
    deps = AgentDependencies(
        context_store=None,
        inference_router=None,
        tool_registry=None,
        confidence_tracker=None,
        stream_manager=None,
        skill_registry=registry,
        **({"skill_selector": selector} if has_selector else {}),
    )
    agent = _Agent(SimpleNamespace(agent="north", domain=DOMAIN), deps)
    skills = [s for s in registry.all() if s.available_to(DOMAIN)]
    names = {s.name for s in skills}
    judged = SkillRegistry(builtin_dir=JUDGE_REPO / "resources" / "builtin-skills", learned_dir=HOME / "skills")
    judge_skills = [s for s in judged.all() if s.available_to(DOMAIN)]
    assert {s.name for s in judge_skills} == names, "the judge and the task must see the same skills"
    full = "\n\n".join(f"## {s.name}\n{s.description}\n\n{s.body.strip()[:BODY_CHARS]}" for s in judge_skills)

    async def block_for(request: str) -> str:
        selected = await agent._select_skills(request)
        return await agent._load_skills_block(AgentPayload(task_id="t", prompt=request), selected)

    async def pick(request: str, block: str) -> str:
        if not block.strip():
            return "none"
        return normal(await ask(*PICKER, PICK_SYSTEM.format(block=block), request), names)

    report: dict[str, Any] = {}
    chats = chat_requests()
    truth = [
        normal(t, names)
        for t in await asyncio.gather(*(ask(*JUDGE, JUDGE_SYSTEM.format(skills=full), r) for r in chats))
    ]
    blocks = [await block_for(r) for r in chats]
    picked = await asyncio.gather(*(pick(r, b) for r, b in zip(chats, blocks, strict=True)))
    right = sum(t != "none" and t == p for t, p in zip(truth, picked, strict=True))
    wrong = sum(t != "none" and p not in (t, "none") for t, p in zip(truth, picked, strict=True))
    missed = sum(t != "none" and p == "none" for t, p in zip(truth, picked, strict=True))
    extra = sum(t == "none" and p != "none" for t, p in zip(truth, picked, strict=True))
    need = sum(t != "none" for t in truth)
    report["chat"] = {
        "requests": len(chats),
        "need_a_skill": need,
        "right_skill": right,
        "wrong_skill": wrong,
        "missed": missed,
        "skill_found_share": round(right / need, 3) if need else None,
        "loaded_a_skill_when_none_fits": extra,
        "accuracy": round((right + sum(t == "none" == p for t, p in zip(truth, picked, strict=True))) / len(chats), 3),
        "mean_skills_text_tokens": round(sum(len(b) for b in blocks) / len(blocks) / 4),
    }

    examples: dict[tuple[str, str], bool] = {}
    for params in drafts():
        name = str(params.get("name") or "").strip().lower().replace(" ", "-")
        if name not in names:
            continue
        for prompt in params.get("positive_prompts") or []:
            examples[(str(prompt), name)] = True
        for prompt in params.get("negative_prompts") or []:
            examples.setdefault((str(prompt), name), False)
    rows = list(examples.items())
    ex_blocks = [await block_for(prompt) for (prompt, _), _ in rows]
    ex_picked = await asyncio.gather(*(pick(prompt, b) for ((prompt, _), _), b in zip(rows, ex_blocks, strict=True)))
    pos = [(name == p) for ((_, name), must), p in zip(rows, ex_picked, strict=True) if must]
    neg = [(name != p) for ((_, name), must), p in zip(rows, ex_picked, strict=True) if not must]
    report["drafts_examples"] = {
        "requests": len(rows),
        "must_pick_picked": f"{sum(pos)}/{len(pos)}",
        "must_not_kept_out": f"{sum(neg)}/{len(neg)}",
        "accuracy": round((sum(pos) + sum(neg)) / max(len(rows), 1), 3),
    }
    return report


async def drafts_part() -> dict[str, Any]:
    outcomes: Counter[str] = Counter()
    saved = 0
    calls = drafts()
    for params in calls:
        with tempfile.TemporaryDirectory() as tmp:
            learned = Path(tmp) / "skills"
            shutil.copytree(HOME / "skills", learned)
            action = str(params.get("action") or "create").strip().lower()
            name = str(params.get("name") or "").strip().lower().replace(" ", "-")
            if action == "create" and (learned / name).exists():
                shutil.rmtree(learned / name)  # replay the create as it was, before the skill existed
            registry = SkillRegistry(builtin_dir=builtin_skills_dir(), learned_dir=learned)
            selector = SkillSelector(registry, embed_fn=embed_fn)
            tool = CreateSkillTool(registry, learned_dir=learned, skill_selector=selector)
            result = await tool.run(ToolInput(params=params))
            if result.success:
                saved += 1
            else:
                error = str(result.error or "")
                outcomes[error.split(":")[0].split(",")[0][:70]] += 1
    return {"drafts": len(calls), "saved": saved, "refused": dict(outcomes.most_common())}


async def main() -> None:
    label = sys.argv[1] if len(sys.argv) > 1 else "run"
    registry = SkillRegistry(builtin_dir=builtin_skills_dir(), learned_dir=HOME / "skills")
    selector = SkillSelector(registry, embed_fn=embed_fn)
    report = {"label": label, "picks": await picks_part(registry, selector), "drafts": await drafts_part()}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"verify_{label}.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
