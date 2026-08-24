"""Precise, provider-aware rate-limit status for the inference router.

The dispatcher already applies a per-(model, provider) *cooldown* when a call is
rate limited or billing is exhausted (see ``inference/cooldowns.py``). That store
only keeps "unavailable until <monotonic time>" - it discards *why* (429 vs 402
vs auth), *who* (provider + model), the tier (free vs paid), and - most
importantly - the provider's OWN reset signal.

This module captures that signal so north can answer, precisely:

    "model ``google/gemini-flash`` on ``gemini`` is rate-limited (free tier,
     limit 60/min, remaining 0). It told us to wait via ``retry-after: 12``,
     so it will be usable again at 14:03:21 (in 12s)."

It parses every reset hint providers actually send - not just the generic
``Retry-After``:

    * ``Retry-After``                 OpenAI / Gemini / Groq / OpenRouter (429/503)
    * ``retry-after-ms``              OpenAI
    * ``X-RateLimit-Reset``           OpenRouter (epoch-s/epoch-ms, in the error
                                      *body* under ``metadata.headers``)
    * ``x-ratelimit-reset-requests``  Groq (e.g. ``"1.2s"`` / ``"120ms"``)
    * ``x-ratelimit-reset-tokens``    Groq (token-rate window)

The soonest positive signal wins, so north retries as early as the provider
allows. Records are persisted to disk so ``north limits`` works even when the
server is down, and so the data survives restarts.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.box import ROUNDED
from rich.table import Table
from rich.text import Text

logger = logging.getLogger(__name__)


# Kind of unavailability - drives how long the window is and how it's shown.
_RATE_LIMIT = "rate_limit"
_PAYMENT_REQUIRED = "payment_required"
_PROVIDER_AUTH = "provider_auth"
_PROVIDER_DOWN = "provider_down"
_PROVIDER_ERROR = "error"  # transient hard failure (5xx, timeout, JSON parse, transcription)
_PAYLOAD_TOO_LARGE = "payload_too_large"  # 413 - model can't accept north's request size

# Default functional windows (mirror cooldowns.py so the displayed ETA matches
# the actual skip window when no provider hint is present).
_DEFAULT_RATE_LIMIT_SECS = 60.0
_MAX_RATE_LIMIT_SECS = 600.0
_PAYMENT_EXHAUSTED_SECS = 86_400.0
_PROVIDER_DOWN_SECS = 86_400.0
# 413 is permanent for the current prompt but may clear if the provider raises
# its limit or north shrinks its payload - use a moderate window, not 24h.
_PAYLOAD_TOO_LARGE_SECS = 3_600.0

# Epoch thresholds used to tell ms apart from seconds in X-RateLimit-Reset.
_MS_EPOCH_FLOOR = 1_000_000_000_000  # 2001-09 in ms
_S_EPOCH_FLOOR = 1_000_000_000  # 2001-09 in s


@dataclass
class RateLimitRecord:
    """One (provider, model) availability record with a precise reset time."""

    provider: str
    model: str
    kind: str  # _RATE_LIMIT | _PAYMENT_REQUIRED | _PROVIDER_AUTH | _PROVIDER_DOWN
    wait_seconds: float  # precise wait the provider implied (0 if unknown)
    source: str  # "retry-after" / "x-ratelimit-reset" / "payment" / "auth" / "default"
    reason: str  # human-readable one-liner
    is_free: bool | None = None  # tier: free / paid / unknown
    limit: float | None = None  # requests-per-window if the provider told us
    remaining: float | None = None  # remaining in the current window
    available_at_epoch: float = 0.0  # wall-clock secs when usable again
    first_seen_epoch: float = 0.0
    last_seen_epoch: float = 0.0
    hits: int = 1

    # ---- derived display helpers (not persisted) ----
    @property
    def available_at_local(self) -> str:
        if self.available_at_epoch <= 0:
            return "unknown"
        return datetime.fromtimestamp(self.available_at_epoch, UTC).astimezone().strftime("%H:%M:%S")

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.available_at_epoch - time.time())

    @property
    def tier_label(self) -> str:
        if self.is_free is None:
            return "tier?"
        return "free" if self.is_free else "paid"

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "kind": self.kind,
            "wait_seconds": self.wait_seconds,
            "source": self.source,
            "reason": self.reason,
            "is_free": self.is_free,
            "limit": self.limit,
            "remaining": self.remaining,
            "available_at_epoch": self.available_at_epoch,
            "first_seen_epoch": self.first_seen_epoch,
            "last_seen_epoch": self.last_seen_epoch,
            "hits": self.hits,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RateLimitRecord:
        return cls(
            provider=data["provider"],
            model=data["model"],
            kind=data.get("kind", _RATE_LIMIT),
            wait_seconds=data.get("wait_seconds", 0.0),
            source=data.get("source", "unknown"),
            reason=data.get("reason", ""),
            is_free=data.get("is_free"),
            limit=data.get("limit"),
            remaining=data.get("remaining"),
            available_at_epoch=data.get("available_at_epoch", 0.0),
            first_seen_epoch=data.get("first_seen_epoch", 0.0),
            last_seen_epoch=data.get("last_seen_epoch", 0.0),
            hits=data.get("hits", 1),
        )


# (provider, model) or (provider, "") for provider-wide events (auth/down).
_Key = tuple[str, str]


def _parse_retry_after_from_value(value: str) -> float | None:
    """Parse a Retry-After header value: seconds (int) or HTTP-date."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        from datetime import UTC, datetime
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(value)
        return max(0.0, (when - datetime.now(when.tzinfo or UTC)).total_seconds())
    except Exception:
        return None


