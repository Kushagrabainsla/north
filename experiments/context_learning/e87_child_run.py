"""Issue #87: does a separate child run make skill drafts pass the check faster?

Since 2026-11-13, 13 of north's 19 failed meta calls were `create_skill` drafts that
failed the selection check, in 4 runs (4 skills). In the main run the agent retried
in its own conversation: 1, 3, 1 and 8 failed attempts before each skill passed.

This gives each of those 4 skills to a fresh child run instead. The child gets a short
brief (the purpose and procedure of the skill, as the main agent first drafted it, and
the user's example requests) and one tool, `test_skill`. The tool runs north's real
`SkillSelector` on the draft, exactly as the check does, and writes nothing. The example
requests stay fixed, as the user gave them; the child may change only the skill's name
and description. A run ends when the draft passes, or after 8 attempts.

Arms:
- child_today:  the child sees today's feedback (which requests failed), today's check
- child_scores: the child also sees each request's score, the floor, and the skill that
                was picked instead; today's check
- child_live:   the same full feedback, but the check uses the rule live tasks use
                (floor 0.70, only skills of the same domain)

The model is Opus 5.5 at high effort: north's model for tasks, as a strong coding
model would be. Records attempts, pass or fail, and tokens.

Prompts and drafts stay in ~/.north/experiments/context_learning; results/ holds counts.

Run: .venv/bin/python -m experiments.context_learning.e87_child_run
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import anthropic

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from experiments.context_learning.e87_skill_check import HOME, build, drafts, embed_fn  # noqa: E402
from skills.registry import SkillRegistry  # noqa: E402
from skills.selector import DEFAULT_MIN_SIMILARITY, SkillSelector, _retrieval_key  # noqa: E402
from utils.math import cosine_similarity  # noqa: E402
from utils.runtime_resources import builtin_skills_dir  # noqa: E402

MODEL = "claude-opus-5-5"
EFFORT = "high"
MAX_ATTEMPTS = 8
LIVE_FLOOR = 0.70
SINCE = "2026-11-13"
DATA = HOME / "experiments" / "context_learning"
OUT = Path(__file__).resolve().parent / "results"

SYSTEM = """You write a skill for north, a personal AI assistant. A skill is a procedure that north
loads when a request matches it. North picks a skill by comparing the request with the skill's
name and description, using text embeddings.

You get the skill's purpose and procedure, and the user's example requests: requests that must
pick this skill, and requests that must not. Write a name and a description for the skill.
The description must start with "Use when". Call test_skill to test a draft. Change the draft and
test again until it passes. Do not change the procedure or the example requests."""

TEST_TOOL = {
    "name": "test_skill",
    "description": "Test a draft skill against the example requests. Returns whether it passes and why not.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "short kebab-case name"},
            "description": {"type": "string", "description": "starts with 'Use when'"},
        },
        "required": ["name", "description"],
        "additionalProperties": False,
    },
}


def _key() -> str:
    for line in (HOME / ".env").read_text().splitlines():
        if line.startswith("NORTH_ANTHROPIC_API_KEY="):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise RuntimeError("NORTH_ANTHROPIC_API_KEY is not set")


CLIENT = anthropic.AsyncAnthropic(api_key=_key(), max_retries=4)


async def _call(request: dict[str, Any]) -> dict[str, Any]:
    """One model turn, cached on disk by its request."""
    path = DATA / "cache" / (hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest() + ".json")
    if path.exists():
        return json.loads(path.read_text())
    path.parent.mkdir(parents=True, exist_ok=True)
    response = await CLIENT.messages.create(**request)
    result = {
        "content": [b.model_dump(exclude_none=True) for b in response.content],
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }
    path.write_text(json.dumps(result))
    return result


class Checker:
    """North's real selection check, on one draft, with feedback of one of two kinds."""

    def __init__(self, registry: SkillRegistry, selector: SkillSelector, live: bool, scores: bool) -> None:
        self.registry, self.selector, self.live, self.scores = registry, selector, live, scores

    async def test(self, base: dict[str, Any], name: str, description: str, tmp: Path) -> tuple[bool, str]:
        params = {**base, "name": name, "description": description}
        if not description.startswith("Use when"):
            return False, "Description must start with 'Use when'."
        try:
            skill = build(params, tmp)
        except Exception as exc:
            return False, f"Skill is invalid: {exc}"
        others = [s for s in self.registry.all() if s.name != skill.name]
        if self.live:
            others = [s for s in others if any(s.available_to(d) for d in skill.domains)]
        floor = LIVE_FLOOR if self.live else DEFAULT_MIN_SIMILARITY
        candidates = [*others, skill]
        lines, ok = [], True
        for kind, prompts, want in (
            ("must pick", base["positive_prompts"], True),
            ("must not pick", base["negative_prompts"], False),
        ):
            for prompt in prompts:
                picked = await self.selector.select(prompt, candidates=candidates, min_similarity=floor)
                hit = skill.name in {item.name for item in picked}
                if hit == want:
                    continue
                ok = False
                if self.scores:
                    # The text the selector itself embeds for a skill.
                    vectors = await embed_fn([prompt, _retrieval_key(skill)])
                    score = cosine_similarity(vectors[0], vectors[1])
                    rival = picked[0].name if picked else "none (nothing reached the floor)"
                    lines.append(f"- {kind}: {prompt!r}: score {score:.2f}, floor {floor:.2f}, picked instead: {rival}")
                else:
                    lines.append(f"- {kind}: {prompt!r}")
        if ok:
            return True, "The skill passes: it is picked for every request it must, and for none it must not."
        header = "Skill selection tests failed, so nothing changed."
        return False, header + "\n" + "\n".join(lines)


