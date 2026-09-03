"""``~/.north/models.db`` - the durable store behind model routing.

Why persist at all: north keeps working when a refresh fails or the machine is
offline, because stale facts beat no facts. ``updated_at`` on every row makes
that staleness visible rather than silent.

Note this is *not* ``facts.db``, which belongs to user-memory facts and is
unrelated. Routing never reads this store on the hot path - a chain changes only
when the catalog does, so the DB is the restart/offline cache and the in-memory
snapshot is what every inference call walks.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from inference.facts.models import FACT_FIELDS, Endpoint, Entitlement, Fact, ModelFacts, Rank
from utils.db import open_db_connection

logger = logging.getLogger(__name__)

# Fields stored as JSON because their value is a set, not a scalar.
_JSON_FIELDS = frozenset({"input_modalities"})
_BOOL_FIELDS = frozenset({"supports_tools", "supports_reasoning", "supports_structured"})


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ModelFactsStore:
    """Reads and writes model facts, endpoints and routing decisions."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _ensure_schema(self) -> None:
        with open_db_connection(self._db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS model_facts ("
                "  canonical_id        TEXT PRIMARY KEY,"
                "  context_window      INTEGER,"
                "  max_output_tokens   INTEGER,"
                "  supports_tools      INTEGER,"
                "  supports_reasoning  INTEGER,"
                "  supports_structured INTEGER,"
                "  input_modalities    TEXT,"
                "  coding_score        REAL,"
                "  agentic_score       REAL,"
                "  intelligence_score  REAL,"
                "  provenance          TEXT NOT NULL,"
                "  updated_at          TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS endpoints ("
                "  canonical_id      TEXT NOT NULL,"
                "  provider          TEXT NOT NULL,"
                "  provider_model_id TEXT NOT NULL,"
                "  price_in          REAL,"
                "  price_out         REAL,"
                "  quantization      TEXT,"
                "  max_payload_chars INTEGER,"
                "  entitlement       TEXT NOT NULL DEFAULT 'UNKNOWN',"
                "  uptime            REAL,"
                "  context_window    INTEGER,"
                "  updated_at        TEXT NOT NULL,"
                "  PRIMARY KEY (canonical_id, provider, provider_model_id))"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_endpoints_model ON endpoints (canonical_id)")

    # ---- model_facts ----

    def upsert_facts(self, records: Iterable[ModelFacts]) -> int:
        """Replace the stored record for each canonical model. Returns rows written."""
        rows = [self._facts_to_row(record) for record in records]
        if not rows:
            return 0
        columns = ("canonical_id", *FACT_FIELDS, "provenance", "updated_at")
        placeholders = ", ".join("?" for _ in columns)
        with open_db_connection(self._db_path) as conn:
            conn.executemany(
                f"INSERT OR REPLACE INTO model_facts ({', '.join(columns)}) VALUES ({placeholders})",
                rows,
            )
        return len(rows)

    def load_facts(self) -> dict[str, ModelFacts]:
        """Every stored fact record, rebuilt with its provenance."""
        with open_db_connection(self._db_path) as conn:
            rows = conn.execute("SELECT * FROM model_facts").fetchall()
        return {row["canonical_id"]: self._row_to_facts(row) for row in rows}

    @staticmethod
    def _facts_to_row(record: ModelFacts) -> tuple:
        values: list[object] = [record.canonical_id]
        for field in FACT_FIELDS:
            value = record.value(field)
            if value is None:
                values.append(None)
            elif field in _JSON_FIELDS:
                values.append(json.dumps(sorted(value)))
            elif field in _BOOL_FIELDS:
                values.append(1 if value else 0)
            else:
                values.append(value)
        values.append(json.dumps(record.provenance()))
        values.append(_now())
        return tuple(values)

    @staticmethod
    def _row_to_facts(row) -> ModelFacts:
        try:
            provenance = json.loads(row["provenance"]) or {}
        except (ValueError, TypeError):
            provenance = {}
        record = ModelFacts(canonical_id=row["canonical_id"])
        for field in FACT_FIELDS:
            raw = row[field]
            if raw is None:
                continue
            if field in _JSON_FIELDS:
                try:
                    value = frozenset(json.loads(raw))
                except (ValueError, TypeError):
                    continue
            elif field in _BOOL_FIELDS:
                value = bool(raw)
            else:
                value = raw
            meta = provenance.get(field) or {}
            rank = Rank[meta["rank"]] if meta.get("rank") in Rank.__members__ else Rank.DECLARED
            when = _parse_time(meta.get("fetched_at")) or _parse_time(row["updated_at"]) or datetime.now(UTC)
            record = record.with_fact(
                field,
                Fact(value=value, rank=rank, source=str(meta.get("source") or "stored"), fetched_at=when),
            )
        return record

    # ---- endpoints ----

    def upsert_endpoints(self, rows: Iterable[Endpoint]) -> int:
        """Write endpoint rows, preserving each row's learned entitlement.

        Entitlement is knowledge about the *account*, learned from live calls; a
        catalog refresh knows nothing about it and must not reset it.
        """
        values = [
            (
                row.canonical_id,
                row.provider,
                row.provider_model_id,
                row.price_in,
                row.price_out,
                row.quantization,
                row.max_payload_chars,
                row.entitlement.value,
                row.uptime,
                row.context_window,
                _now(),
            )
            for row in rows
        ]
        if not values:
            return 0
        with open_db_connection(self._db_path) as conn:
            conn.executemany(
                "INSERT INTO endpoints (canonical_id, provider, provider_model_id, price_in, price_out,"
                " quantization, max_payload_chars, entitlement, uptime, context_window, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (canonical_id, provider, provider_model_id) DO UPDATE SET"
                "   price_in=excluded.price_in, price_out=excluded.price_out,"
                "   quantization=excluded.quantization, max_payload_chars=excluded.max_payload_chars,"
                "   uptime=excluded.uptime, context_window=excluded.context_window,"
                "   updated_at=excluded.updated_at",
                values,
            )
        return len(values)

    def load_endpoints(self) -> list[Endpoint]:
        with open_db_connection(self._db_path) as conn:
            rows = conn.execute("SELECT * FROM endpoints").fetchall()
        return [
            Endpoint(
                canonical_id=row["canonical_id"],
                provider=row["provider"],
                provider_model_id=row["provider_model_id"],
                price_in=row["price_in"],
                price_out=row["price_out"],
                quantization=row["quantization"],
                max_payload_chars=row["max_payload_chars"],
                entitlement=(
                    Entitlement(row["entitlement"])
                    if row["entitlement"] in Entitlement.__members__
                    else Entitlement.UNKNOWN
                ),
                uptime=row["uptime"],
                context_window=row["context_window"],
            )
            for row in rows
        ]

    def set_entitlement(self, keys: Iterable[tuple[str, str, str]], entitlement: Entitlement) -> int:
        """Record what a live call proved about the account's access to these endpoints."""
        values = [(entitlement.value, _now(), *key) for key in keys]
        if not values:
            return 0
        with open_db_connection(self._db_path) as conn:
            conn.executemany(
                "UPDATE endpoints SET entitlement = ?, updated_at = ?"
                " WHERE canonical_id = ? AND provider = ? AND provider_model_id = ?",
                values,
            )
        return len(values)

    def prune_missing_endpoints(self, providers: Iterable[str], live_keys: set[tuple[str, str, str]]) -> int:
        """Drop endpoint rows for *providers* that the live catalog no longer lists.

        Only for providers that actually answered this refresh - a provider whose
        fetch failed keeps its rows, which is the whole point of persisting them.
        """
        removed = 0
        provider_names = list(providers)
        if not provider_names:
            return 0
        with open_db_connection(self._db_path) as conn:
            stored = conn.execute(
                "SELECT canonical_id, provider, provider_model_id FROM endpoints WHERE provider IN"
                f" ({', '.join('?' for _ in provider_names)})",
                provider_names,
            ).fetchall()
            stale = [tuple(row) for row in stored if tuple(row) not in live_keys]
            if stale:
                conn.executemany(
                    "DELETE FROM endpoints WHERE canonical_id = ? AND provider = ? AND provider_model_id = ?",
                    stale,
                )
                removed = len(stale)
        return removed


def _parse_time(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
