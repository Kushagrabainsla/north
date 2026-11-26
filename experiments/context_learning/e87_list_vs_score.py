"""Issue #87: does the model pick the right skill from a list better than the similarity score does?

Today a task gets a skill's description only when the skill scores 0.70 or more against
the request (`agents/base.py`); every other skill is listed by name only. The open Agent
Skills standard instead shows the model every skill's name and description and lets the
model choose. This compares the two on north's real requests.

Requests:
- chat: the distinct prompts of north's finished chat runs (not flow steps), from
  ~/.north/tasks/tasks.db (read-only)
- examples: the example requests of every create_skill draft whose skill exists now:
  a positive request must pick that skill; a negative one must not

Ground truth for chat requests: a judge (Opus 5.5, high effort) that sees every skill in
full (name, description and steps) answers which skill the request needs, or none.
For example requests, the draft itself is the label.

Pickers:
- score:      north's real SkillSelector, as a live task calls it (floor 0.70, skills of
              the general domain); it may pick up to two
- list_opus:  Opus 5.5 (north's model for tasks), medium effort, sees every skill's name
              and description and answers one skill or none
- list_haiku: the same with Haiku 5.5, low effort (a cheap pre-pick)

Prompts and labels stay in ~/.north/experiments/context_learning; results/ holds counts.

Run: .venv/bin/python -m experiments.context_learning.e87_list_vs_score
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import anthropic

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from experiments.context_learning.e87_skill_check import HOME, drafts, embed_fn  # noqa: E402
from skills.registry import SkillRegistry  # noqa: E402
from skills.selector import SkillSelector  # noqa: E402
from utils.runtime_resources import builtin_skills_dir  # noqa: E402

DOMAIN = "general"
LIVE_FLOOR = 0.70
JUDGE = ("claude-opus-5-5", "high")
PICKERS = {"list_opus": ("claude-opus-5-5", "medium"), "list_haiku": ("claude-haiku-5-5", "low")}
DATA = HOME / "experiments" / "context_learning"
OUT = Path(__file__).resolve().parent / "results"
BODY_CHARS = 3000

SCHEMA = {
    "type": "object",
    "properties": {"skill": {"type": "string"}, "reason": {"type": "string"}},
    "required": ["skill", "reason"],
    "additionalProperties": False,
}
JUDGE_SYSTEM = """You decide which saved procedure (skill) an AI assistant should follow for a user's request.
Below is every skill in full. Answer with the one skill whose procedure this request needs, or "none"
when no skill fits. A skill fits only when following its steps is the right way to do what the request
asks; a skill on a related topic that would not be followed does not fit.

{skills}"""
PICK_SYSTEM = """You are an AI assistant. Before you work on the user's request, choose whether one of your
saved procedures (skills) applies. Each line is a skill's name and description. Answer with the one skill
to load, or "none" when no skill applies.

