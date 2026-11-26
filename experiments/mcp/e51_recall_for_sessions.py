"""#51: would north's recall help the owner's own `claude` sessions?

The issue proposes a SessionStart hook that puts north's recall into each `claude` session. Before building it,
this replays the owner's real sessions:

1. Sessions: every `claude` session the owner started from the desktop app (entrypoint "claude-desktop"), from
   ~/.claude/projects. Sessions that north started (entrypoint "sdk-cli") are left out. The query is the first
   message the owner typed, as a SessionStart hook could see it at the earliest (the hook itself sees no message,
   so this is the best case for recall).
2. Recall: north's production fact search (15 facts, its own cutoff) and episode search (3), on a copy of
   north's stores, with north's on-device encoder. Time per recall is measured in the process (no HTTP).
3. Judge: Haiku 5.5 reads the message, the session's own memory (~/.claude/CLAUDE.md and the project's memory
   folder) and the recalled items. For each item it says whether the session needed it, and whether the session's
   own memory already holds it. It must quote the words of the message that make an item needed.

Private text (messages, facts, verdicts) stays in ~/.north/experiments/mcp. Only counts go to results/.

    .venv/bin/python experiments/mcp/e51_recall_for_sessions.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import statistics
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))

NORTH_HOME = Path.home() / ".north"
DATA = NORTH_HOME / "experiments" / "mcp"
SNAPSHOT = DATA / "snapshot"
CACHE = DATA / "llm_cache"
PROJECTS = Path.home() / ".claude" / "projects"
GLOBAL_RULES = Path.home() / ".claude" / "CLAUDE.md"
JUDGE = "claude-haiku-5-5"
MAX_MESSAGE = 6_000
MAX_MEMORY = 20_000
SKIP = ("<command-", "<local-command", "Caveat:", "This session is being continued")


# ------------------------------------------------------------------ sessions


def first_message(path: Path) -> tuple[str, str, str] | None:
    """(text, timestamp, entrypoint) of the first message a person typed, or None."""
    for line in path.open(errors="replace"):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("type") != "user" or row.get("isMeta"):
            continue
        content = row.get("message", {}).get("content")
        if isinstance(content, list):
            content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
        if not isinstance(content, str) or not content.strip() or content.lstrip().startswith(SKIP):
            continue
        return content, row.get("timestamp", ""), row.get("entrypoint", "")
    return None


def sessions() -> list[dict[str, Any]]:
    out = []
    for path in sorted(PROJECTS.glob("*/*.jsonl")):
        found = first_message(path)
        if found is None or found[2] != "claude-desktop":
            continue
        out.append({"id": path.stem, "project": path.parent.name, "text": found[0], "at": found[1]})
    return out


def own_memory(project: str) -> str:
    """What the session already had: the global rules and the project's memory folder."""
    parts = [GLOBAL_RULES.read_text(errors="replace")] if GLOBAL_RULES.is_file() else []
    folder = PROJECTS / project / "memory"
    if folder.is_dir():
        parts += [p.read_text(errors="replace") for p in sorted(folder.glob("*.md"))]
    return "\n\n".join(parts)[:MAX_MEMORY]


# ------------------------------------------------------------------ recall


def snapshot() -> None:
    SNAPSHOT.mkdir(parents=True, exist_ok=True)
    for name in ("facts.db", "episodic.db"):
        dst = SNAPSHOT / name
        if dst.exists():
            continue
        with sqlite3.connect(f"file:{NORTH_HOME / name}?mode=ro", uri=True) as src, sqlite3.connect(dst) as out:
            src.backup(out)


_ENCODER: Any = None


async def embed(texts: list[str]) -> list[list[float]]:
    global _ENCODER
    if _ENCODER is None:
        from inference.providers.local_embeddings import DEFAULT_MODEL_ID
        from inference.providers.onnx_encoder import OnnxTextEncoder

        _ENCODER = OnnxTextEncoder(DEFAULT_MODEL_ID)
    return [[float(v) for v in row] for row in await asyncio.to_thread(_ENCODER.encode, texts)]


# ------------------------------------------------------------------ judge

SYSTEM = """You judge whether remembered items would help a coding assistant in one session.

You get the first message the user typed in the session, the assistant's own memory for that project, and items
that another system (north) recalled for the session. For each item decide two things:

needed: true only if the session's work clearly depends on this item. The message must ask for something that the
item changes: a fact about the user, their setup or their rules that the assistant would otherwise get wrong or
have to ask about. Related topics, background or general preferences are not enough. Quote the exact words of the
message that make the item needed; if you cannot quote them, needed is false.

already_known: true if the assistant's own memory already says the same thing (in any words).

Be strict. Most items are not needed."""

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "quote": {"type": "string"},
                    "needed": {"type": "boolean"},
                    "already_known": {"type": "boolean"},
                },
                "required": ["id", "quote", "needed", "already_known"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


def _api_key() -> str:
    for line in (NORTH_HOME / ".env").read_text().splitlines():
        if line.startswith("NORTH_ANTHROPIC_API_KEY="):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise RuntimeError("NORTH_ANTHROPIC_API_KEY is not set in ~/.north/.env")


_CLIENT: Any = None
_LIMIT = asyncio.Semaphore(8)
SPEND = {"calls": 0, "cached": 0, "input_tokens": 0, "output_tokens": 0}


async def judge(message: str, memory: str, items: list[dict[str, str]]) -> list[dict[str, Any]]:
    global _CLIENT
    user = (
        f"<message>\n{message[:MAX_MESSAGE]}\n</message>\n\n<assistant_memory>\n{memory}\n</assistant_memory>\n\n"
        "<recalled_items>\n" + "\n".join(f"[{i['id']}] {i['text']}" for i in items) + "\n</recalled_items>"
    )
    key = hashlib.sha256(json.dumps([JUDGE, SYSTEM, user, SCHEMA]).encode()).hexdigest()
    path = CACHE / f"{key}.json"
    if path.exists():
        SPEND["cached"] += 1
        return json.loads(path.read_text())
    if _CLIENT is None:
        import anthropic

        _CLIENT = anthropic.AsyncAnthropic(api_key=_api_key(), max_retries=4)
    async with _LIMIT:
        response = await _CLIENT.messages.create(
            model=JUDGE,
            max_tokens=8000,
            system=SYSTEM,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        )
    SPEND["calls"] += 1
    SPEND["input_tokens"] += response.usage.input_tokens
    SPEND["output_tokens"] += response.usage.output_tokens
    text = next((b.text for b in response.content if b.type == "text"), '{"items": []}')
    verdicts = json.loads(text)["items"]
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(verdicts))
    return verdicts


