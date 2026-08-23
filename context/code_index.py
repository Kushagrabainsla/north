"""Semantic code index for grounding engineering agents (#2 code RAG).

Complements the repo map (structure) with *search by meaning*: the coder can ask
"where do we validate config" or "the retry/backoff logic" and get the actual
functions back, ranked by semantic similarity, even when it doesn't know the
exact identifiers to grep for.

Design (mirrors north's other embedding indexes - EmbeddingIndex, ToolIndex):
- Symbol-aware chunking: Python is split per function / method / class-card via
  the AST; other languages fall back to overlapping line windows.
- Incremental: each file's content hash is stored, so only changed files are
  re-embedded on the next search. The first search over a workspace pays the
  full embedding cost; later searches are cheap.
- Bounded: a capped, shallowest-first set of source files is indexed, chunks are
  size-capped, and embeddings are batched, so a large repo can't explode cost.
- Fails soft: any embedding error yields an empty result so the caller (the
  search_code tool) can fall back to grep/search_symbols.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from context.repo_map import _collect_source_files
from utils.db import open_db_connection
from utils.math import cosine_similarity

logger = logging.getLogger(__name__)

EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS code_chunks (
    workspace  TEXT    NOT NULL,
    path       TEXT    NOT NULL,
    start_line INTEGER NOT NULL,
    end_line   INTEGER NOT NULL,
    symbol     TEXT    NOT NULL,
    chunk_text TEXT    NOT NULL,
    embedding  TEXT    NOT NULL,
    PRIMARY KEY (workspace, path, start_line)
);
CREATE TABLE IF NOT EXISTS code_files (
    workspace    TEXT NOT NULL,
    path         TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    PRIMARY KEY (workspace, path)
);
"""

