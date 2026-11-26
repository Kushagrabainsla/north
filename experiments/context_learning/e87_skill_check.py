"""Issue #87: do skill drafts fail because the check differs from how live tasks pick skills?

`create_skill` tests a draft with its example requests before it switches it on
(`tools/universal/create_skill.py`, `_selection_failures`): the draft must be picked
for each positive request and for no negative one. The check calls the selector with
its default floor (0.35) and against every skill in north. Live tasks call the same
selector with a floor of 0.70 and only the skills of the agent's domain
(`agents/base.py`, `_SUGGESTED_SKILL_MIN_SIMILARITY`).

This replays every real `create_skill` draft that carried example requests (from
~/.north/tasks/tasks.db, read-only). It rebuilds each draft as `create_skill` does and
runs north's real `SkillSelector` on it, under two rules:

- check:  floor 0.35, every other skill plus the draft (today's check)
- live:   floor 0.70, the other skills of the draft's domains plus the draft (what a task does)

For each draft it records which of its positive requests each rule picks and which
negative requests it wrongly picks. It also reports, for the skills that passed the
check, how many of their own positive requests a live task would pick them for.

The skills are read from ~/.north/skills and the built-in folder; nothing is written.

Run: .venv/bin/python -m experiments.context_learning.e87_skill_check
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from skills.models import SkillSource  # noqa: E402
from skills.registry import SkillRegistry, skill_from_document  # noqa: E402
from skills.selector import SkillSelector  # noqa: E402
from utils.runtime_resources import builtin_skills_dir  # noqa: E402

HOME = Path.home() / ".north"
OUT = Path(__file__).resolve().parent / "results"
LIVE_FLOOR = 0.70  # agents/base.py _SUGGESTED_SKILL_MIN_SIMILARITY
CHECK_FLOOR = None  # the selector's own default (0.35), as create_skill calls it

_ENCODER: Any = None


async def embed_fn(texts: list[str]) -> list[list[float]]:
    global _ENCODER
    if _ENCODER is None:
        from inference.providers.local_embeddings import DEFAULT_MODEL_ID
        from inference.providers.onnx_encoder import OnnxTextEncoder

        _ENCODER = OnnxTextEncoder(DEFAULT_MODEL_ID)
    return [[float(v) for v in row] for row in await asyncio.to_thread(_ENCODER.encode, texts)]


def drafts() -> list[dict[str, Any]]:
    """Every create_skill call with example requests, and whether the real check passed it."""
    with sqlite3.connect(f"file:{HOME / 'tasks' / 'tasks.db'}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT run_id, event, data, timestamp FROM agent_run_events WHERE event IN ('tool_called', 'tool_result') "
            "AND json_extract(data, '$.tool') = 'create_skill' ORDER BY id"
        ).fetchall()
    out, pending = [], {}
    for run_id, event, data, stamp in rows:
        parsed = json.loads(data)
        if event == "tool_called":
            pending[run_id] = (parsed.get("params") or {}, stamp)
            continue
        params, called_at = pending.pop(run_id, ({}, stamp))
        if not params.get("positive_prompts") or not params.get("instructions"):
            continue
        error = str(parsed.get("error") or "")
        out.append(
            {
                "run_id": run_id,
                "at": called_at[:16],
                "params": params,
                "passed": bool(parsed.get("success")),
                "selection_failed": "selection tests failed" in error.lower(),
            }
        )
    return out


def build(params: dict[str, Any], folder: Path) -> Any:
    """The draft skill as create_skill builds it."""
    name = str(params.get("name") or "draft").strip().lower().replace(" ", "-")
    metadata = {
        "name": name,
        "description": str(params.get("description") or ""),
        "source": SkillSource.LEARNED.value,
        "status": "active",
        "domains": params.get("domains") or ["general"],
        "intents": params.get("intents") or [],
    }
    document = f"---\n{yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True)}---\n\n{params['instructions']}\n"
    return skill_from_document(document, folder / name, SkillSource.LEARNED)


async def rule(selector: SkillSelector, skill: Any, others: list[Any], prompts: list[str], floor: float | None) -> int:
    count = 0
    for prompt in prompts:
        picked = await selector.select(prompt, candidates=[*others, skill], min_similarity=floor)
        count += skill.name in {item.name for item in picked}
    return count


async def main() -> None:
    registry = SkillRegistry(builtin_dir=builtin_skills_dir(), learned_dir=HOME / "skills")
    selector = SkillSelector(registry, embed_fn=embed_fn)
    calls = drafts()
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for call in calls:
            params = call["params"]
            try:
                skill = build(params, Path(tmp))
            except Exception as exc:  # a draft the real tool also refused before the check
                rows.append({**{k: call[k] for k in ("at", "passed")}, "error": type(exc).__name__})
                continue
            positives = [str(p) for p in params.get("positive_prompts") or []]
            negatives = [str(p) for p in params.get("negative_prompts") or []]
            everyone = [s for s in registry.all() if s.name != skill.name]
            same_domain = [s for s in everyone if any(s.available_to(d) for d in skill.domains)]
            check_pos = await rule(selector, skill, everyone, positives, CHECK_FLOOR)
            check_neg = await rule(selector, skill, everyone, negatives, CHECK_FLOOR)
            live_pos = await rule(selector, skill, same_domain, positives, LIVE_FLOOR)
            live_neg = await rule(selector, skill, same_domain, negatives, LIVE_FLOOR)
            rows.append(
                {
                    "at": call["at"],
                    "passed": call["passed"],
                    "selection_failed": call["selection_failed"],
                    "positives": len(positives),
                    "negatives": len(negatives),
                    "check_pass": check_pos == len(positives) and check_neg == 0,
                    "live_pass": live_pos == len(positives) and live_neg == 0,
                    "check_picked_positives": check_pos,
                    "check_picked_negatives": check_neg,
                    "live_picked_positives": live_pos,
                    "live_picked_negatives": live_neg,
                }
            )
    tested = [r for r in rows if "check_pass" in r]
    recorded_fail = [r for r in tested if r["selection_failed"]]
    recorded_pass = [r for r in tested if r["passed"]]
    report = {
        "drafts_with_examples": len(rows),
        "replayed": len(tested),
        "replay_agrees_with_the_recorded_check": sum(
            r["check_pass"] == r["passed"] for r in tested if r["passed"] or r["selection_failed"]
        ),
        "of_recorded": sum(1 for r in tested if r["passed"] or r["selection_failed"]),
        "failed_the_selection_check": {
            "drafts": len(recorded_fail),
            "would_pass_the_live_rule": sum(r["live_pass"] for r in recorded_fail),
            "why_the_check_failed_them": dict(
                Counter(
                    ("negatives picked" if r["check_picked_negatives"] else "")
                    + (" + " if r["check_picked_negatives"] and r["check_picked_positives"] < r["positives"] else "")
                    + ("positives missed" if r["check_picked_positives"] < r["positives"] else "")
                    for r in recorded_fail
                )
            ),
        },
        "passed_the_check": {
            "drafts": len(recorded_pass),
            "positive_requests": sum(r["positives"] for r in recorded_pass),
            "picked_by_a_live_task": sum(r["live_picked_positives"] for r in recorded_pass),
            "drafts_a_live_task_never_picks": sum(r["live_picked_positives"] == 0 for r in recorded_pass),
        },
        "rows": rows,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "e87_skill_check.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