# ------------------------------------------------------------------ control

CONTROL = "--control" in sys.argv


async def control(rows: list[dict[str, Any]]) -> None:
    """Can the judge say "needed" at all? Give it each session's own project notes as the recalled items.

    Only sessions whose project has a memory folder take part. The judge sees the global rules as the session's
    own memory, and the project's notes (one item per note) as what was recalled.
    """
    cases = []
    for s in rows:
        folder = PROJECTS / s["project"] / "memory"
        notes = sorted(p for p in folder.glob("*.md") if p.name != "MEMORY.md") if folder.is_dir() else []
        if notes:
            items = [{"id": f"n{n}", "text": p.read_text(errors="replace")[:800]} for n, p in enumerate(notes)]
            cases.append((s, items))
    rules = GLOBAL_RULES.read_text(errors="replace") if GLOBAL_RULES.is_file() else ""
    verdicts = await asyncio.gather(*(judge(s["text"], rules, items) for s, items in cases))
    needed = [sum(bool(v["needed"]) and bool(v["quote"].strip()) for v in vs) for vs in verdicts]
    result = {
        "sessions": len(cases),
        "items": sum(len(items) for _, items in cases),
        "items_needed": sum(needed),
        "sessions_with_a_needed_item": sum(n > 0 for n in needed),
        "judge_calls": SPEND,
    }
    (HERE / "results" / "e51_control.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


# ------------------------------------------------------------------ main


async def main() -> None:
    from memory.episodic import EpisodicStore
    from memory.facts import FactStore

    snapshot()
    facts = FactStore(db_path=SNAPSHOT / "facts.db", embed_fn=embed)
    episodes = EpisodicStore(db_path=SNAPSHOT / "episodic.db", embed_fn=embed)
    await facts.search("warm up")  # loads the encoder and the fact cache once, as a running daemon has

    rows = sessions()
    if CONTROL:
        await control(rows)
        return
    recalled: list[tuple[dict[str, Any], list[dict[str, str]], float]] = []
    for s in rows:
        started = time.perf_counter()
        found_facts, found_episodes = await asyncio.gather(
            facts.search(s["text"][:MAX_MESSAGE], max_results=15),
            episodes.search(s["text"][:MAX_MESSAGE], max_results=3, allowed_domains=frozenset({"general"})),
        )
        ms = (time.perf_counter() - started) * 1000
        items = [{"id": f"f{n}", "text": t} for n, t in enumerate(found_facts)]
        items += [{"id": f"e{n}", "text": t} for n, t in enumerate(found_episodes)]
        recalled.append((s, items, ms))

    verdicts = await asyncio.gather(*(judge(s["text"], own_memory(s["project"]), items) for s, items, _ in recalled))

    private = []
    per_session = []
    for (s, items, ms), verdict in zip(recalled, verdicts, strict=True):
        by_id = {v["id"]: v for v in verdict}
        needed = [i for i in items if by_id.get(i["id"], {}).get("needed") and by_id[i["id"]]["quote"].strip()]
        new = [i for i in needed if not by_id[i["id"]]["already_known"]]
        per_session.append({"items": len(items), "needed": len(needed), "new": len(new), "ms": ms})
        private.append(
            {"session": s["id"], "project": s["project"], "at": s["at"], "verdicts": verdict, "items": items}
        )
    (DATA / "e51_verdicts.jsonl").write_text("".join(json.dumps(p) + "\n" for p in private))

    n = len(per_session)
    items_total = sum(p["items"] for p in per_session)
    times = sorted(p["ms"] for p in per_session)
    result = {
        "sessions": n,
        "projects": len({s["project"] for s, _, _ in recalled}),
        "items_recalled": items_total,
        "items_needed": sum(p["needed"] for p in per_session),
        "items_needed_and_new": sum(p["new"] for p in per_session),
        "sessions_with_a_needed_item": sum(p["needed"] > 0 for p in per_session),
        "sessions_with_a_needed_new_item": sum(p["new"] > 0 for p in per_session),
        "recall_ms_median": round(statistics.median(times), 1),
        "recall_ms_p95": round(times[int(0.95 * (n - 1))], 1),
        "recall_ms_max": round(times[-1], 1),
        "judge": JUDGE,
        "judge_calls": SPEND,
    }
    out = HERE / "results" / "e51_recall_for_sessions.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