{skills}"""


def _key() -> str:
    for line in (HOME / ".env").read_text().splitlines():
        if line.startswith("NORTH_ANTHROPIC_API_KEY="):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise RuntimeError("NORTH_ANTHROPIC_API_KEY is not set")


CLIENT = anthropic.AsyncAnthropic(api_key=_key(), max_retries=4)
LIMIT = asyncio.Semaphore(6)


async def ask(model: str, effort: str, system: str, request: str) -> str:
    """One structured answer: a skill name or 'none'. Cached on disk; the system prompt is cached by the API."""
    body = {
        "model": model,
        "max_tokens": 4000,
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": f"Request:\n{request}"}],
        "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": SCHEMA}},
    }
    path = DATA / "cache" / (hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest() + ".json")
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


def example_requests(names: set[str]) -> list[tuple[str, str, bool]]:
    """(request, skill, must_pick) from every draft whose skill exists now."""
    seen: dict[tuple[str, str], bool] = {}
    for call in drafts():
        params = call["params"]
        name = str(params.get("name") or "").strip().lower().replace(" ", "-")
        if name not in names:
            continue
        for prompt in params.get("positive_prompts") or []:
            seen[(str(prompt), name)] = True
        for prompt in params.get("negative_prompts") or []:
            seen.setdefault((str(prompt), name), False)
    return [(prompt, name, must) for (prompt, name), must in seen.items()]


def normal(answer: str, names: set[str]) -> str:
    answer = answer.strip().strip("`'\"").lower()
    return answer if answer in names else "none"


async def main() -> None:
    registry = SkillRegistry(builtin_dir=builtin_skills_dir(), learned_dir=HOME / "skills")
    selector = SkillSelector(registry, embed_fn=embed_fn)
    skills = [s for s in registry.all() if s.available_to(DOMAIN)]
    names = {s.name for s in skills}
    source = {s.name: s.source.value for s in skills}
    full = "\n\n".join(f"## {s.name}\n{s.description}\n\n{s.body.strip()[:BODY_CHARS]}" for s in skills)
    listed = "\n".join(f"- {s.name}: {s.description}" for s in skills)

    async def score_pick(request: str) -> list[str]:
        picked = await selector.select(request, candidates=skills, min_similarity=LIVE_FLOOR)
        return [s.name for s in picked]

    async def list_pick(picker: str, request: str) -> str:
        model, effort = PICKERS[picker]
        return normal(await ask(model, effort, PICK_SYSTEM.format(skills=listed), request), names)

    report: dict[str, Any] = {"skills_in_domain": len(skills), "sets": {}}

    # Chat requests, labelled by the judge.
    chats = chat_requests()
    truth = [
        normal(t, names)
        for t in await asyncio.gather(*(ask(*JUDGE, JUDGE_SYSTEM.format(skills=full), r) for r in chats))
    ]
    score = [await score_pick(r) for r in chats]
    lists = {p: await asyncio.gather(*(list_pick(p, r) for r in chats)) for p in PICKERS}
    needs = [t != "none" for t in truth]
    chat_report: dict[str, Any] = {
        "requests": len(chats),
        "judge_says_a_skill_fits": sum(needs),
        # By kind only: the user's own skill names stay out of the repository.
        "judge_picks_by_kind": dict(Counter(source[t] for t in truth if t != "none")),
        "distinct_skills_the_judge_picked": len({t for t in truth if t != "none"}),
    }

    def tally(picks: list[Any]) -> dict[str, Any]:
        right = wrong_skill = missed = false_pick = right_none = 0
        for want, got in zip(truth, picks, strict=True):
            got_set = set(got) if isinstance(got, list) else ({got} - {"none"})
            if want == "none":
                if got_set:
                    false_pick += 1
                else:
                    right_none += 1
            elif want in got_set:
                right += 1
            elif got_set:
                wrong_skill += 1
            else:
                missed += 1
        need = right + wrong_skill + missed
        return {
            "right_skill": right,
            "wrong_skill": wrong_skill,
            "missed": missed,
            "skill_found_share": round(right / need, 3) if need else None,
            "picked_a_skill_when_none_fits": false_pick,
            "right_none": right_none,
            "accuracy": round((right + right_none) / len(truth), 3),
        }

    chat_report["score"] = tally(score)
    for picker, picks in lists.items():
        chat_report[picker] = tally(picks)
    report["sets"]["chat"] = chat_report

    # Example requests from the skill drafts.
    examples = example_requests(names)
    ex_score = [await score_pick(r) for r, _, _ in examples]
    ex_lists = {p: await asyncio.gather(*(list_pick(p, r) for r, _, _ in examples)) for p in PICKERS}

    def ex_tally(picks: list[Any]) -> dict[str, Any]:
        pos_right = pos_total = neg_right = neg_total = 0
        for (_, name, must), got in zip(examples, picks, strict=True):
            got_set = set(got) if isinstance(got, list) else {got}
            if must:
                pos_total += 1
                pos_right += name in got_set
            else:
                neg_total += 1
                neg_right += name not in got_set
        return {
            "positives_picked": f"{pos_right}/{pos_total}",
            "negatives_kept_out": f"{neg_right}/{neg_total}",
            "accuracy": round((pos_right + neg_right) / max(pos_total + neg_total, 1), 3),
        }

    ex_report: dict[str, Any] = {"requests": len(examples), "score": ex_tally(ex_score)}
    for picker, picks in ex_lists.items():
        ex_report[picker] = ex_tally(picks)
    report["sets"]["examples"] = ex_report

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "e87_list_vs_score.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