async def child_run(case: dict[str, Any], checker: Checker, tmp: Path) -> dict[str, Any]:
    base = case["params"]
    brief = (
        f"## Purpose (the main agent's first description)\n{base.get('description', '')}\n\n"
        f"## Procedure\n{str(base.get('instructions', ''))[:4000]}\n\n"
        "## Requests that must pick this skill\n" + "\n".join(f"- {p}" for p in base["positive_prompts"]) + "\n\n"
        "## Requests that must not pick it\n" + "\n".join(f"- {p}" for p in base["negative_prompts"])
    )
    messages: list[dict[str, Any]] = [{"role": "user", "content": brief}]
    attempts, tokens_in, tokens_out = 0, 0, 0
    passed = False
    for _turn in range(MAX_ATTEMPTS * 2):
        turn = await _call(
            {
                "model": MODEL,
                "max_tokens": 16000,
                "system": SYSTEM,
                "messages": messages,
                "tools": [TEST_TOOL],
                "output_config": {"effort": EFFORT},
            }
        )
        tokens_in += turn["input_tokens"]
        tokens_out += turn["output_tokens"]
        uses = [b for b in turn["content"] if b.get("type") == "tool_use"]
        if not uses:
            break
        messages.append({"role": "assistant", "content": turn["content"]})
        results = []
        for use in uses:
            attempts += 1
            args = use.get("input") or {}
            passed, feedback = await checker.test(
                base, str(args.get("name", "")), str(args.get("description", "")), tmp
            )
            results.append({"type": "tool_result", "tool_use_id": use["id"], "content": feedback})
            if passed:
                break
        messages.append({"role": "user", "content": results})
        if passed or attempts >= MAX_ATTEMPTS:
            break
    return {"passed": passed, "attempts": attempts, "input_tokens": tokens_in, "output_tokens": tokens_out}


async def main() -> None:
    registry = SkillRegistry(builtin_dir=builtin_skills_dir(), learned_dir=HOME / "skills")
    selector = SkillSelector(registry, embed_fn=embed_fn)
    calls = [c for c in drafts() if c["at"] >= SINCE]
    cases, seen = [], set()
    for call in calls:
        if call["selection_failed"] and call["run_id"] not in seen:
            seen.add(call["run_id"])
            fails = sum(1 for c in calls if c["run_id"] == call["run_id"] and c["selection_failed"])
            cases.append({**call, "main_run_failed_attempts": fails})
    arms = {
        "child_today": Checker(registry, selector, live=False, scores=False),
        "child_scores": Checker(registry, selector, live=False, scores=True),
        "child_live": Checker(registry, selector, live=True, scores=True),
    }
    report: dict[str, Any] = {
        "model": MODEL,
        "effort": EFFORT,
        "cases": len(cases),
        "main_run": {
            "failed_attempts": [c["main_run_failed_attempts"] for c in cases],
            "passed": len(cases),
        },
        "arms": {},
    }
    with tempfile.TemporaryDirectory() as tmp:
        for name, checker in arms.items():
            results = [await child_run(case, checker, Path(tmp)) for case in cases]
            report["arms"][name] = {
                "passed": sum(r["passed"] for r in results),
                "attempts": [r["attempts"] for r in results],
                "failed_attempts": [r["attempts"] - int(r["passed"]) for r in results],
                "input_tokens": sum(r["input_tokens"] for r in results),
                "output_tokens": sum(r["output_tokens"] for r in results),
            }
            print(name, report["arms"][name])
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "e87_child_run.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
