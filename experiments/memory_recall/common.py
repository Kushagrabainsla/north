"""Shared parts of the memory recall experiments (issues #85, #86, #89, #91).

The experiments replay north's own past tasks against a snapshot of the user's
memory. Personal data (prompts, facts, answers, labels) stays in DATA_DIR, inside
north's home folder, and never goes into the repository. Only counts and rates
go into the recorded results.

Model calls go to the Anthropic API with north's own key. Each response is
cached on disk by a hash of its request, so a re-run costs nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

NORTH_HOME = Path.home() / ".north"
DATA_DIR = NORTH_HOME / "experiments" / "memory_recall"
SNAPSHOT_DIR = DATA_DIR / "snapshot"
CACHE_DIR = DATA_DIR / "llm_cache"
RESULTS_DIR = Path(__file__).resolve().parent / "results"

# The same limits as production recall (memory/gateway.py, memory/facts.py).
FACT_LIMIT = 15
EPISODE_LIMIT = 3
PROFILE_CHAR_CAP = 12_000

# USD per million tokens: (input, output). From Anthropic's price list, 2026-11-03.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}


# --------------------------------------------------------------------------- #
# Snapshot


def snapshot(force: bool = False) -> Path:
    """Copy north's memory and run stores into SNAPSHOT_DIR, once. Returns the folder.

    The SQLite backup API reads a consistent copy, WAL included, without
    writing to the live store.
    """
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    sources = {
        "facts.db": NORTH_HOME / "facts.db",
        "episodic.db": NORTH_HOME / "episodic.db",
        "memory.db": NORTH_HOME / "memory.db",
        "tasks.db": NORTH_HOME / "tasks" / "tasks.db",
    }
    for name, src in sources.items():
        dst = SNAPSHOT_DIR / name
        if dst.exists() and not force:
            continue
        with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as source, sqlite3.connect(dst) as target:
            source.backup(target)
    return SNAPSHOT_DIR


# --------------------------------------------------------------------------- #
# Embeddings and recall with ids


_ENCODER: Any = None


def encode(texts: list[str]) -> list[list[float]]:
    global _ENCODER
    if _ENCODER is None:
        from inference.providers.local_embeddings import DEFAULT_MODEL_ID
        from inference.providers.onnx_encoder import OnnxTextEncoder

        _ENCODER = OnnxTextEncoder(DEFAULT_MODEL_ID)
    return [[float(v) for v in row] for row in _ENCODER.encode(texts)]


async def embed_fn(texts: list[str]) -> list[list[float]]:
    return await asyncio.to_thread(encode, texts)


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


@dataclass
class Item:
    """One recalled memory item, as the agent sees it."""

    id: str
    kind: str  # fact | episode | profile
    text: str
    score: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)


class MemorySnapshot:
    """North's memory stores, opened on the snapshot, with recall that keeps item ids."""

    def __init__(self) -> None:
        from memory.facts import FactStore

        snapshot()
        self.fact_store = FactStore(db_path=SNAPSHOT_DIR / "facts.db", embed_fn=embed_fn)
        self._facts: list[Any] | None = None
        self._episodes: list[dict[str, Any]] | None = None
        # When each item came to exist, so a replayed task sees only the memory it could have
        # had. A fact has no creation time: observed_at, else updated_at (which is never earlier).
        self.since: dict[str, str] = {}
        with sqlite3.connect(SNAPSHOT_DIR / "facts.db") as conn:
            for fid, observed, updated in conn.execute("SELECT id, observed_at, updated_at FROM context_facts"):
                self.since[f"fact:{fid}"] = (observed or updated or "")[:19]
        with sqlite3.connect(SNAPSHOT_DIR / "episodic.db") as conn:
            for eid, stamp in conn.execute("SELECT id, timestamp FROM episodes"):
                self.since[f"episode:{eid}"] = (stamp or "")[:19]

    def existed(self, item_id: str, as_of: str | None) -> bool:
        if not as_of:
            return True
        stamp = self.since.get(item_id, "")
        return not stamp or stamp <= as_of[:19]

    async def facts(self) -> list[Any]:
        if self._facts is None:
            self._facts = await self.fact_store._get_cache()
        return self._facts

    def episodes(self) -> list[dict[str, Any]]:
        if self._episodes is None:
            with sqlite3.connect(SNAPSHOT_DIR / "episodic.db") as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT id, domain, outcome, summary, embedding, timestamp FROM episodes "
                    "WHERE embedding IS NOT NULL AND embedding NOT IN ('', '[]')"
                ).fetchall()
            self._episodes = [{**dict(r), "embedding": json.loads(r["embedding"])} for r in rows]
        return self._episodes

    async def scored_facts(self, qvec: list[float], as_of: str | None = None) -> list[Item]:
        """Every current fact (that existed at *as_of*) with its similarity to *qvec*, best first."""
        from memory.facts import _today, _with_dates

        today = _today()
        out = [
            Item(
                id=f"fact:{f.id}",
                kind="fact",
                text=_with_dates(f.content, f.valid_from, f.valid_until, f.observed_at, f.found_in),
                score=cosine(qvec, f.embedding),
                meta={"category": f.category, "observed_at": f.observed_at},
            )
            for f in await self.facts()
            if len(f.embedding) == len(qvec)
            and (f.valid_until is None or f.valid_until >= today)
            and self.existed(f"fact:{f.id}", as_of)
        ]
        out.sort(key=lambda i: i.score, reverse=True)
        return out

    def scored_episodes(
        self, qvec: list[float], domains: frozenset[str] = frozenset({"general"}), as_of: str | None = None
    ) -> list[Item]:
        from memory.episodic import _label

        out = [
            Item(
                id=f"episode:{e['id']}",
                kind="episode",
                text=_label(e["summary"], e["outcome"] or "success"),
                score=cosine(qvec, e["embedding"]),
                meta={"timestamp": e["timestamp"]},
            )
            for e in self.episodes()
            if e["domain"] in domains and len(e["embedding"]) == len(qvec) and self.existed(f"episode:{e['id']}", as_of)
        ]
        out.sort(key=lambda i: i.score, reverse=True)
        return out

    async def recall(self, query: str, as_of: str | None = None) -> dict[str, Any]:
        """Production recall (facts cutoff, top episodes, the profile), with ids, as it was at *as_of*."""
        from memory.facts import _RECALL_ALWAYS_KEEP, _RECALL_MIN_SIMILARITY, _RECALL_RELATIVE_MARGIN

        qvec = encode([query])[0]
        facts = await self.scored_facts(qvec, as_of)
        best = facts[0].score if facts else 0.0
        cutoff = max(_RECALL_MIN_SIMILARITY, best - _RECALL_RELATIVE_MARGIN)
        kept = [
            f
            for rank, f in enumerate(facts[:FACT_LIMIT])
            if f.score >= _RECALL_MIN_SIMILARITY and (rank < _RECALL_ALWAYS_KEEP or f.score >= cutoff)
        ]
        episodes = [e for e in self.scored_episodes(qvec, as_of=as_of) if e.score > 0.3][:EPISODE_LIMIT]
        return {"qvec": qvec, "facts": kept, "episodes": episodes, "profile": profile_sections()}


