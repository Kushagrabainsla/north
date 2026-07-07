"""Unit tests for submission idempotency (orchestrator/idempotency.py)."""

from __future__ import annotations

import time

from ledger.models import LedgerSource
from orchestrator.idempotency import IdempotencyCache, idempotency_key
from orchestrator.models import TaskRequest


def test_explicit_key_is_used_verbatim() -> None:
    req = TaskRequest(prompt="hi", source=LedgerSource.WEBHOOK, idempotency_key="delivery-123")
    assert idempotency_key(req) == "delivery-123"


def test_derived_key_is_stable_for_same_source_and_prompt() -> None:
    a = TaskRequest(prompt="same", source=LedgerSource.PROMPT)
    b = TaskRequest(prompt="same", source=LedgerSource.PROMPT)
    assert idempotency_key(a) == idempotency_key(b)
    assert idempotency_key(a).startswith("auto:")


def test_derived_key_differs_by_prompt_and_source() -> None:
    base = TaskRequest(prompt="p", source=LedgerSource.PROMPT)
    other_prompt = TaskRequest(prompt="q", source=LedgerSource.PROMPT)
    other_source = TaskRequest(prompt="p", source=LedgerSource.CRON)
    assert idempotency_key(base) != idempotency_key(other_prompt)
    assert idempotency_key(base) != idempotency_key(other_source)


def test_cache_returns_put_value_within_ttl() -> None:
    cache = IdempotencyCache(ttl_seconds=10)
    cache.put("k", "task_1")
    assert cache.get("k") == "task_1"


def test_cache_returns_none_for_unknown_key() -> None:
    assert IdempotencyCache(ttl_seconds=10).get("missing") is None


def test_cache_evicts_after_ttl() -> None:
    cache = IdempotencyCache(ttl_seconds=0.05)
    cache.put("k", "task_1")
    time.sleep(0.08)
    assert cache.get("k") is None