_MAX_FILES: int = 300  # shallowest-first cap on files indexed per workspace
_MAX_CHUNK_CHARS: int = 2000  # cap embedded/stored text per chunk
_MAX_CHUNKS_PER_FILE: int = 60
_EMBED_BATCH: int = 64  # texts per embed call (provider-agnostic safe batch)
_WINDOW_LINES: int = 45  # line-window size for non-Python files
_WINDOW_OVERLAP: int = 10


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _python_chunks(text: str) -> list[tuple[int, int, str, str]]:
    """(start_line, end_line, symbol, code) per top-level function, method, and class-card."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    out: list[tuple[int, int, str, str]] = []

    def _seg(node: ast.AST, symbol: str) -> None:
        seg = ast.get_source_segment(text, node)
        if not seg:
            return
        start = getattr(node, "lineno", 1)
        end = getattr(node, "end_lineno", start)
        out.append((start, end, symbol, seg[:_MAX_CHUNK_CHARS]))

    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            _seg(node, f"def {node.name}")
        elif isinstance(node, ast.ClassDef):
            doc = ast.get_docstring(node) or ""
            methods = [m.name for m in node.body if isinstance(m, ast.AsyncFunctionDef | ast.FunctionDef)]
            card = f"class {node.name}\n{doc}".strip()
            if methods:
                card += "\nmethods: " + ", ".join(methods)
            start = getattr(node, "lineno", 1)
            out.append((start, start, f"class {node.name}", card[:_MAX_CHUNK_CHARS]))
            for m in node.body:
                if isinstance(m, ast.AsyncFunctionDef | ast.FunctionDef):
                    _seg(m, f"{node.name}.{m.name}")
    return out


def _window_chunks(text: str) -> list[tuple[int, int, str, str]]:
    """Overlapping line windows for languages without an AST parser here."""
    lines = text.splitlines()
    if not lines:
        return []
    out: list[tuple[int, int, str, str]] = []
    step = max(1, _WINDOW_LINES - _WINDOW_OVERLAP)
    for start in range(0, len(lines), step):
        window = lines[start : start + _WINDOW_LINES]
        body = "\n".join(window).strip()
        if body:
            out.append((start + 1, start + len(window), "", body[:_MAX_CHUNK_CHARS]))
        if start + _WINDOW_LINES >= len(lines):
            break
    return out


def _chunk_file(rel: str, text: str) -> list[tuple[int, int, str, str]]:
    chunks = _python_chunks(text) if rel.endswith(".py") else _window_chunks(text)
    return chunks[:_MAX_CHUNKS_PER_FILE]


class CodeIndex:
    """Embeds source chunks per workspace for semantic retrieval; incremental by file hash."""

    def __init__(self, db_path: Path, embed_fn: EmbedFn) -> None:
        self._db_path = db_path
        self._embed_fn = embed_fn
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with open_db_connection(self._db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA)
        # workspace -> parsed [(path, start, end, symbol, text, vec)], invalidated on update.
        self._cache: dict[str, list[tuple[str, int, int, str, str, list[float]]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, workspace: str) -> asyncio.Lock:
        return self._locks.setdefault(workspace, asyncio.Lock())

    async def search(self, workspace: str, query: str, max_results: int = 5) -> list[tuple[str, int, int, str, str]]:
        """Return up to *max_results* ``(path, start, end, symbol, code)`` by similarity.

        Refreshes the index for *workspace* first (only changed files re-embed).
        Returns [] on any embedding error so the caller can fall back to grep.
        """
        ws = str(Path(workspace).resolve())
        await self._ensure_fresh(ws)
        try:
            q_embs = await self._embed_fn([query])
        except Exception:
            return []
        if not q_embs:
            return []
        qvec = q_embs[0]

        rows = self._cache.get(ws)
        if rows is None:
            rows = await asyncio.to_thread(self._load_workspace_sync, ws)
            self._cache[ws] = rows

        scored: list[tuple[float, str, int, int, str, str]] = []
        for path, start, end, symbol, chunk, emb in rows:
            if len(emb) != len(qvec):
                continue
            scored.append((cosine_similarity(qvec, emb), path, start, end, symbol, chunk))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [(p, s, e, sym, c) for _, p, s, e, sym, c in scored[:max_results]]

    async def _ensure_fresh(self, ws: str) -> None:
        """Re-embed changed files and drop deleted ones for the resolved workspace *ws*."""
        async with self._lock_for(ws):
            root = Path(ws)
            if not root.is_dir():
                return
            stored = await asyncio.to_thread(self._stored_hashes_sync, ws)
            removed, to_process = await asyncio.to_thread(self._scan_files_sync, root, stored)

            if removed:
                await asyncio.to_thread(self._delete_files_sync, ws, removed)
                self._cache.pop(ws, None)

            changed_any = False
            files_to_embed = []
            flat_texts = []

            for rel, _text, digest, chunks in to_process:
                if not chunks:
                    await asyncio.to_thread(self._upsert_file_sync, ws, rel, digest, [])
                    changed_any = True
                else:
                    files_to_embed.append((rel, digest, chunks))
                    for c in chunks:
                        flat_texts.append(f"{rel}\n{c[3]}")

            if flat_texts:
                all_embedded = await self._embed_batch_flat(flat_texts)
                if all_embedded is not None and len(all_embedded) == len(flat_texts):
                    offset = 0
                    for rel, digest, chunks in files_to_embed:
                        count = len(chunks)
                        file_embedded = all_embedded[offset : offset + count]
                        offset += count
                        rows = [
                            (start, end, symbol, chunk_text, json.dumps(vec))
                            for (start, end, symbol, chunk_text), vec in zip(chunks, file_embedded, strict=False)
                        ]
                        await asyncio.to_thread(self._upsert_file_sync, ws, rel, digest, rows)
                        changed_any = True

            if changed_any:
                self._cache.pop(ws, None)

    def _scan_files_sync(
        self, root: Path, stored: dict[str, str]
    ) -> tuple[list[str], list[tuple[str, str, str, list[tuple[int, int, str, str]]]]]:
        """Scan workspace files, return (removed_paths, [(rel, text, digest, chunks)])."""
        files = _collect_source_files(root, _MAX_FILES)
        current = {rel for rel, _ in files}
        removed = sorted(set(stored) - current)
        to_embed = []
        for rel, path in files:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            digest = _hash(text)
            if stored.get(rel) == digest:
                continue
            chunks = _chunk_file(rel, text)
            to_embed.append((rel, text, digest, chunks))
        return removed, to_embed

    async def _embed_batch_flat(self, texts: list[str]) -> list[list[float]] | None:
        """Embed all chunk texts in batches of _EMBED_BATCH."""
        out: list[list[float]] = []
        for i in range(0, len(texts), _EMBED_BATCH):
            batch = texts[i : i + _EMBED_BATCH]
            try:
                embs = await self._embed_fn(batch)
            except Exception:
                logger.warning("CodeIndex: embed batch failed (%d texts) - not indexed", len(batch))
                return None
            if len(embs) != len(batch):
                return None
            out.extend(embs)
        return out

    # ------------------------------------------------------------------ #

    def _stored_hashes_sync(self, ws: str) -> dict[str, str]:
        with open_db_connection(self._db_path) as conn:
            rows = conn.execute("SELECT path, content_hash FROM code_files WHERE workspace = ?", (ws,)).fetchall()
        return {r["path"]: r["content_hash"] for r in rows}

    def _delete_files_sync(self, ws: str, paths: list[str]) -> None:
        with open_db_connection(self._db_path) as conn:
            for path in paths:
                conn.execute("DELETE FROM code_chunks WHERE workspace = ? AND path = ?", (ws, path))
                conn.execute("DELETE FROM code_files WHERE workspace = ? AND path = ?", (ws, path))

    def _upsert_file_sync(self, ws: str, rel: str, digest: str, rows: list[tuple[int, int, str, str, str]]) -> None:
        with open_db_connection(self._db_path) as conn:
            conn.execute("DELETE FROM code_chunks WHERE workspace = ? AND path = ?", (ws, rel))
            if rows:
                conn.executemany(
                    "INSERT INTO code_chunks "
                    "(workspace, path, start_line, end_line, symbol, chunk_text, embedding) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [(ws, rel, s, e, sym, txt, emb) for (s, e, sym, txt, emb) in rows],
                )
            conn.execute(
                "INSERT INTO code_files (workspace, path, content_hash) VALUES (?, ?, ?) "
                "ON CONFLICT(workspace, path) DO UPDATE SET content_hash=excluded.content_hash",
                (ws, rel, digest),
            )

    def _load_workspace_sync(self, ws: str) -> list[tuple[str, int, int, str, str, list[float]]]:
        with open_db_connection(self._db_path) as conn:
            rows = conn.execute(
                "SELECT path, start_line, end_line, symbol, chunk_text, embedding FROM code_chunks WHERE workspace = ?",
                (ws,),
            ).fetchall()
        out: list[tuple[str, int, int, str, str, list[float]]] = []
        for r in rows:
            try:
                emb = json.loads(r["embedding"])
            except (json.JSONDecodeError, TypeError):
                continue
            out.append((r["path"], r["start_line"], r["end_line"], r["symbol"], r["chunk_text"], emb))
        return out