# --------------------------------------------------------------------------- #
# The profile document


def profile_text() -> str:
    with sqlite3.connect(SNAPSHOT_DIR / "memory.db") as conn:
        row = conn.execute("SELECT content FROM context_documents WHERE document = 'user.md'").fetchone()
    return row[0] if row else ""


def profile_sections(text: str | None = None) -> list[Item]:
    """The profile split at its '## ' headings, as production truncates it (12,000 characters)."""
    full = profile_text() if text is None else text
    items: list[Item] = []
    for chunk in re.split(r"(?m)^(?=## )", full):
        if not chunk.strip():
            continue
        head = chunk.strip().splitlines()[0].lstrip("# ").strip()
        start = full.find(chunk)
        visible = max(0, min(len(chunk), PROFILE_CHAR_CAP - start))
        items.append(
            Item(
                id=f"profile:{head}",
                kind="profile",
                text=chunk.strip(),
                meta={"chars": len(chunk), "visible_chars": visible, "truncated": visible < len(chunk)},
            )
        )
    return items


def tokens(text: str) -> int:
    """North's own estimate: four characters a token."""
    return (len(text) + 3) // 4


# --------------------------------------------------------------------------- #
# Model calls


def _api_key() -> str:
    for line in (NORTH_HOME / ".env").read_text().splitlines():
        if line.startswith("NORTH_ANTHROPIC_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("NORTH_ANTHROPIC_API_KEY is not set in ~/.north/.env")


_CLIENT: Any = None


def client() -> Any:
    global _CLIENT
    if _CLIENT is None:
        import anthropic

        _CLIENT = anthropic.AsyncAnthropic(api_key=_api_key(), max_retries=4)
    return _CLIENT


@dataclass
class Spend:
    calls: int = 0
    cached_calls: int = 0
    usd: float = 0.0
    seconds: float = 0.0

    def add(self, model: str, usage: dict[str, int], seconds: float) -> None:
        price_in, price_out = PRICES[model]
        tokens_in = usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0) * 1.25
        tokens_in += usage.get("cache_read_input_tokens", 0) * 0.1
        self.usd += (tokens_in * price_in + usage.get("output_tokens", 0) * price_out) / 1_000_000
        self.calls += 1
        self.seconds += seconds