def _parse_groq_reset(value: str) -> float | None:
    """Parse a Groq reset value like ``"1.2s"`` or ``"120ms"`` → seconds."""
    value = (value or "").strip().lower()
    if not value:
        return None
    try:
        if value.endswith("ms"):
            return max(0.0, float(value[:-2]) / 1000.0)
        if value.endswith("s"):
            return max(0.0, float(value[:-1]))
        return max(0.0, float(value))  # bare seconds
    except ValueError:
        return None


def _parse_duration_seconds(value: str) -> float | None:
    """Parse a protobuf Duration string (e.g. ``"12s"``, ``"0.5s"``, ``"1500ms"``)."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        if value.endswith("ms"):
            return max(0.0, float(value[:-2]) / 1000.0)
        if value.endswith("s"):
            return max(0.0, float(value[:-1]))
        return max(0.0, float(value))
    except ValueError:
        return None


def _parse_epoch_reset(value: str) -> float | None:
    """Parse an ``X-RateLimit-Reset`` value (epoch seconds or epoch ms)."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        num = float(value)
    except ValueError:
        return None
    if num >= _MS_EPOCH_FLOOR:
        return max(0.0, num / 1000.0 - time.time())  # ms → relative wait
    if num >= _S_EPOCH_FLOOR:
        return max(0.0, num - time.time())  # s → relative wait
    return None  # too small to be an epoch; ignore (could be a relative count)