SPEND = Spend()
_SEMAPHORE = asyncio.Semaphore(8)


async def ask_json(
    model: str,
    system: str,
    user: str,
    schema: dict[str, Any],
    *,
    effort: str = "low",
    max_tokens: int = 4000,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One structured-output call, cached on disk. Returns {"data", "usage", "seconds"}."""
    request = {
        "model": model,
        "system": system,
        "user": user,
        "schema": schema,
        "effort": effort,
        "tools": tools or [],
    }
    key = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    path = CACHE_DIR / f"{key}.json"
    if path.exists():
        cached = json.loads(path.read_text())
        SPEND.cached_calls += 1
        return cached
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    async with _SEMAPHORE:
        started = time.monotonic()
        response = await client().messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        )
        seconds = time.monotonic() - started
    text = next((b.text for b in response.content if b.type == "text"), "{}")
    usage = response.usage.model_dump() if response.usage else {}
    usage = {k: v for k, v in usage.items() if isinstance(v, int)}
    SPEND.add(model, usage, seconds)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        if max_tokens >= 32000:
            raise
        # Thinking used up the room and cut the answer: ask again with more room.
        return await ask_json(model, system, user, schema, effort=effort, max_tokens=max_tokens * 4, tools=tools)
    result = {"data": data, "usage": usage, "seconds": seconds, "stop_reason": response.stop_reason}
    path.write_text(json.dumps(result))
    return result


async def ask_with_tools(
    model: str,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    effort: str = "low",
    max_tokens: int = 4000,
) -> dict[str, Any]:
    """One tool-enabled turn, cached on disk. Returns {"content", "usage", "seconds", "stop_reason"}."""
    request = {"model": model, "system": system, "messages": messages, "tools": tools, "effort": effort}
    key = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    path = CACHE_DIR / f"{key}.json"
    if path.exists():
        SPEND.cached_calls += 1
        return json.loads(path.read_text())
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    async with _SEMAPHORE:
        started = time.monotonic()
        response = await client().messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            output_config={"effort": effort},
            **({"tools": tools} if tools else {}),
        )
        seconds = time.monotonic() - started
    usage = response.usage.model_dump() if response.usage else {}
    usage = {k: v for k, v in usage.items() if isinstance(v, int)}
    SPEND.add(model, usage, seconds)
    # Thinking blocks go back unchanged with the tool results, as the API requires.
    content = [b.model_dump(exclude_none=True) for b in response.content]
    result = {"content": content, "usage": usage, "seconds": seconds, "stop_reason": response.stop_reason}
    path.write_text(json.dumps(result))
    return result


# --------------------------------------------------------------------------- #
# Files


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, default=_default) + "\n" for r in rows))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _default(value: Any) -> Any:
    if isinstance(value, Item):
        return asdict(value)
    raise TypeError(type(value))


def write_result(name: str, data: dict[str, Any]) -> Path:
    """Aggregate numbers only: these files go into the repository."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"{name}.json"
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return path