def compute_wait_seconds(
    *,
    status_code: int | None,
    headers: dict[str, str] | None,
    body: dict[str, Any] | None = None,
    retry_after: float | None = None,
    default: float = _DEFAULT_RATE_LIMIT_SECS,
) -> tuple[float, str]:
    """Return (wait_seconds, source) from every provider reset signal.

    ``wait_seconds`` is the *soonest* positive signal found (so north retries as
    early as allowed); ``source`` names which hint won. Falls back to
    ``default`` (labeled ``"default"``) when the provider sent nothing.
    """
    candidates: list[tuple[float, str]] = []

    def _consider(seconds: float | None, src: str) -> None:
        if seconds is not None and 0 < seconds <= _MAX_RATE_LIMIT_SECS:
            candidates.append((seconds, src))

    if retry_after is not None:
        _consider(retry_after, "retry-after")

    raw_headers = {k.lower(): v for k, v in (headers or {}).items()}

    # Plain Retry-After header (seconds or HTTP-date), the most common signal.
    if "retry-after" in raw_headers:
        _consider(_parse_retry_after_from_value(raw_headers["retry-after"]), "retry-after")

    # OpenAI: ``retry-after-ms`` (ms) takes precedence over ``retry-after``.
    ra_ms = raw_headers.get("retry-after-ms")
    if ra_ms:
        with contextlib.suppress(ValueError):
            _consider(max(0.0, float(ra_ms) / 1000.0), "retry-after-ms")

    # OpenRouter / generic X-RateLimit-Reset - can live in headers OR in the
    # error body under metadata.headers (OpenRouter puts it there as a string).
    for hdr_name in ("x-ratelimit-reset", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
        value = raw_headers.get(hdr_name)
        if not value:
            continue
        # Could be an epoch (reset time) or a Groq-style duration ("1.2s").
        _consider(_parse_epoch_reset(value), hdr_name)
        _consider(_parse_groq_reset(value), f"{hdr_name}(dur)")

    if isinstance(body, dict):
        meta = body.get("metadata") or body.get("error", {}).get("metadata") or {}
        meta_headers = meta.get("headers") if isinstance(meta, dict) else None
        if isinstance(meta_headers, dict):
            for k, v in meta_headers.items():
                lk = str(k).lower()
                if lk in ("x-ratelimit-reset", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
                    _consider(_parse_epoch_reset(str(v)), f"{lk}(body)")
                    _consider(_parse_groq_reset(str(v)), f"{lk}(body,dur)")
        # Google / Gemini RPC error: error.details[].retryDelay (Duration string).
        err = body.get("error") if isinstance(body, dict) else None
        details = err.get("details") if isinstance(err, dict) else None
        if isinstance(details, list):
            for detail in details:
                if isinstance(detail, dict) and detail.get("@type", "").endswith("RetryInfo"):
                    delay = detail.get("retryDelay")
                    if isinstance(delay, str):
                        _consider(_parse_duration_seconds(delay), "retryinfo(dur)")

    if not candidates:
        return default, "default"
    # Soonest positive wait wins - north retries as early as the provider allows.
    return min(candidates, key=lambda x: x[0])


# Debounce interval: multiple rapid events coalesce into one disk write.
_PERSIST_DEBOUNCE_SECONDS = 2.0


class RateLimitStatusStore:
    """In-memory records of (provider, model) availability, persisted to disk."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._records: dict[_Key, RateLimitRecord] = {}
        # Models successfully used this session. In-memory only (not persisted) so a
        # restart honestly reports un-tried models as "unknown" rather than "ok".
        self._checked: set[_Key] = set()
        # Serialises concurrent _persist_sync calls so the read-modify-write is atomic.
        self._persist_lock = threading.Lock()
        # Debounced flush: a pending asyncio.Task that writes after a delay.
        self._flush_task: asyncio.Task | None = None
        self._dirty = False

    # ---- load / persist ----

    def load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
        except Exception:
            logger.warning("Failed to load rate-limit status - starting fresh", exc_info=True)
            return
        now = time.time()
        loaded = 0
        for raw in data.get("records", []):
            try:
                rec = RateLimitRecord.from_dict(raw)
            except Exception:
                continue
            if rec.available_at_epoch <= now:
                continue  # expired - don't resurrect stale entries
            self._records[(rec.provider, rec.model)] = rec
            loaded += 1
        if loaded:
            logger.info("Loaded %d active rate-limit record(s) from disk", loaded)

    def _persist(self) -> None:
        """Schedule a debounced disk write.

        Multiple rapid events (e.g. 10 rate limits during a burst) coalesce into
        a single write after _PERSIST_DEBOUNCE_SECONDS, instead of one write per
        event.  The write is still dispatched to a thread pool to avoid blocking
        the event loop.
        """
        if self._path is None:
            return
        self._dirty = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Not in an async context (e.g. startup) — write immediately.
            self._persist_sync(list(self._records.values()))
            self._dirty = False
            return
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = loop.create_task(self._flush_after_delay())

    async def _flush_after_delay(self) -> None:
        """Wait then flush; coalesces rapid events into one write."""
        await asyncio.sleep(_PERSIST_DEBOUNCE_SECONDS)
        if self._dirty:
            snapshot = list(self._records.values())
            self._dirty = False
            await asyncio.get_running_loop().run_in_executor(
                None, self._persist_sync, snapshot
            )

    def flush_now(self) -> None:
        """Synchronous immediate flush (call at shutdown)."""
        if self._dirty and self._path is not None:
            self._persist_sync(list(self._records.values()))
            self._dirty = False

    def _persist_sync(self, records: list[RateLimitRecord] | None = None) -> None:
        if self._path is None:
            return
        with self._persist_lock:
            try:
                now = time.time()
                recs = records if records is not None else list(self._records.values())
                data = {
                    "records": [
                        r.to_dict()
                        for r in recs
                        if r.available_at_epoch > now
                    ]
                }
                tmp_path = self._path.with_suffix(".tmp")
                tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                tmp_path.replace(self._path)
            except Exception:
                logger.warning("Failed to persist rate-limit status", exc_info=True)

    # ---- recording ----

    def record_rate_limit(
        self,
        provider: str,
        model: str,
        *,
        status_code: int | None = None,
        headers: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        retry_after: float | None = None,
        is_free: bool | None = None,
        limit: float | None = None,
        remaining: float | None = None,
    ) -> RateLimitRecord:
        wait, source = compute_wait_seconds(
            status_code=status_code,
            headers=headers,
            body=body,
            retry_after=retry_after,
            default=_DEFAULT_RATE_LIMIT_SECS,
        )
        base_headers = {k.lower(): v for k, v in (headers or {}).items()}

        def _to_float(v: str | None) -> float | None:
            if not v:
                return None
            try:
                return float(v)
            except ValueError:
                return None

        # X-RateLimit-Limit / -Remaining (requests per window) when present.
        limit = limit if limit is not None else _to_float(base_headers.get("x-ratelimit-limit"))
        remaining = remaining if remaining is not None else _to_float(base_headers.get("x-ratelimit-remaining"))
        return self._upsert(
            provider,
            model,
            kind=_RATE_LIMIT,
            wait_seconds=wait,
            source=source,
            reason=f"rate limited ({status_code or '429'})",
            is_free=is_free,
            limit=limit,
            remaining=remaining,
        )

    def record_payment_required(self, provider: str, model: str, *, is_free: bool | None = None) -> RateLimitRecord:
        return self._upsert(
            provider,
            model,
            kind=_PAYMENT_REQUIRED,
            wait_seconds=_PAYMENT_EXHAUSTED_SECS,
            source="payment",
            reason="insufficient credits (402)",
            is_free=is_free,
        )

    def record_provider_down(self, provider: str, reason: str) -> RateLimitRecord:
        return self._upsert(
            provider,
            "",
            kind=_PROVIDER_DOWN,
            wait_seconds=_PROVIDER_DOWN_SECS,
            source="auth",
            reason=reason,
            is_free=None,
        )

    def record_error(
        self,
        provider: str,
        model: str,
        *,
        reason: str = "",
        is_free: bool | None = None,
    ) -> RateLimitRecord:
        """Record a transient hard failure (5xx, timeout, bad JSON, transcription).

        Unlike rate_limit/payment, this is a short 60s window: if the model recovers
        it clears on its own, and it re-records on the next failure. This makes
        generic InferenceError/TranscriptionError failures visible in ``north limits``
        instead of looking "all available".
        """
        return self._upsert(
            provider,
            model,
            kind=_PROVIDER_ERROR,
            wait_seconds=_DEFAULT_RATE_LIMIT_SECS,
            source="error",
            reason=reason or "inference error",
            is_free=is_free,
        )

    def record_payload_too_large(
        self, provider: str, model: str, *, reason: str = "", is_free: bool | None = None
    ) -> RateLimitRecord:
        """Record a 413 (request/payload too large) for a (provider, model).

        Distinct from rate_limit: a 413 won't clear with a Retry-After, so the
        model is skipped for a moderate window (1h) rather than hammered. Free-tier
        models with tiny request caps (e.g. Groq free) land here so north routes
        normal prompts to models that accept the payload size.
        """
        return self._upsert(
            provider,
            model,
            kind=_PAYLOAD_TOO_LARGE,
            wait_seconds=_PAYLOAD_TOO_LARGE_SECS,
            source="413",
            reason=reason or "payload too large (413)",
            is_free=is_free,
        )

    def mark_ok(self, provider: str, model: str) -> None:
        """Note that a model completed successfully this session (used for the
        'unknown vs verified-available' distinction). In-memory only."""
        self._checked.add((provider, model))

    def checked_count(self) -> int:
        return len(self._checked)

    def _upsert(
        self,
        provider: str,
        model: str,
        *,
        kind: str,
        wait_seconds: float,
        source: str,
        reason: str,
        is_free: bool | None,
        limit: float | None = None,
        remaining: float | None = None,
    ) -> RateLimitRecord:
        key: _Key = (provider, model)
        now = time.time()
        existing = self._records.get(key)
        if existing is not None and existing.kind == kind:
            # Refresh the window, bump the hit count, keep first-seen.
            existing.wait_seconds = wait_seconds
            existing.source = source
            existing.reason = reason
            existing.last_seen_epoch = now
            existing.hits += 1
            if is_free is not None:
                existing.is_free = is_free
            if limit is not None:
                existing.limit = limit
            if remaining is not None:
                existing.remaining = remaining
            existing.available_at_epoch = now + wait_seconds
            rec = existing
        else:
            rec = RateLimitRecord(
                provider=provider,
                model=model,
                kind=kind,
                wait_seconds=wait_seconds,
                source=source,
                reason=reason,
                is_free=is_free,
                limit=limit,
                remaining=remaining,
                available_at_epoch=now + wait_seconds,
                first_seen_epoch=now,
                last_seen_epoch=now,
            )
            self._records[key] = rec
        self._persist()
        return rec

    # ---- queries ----

    def is_active(self, provider: str, model: str) -> bool:
        rec = self._records.get((provider, model))
        if rec is None:
            # also check a provider-wide down record
            rec = self._records.get((provider, ""))
        return rec is not None and rec.available_at_epoch > time.time()

    def wait_for(self, provider: str, model: str) -> float:
        rec = self._records.get((provider, model)) or self._records.get((provider, ""))
        if rec is None:
            return 0.0
        return rec.remaining_seconds

    def snapshot(self) -> list[RateLimitRecord]:
        """All currently-active records, soonest-available first."""
        now = time.time()
        active = [r for r in self._records.values() if r.available_at_epoch > now]
        active.sort(key=lambda r: r.available_at_epoch)
        return active

    def clear(self) -> None:
        self._records.clear()
        self._persist()


# ── shared read-only access (CLI + Telegram) ─────────────────────────────────


def load_active_records(path: Path) -> list[RateLimitRecord]:
    """Read the on-disk status file and return only currently-active records.

    Works without an in-memory dispatcher (server may be offline).
    """
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except Exception:
        return []
    now = time.time()
    out: list[RateLimitRecord] = []
    for raw in data.get("records", []):
        try:
            rec = RateLimitRecord.from_dict(raw)
        except Exception:
            continue
        if rec.available_at_epoch > now:
            out.append(rec)
    out.sort(key=lambda r: r.available_at_epoch)
    return out


_KIND_LABEL = {
    "rate_limit": "RATE",
    "payment_required": "PAY",
    "provider_auth": "AUTH",
    "provider_down": "DOWN",
    "error": "ERR",
    "payload_too_large": "BIG",
}
_TIER_LABEL = {True: "free", False: "paid", None: "?"}


def _format_markdown(rec: RateLimitRecord) -> str:
    """One line per record, precise + tier-aware. Suitable for Telegram."""
    rem = rec.remaining_seconds
    if rec.available_at_epoch:
        when = datetime.fromtimestamp(rec.available_at_epoch, UTC).astimezone().strftime("%H:%M:%S")
    else:
        when = "?"
    wait = f"{rem:,.0f}s" if rem >= 1 else f"{rem * 1000:,.0f}ms"
    tier = _TIER_LABEL[rec.is_free]
    scope = rec.model or f"{rec.provider} (provider-wide)"
    limit_str = ""
    remaining = rec.remaining
    cap = rec.limit
    if remaining is not None and cap is not None:
        limit_str = f" — {int(remaining)}/{int(cap)} left"
    label = _KIND_LABEL.get(rec.kind, rec.kind)
    reason = f" — {rec.reason}" if rec.reason else ""
    return (
        f"*{label}* `{scope}` @{rec.provider} ({tier}){reason}\n"
        f"  back at {when} (in {wait}) via `{rec.source}`{limit_str}"
    )


def format_status_markdown(
    path: Path,
    *,
    checked: int | None = None,
    pool_total: int | None = None,
) -> str:
    """Render active rate-limit status as Markdown (used by ``north limits`` and Telegram).

    ``checked`` / ``pool_total`` (when provided) let the caller surface how many
    models have actually been probed this session vs how many exist in the pool, so
    un-tried models are shown as *unknown* rather than falsely "available".
    """
    records = load_active_records(path)
    if not records:
        if checked is not None and pool_total is not None and pool_total > 0:
            unverified = max(0, pool_total - checked)
            if unverified:
                return (
                    f"_no active cooldowns_ — {checked}/{pool_total} model(s) probed this session; "
                    f"{unverified} not yet tried (status unknown)"
                )
        return "_no active cooldowns recorded_ (no failures seen this session)"
    lines = [f"*rate-limit status* — {len(records)} unavailable", ""]
    lines.extend(_format_markdown(r) for r in records)
    return "\n".join(lines)


def format_status_table(
    path: Path,
    *,
    checked: int | None = None,
    pool_total: int | None = None,
) -> Table:
    """Render active rate-limit status as a Rich Table."""
    records = load_active_records(path)
    t = Table(title="Inference Rate Limits & Cooldowns", box=ROUNDED, header_style="bold cyan")
    t.add_column("Provider", style="cyan")
    t.add_column("Model / Scope", style="white")
    t.add_column("Tier", justify="center")
    t.add_column("Kind", justify="center")
    t.add_column("Reset ETA", style="dim")
    t.add_column("Remaining", justify="right")
    t.add_column("Signal Source / Reason", style="bright_black")

    if not records:
        if checked is not None and pool_total is not None and pool_total > 0:
            unverified = max(0, pool_total - checked)
            status_desc = f"{checked}/{pool_total} probed, {unverified} not yet tried"
        else:
            status_desc = "All model pools are operational"
        t.add_row(
            "All",
            "No active rate limits or cooldowns",
            Text("all", style="dim"),
            Text("READY", style="bold green"),
            "-",
            "-",
            status_desc,
        )
        return t

    for rec in records:
        rem = rec.remaining_seconds
        if rec.available_at_epoch:
            when = datetime.fromtimestamp(rec.available_at_epoch, UTC).astimezone().strftime("%H:%M:%S")
            wait = f"{rem:,.0f}s" if rem >= 1 else f"{rem * 1000:,.0f}ms"
            eta_str = f"{when} (in {wait})"
        else:
            eta_str = "unknown"

        tier_style = "cyan" if rec.is_free else "yellow" if rec.is_free is False else "dim"
        tier_text = Text(_TIER_LABEL.get(rec.is_free, "?"), style=tier_style)

        kind_code = _KIND_LABEL.get(rec.kind, rec.kind.upper())
        kind_style = "bold red" if rec.kind in ("rate_limit", "provider_auth", "provider_down") else "bold yellow"
        kind_text = Text(kind_code, style=kind_style)

        rem_val = rec.remaining
        cap_val = rec.limit
        if rem_val is not None and cap_val is not None:
            quota_str = f"{int(rem_val)}/{int(cap_val)}"
        elif rem_val is not None:
            quota_str = f"{int(rem_val)}"
        else:
            quota_str = "-"

        reason_str = f"via {rec.source}" + (f": {rec.reason}" if rec.reason else "")

        t.add_row(
            rec.provider,
            rec.model or "(provider-wide)",
            tier_text,
            kind_text,
            eta_str,
            quota_str,
            reason_str[:60],
        )

    return t
