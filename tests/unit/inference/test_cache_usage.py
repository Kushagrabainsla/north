"""Reading cache usage back out of each provider's reply.

Every provider reports the same fact under a different key, and a cache that
silently stops working shows up nowhere else - so each shape is pinned here.
"""

from __future__ import annotations

import pytest

from inference.usage import cache_tokens


class TestProviderShapes:
    def test_openai_compatible(self) -> None:
        """OpenRouter, Groq, Zen and Gemini all report it here."""
        assert cache_tokens({"prompt_tokens_details": {"cached_tokens": 128}}) == (128, 0)

    def test_openai_responses_api(self) -> None:
        """The shape the Codex subscription backend returns."""
        usage = {"input_tokens_details": {"cached_tokens": 1840, "cache_write_tokens": 32}}
        assert cache_tokens(usage) == (1840, 32)

    def test_anthropic(self) -> None:
        usage = {"cache_read_input_tokens": 900, "cache_creation_input_tokens": 64}
        assert cache_tokens(usage) == (900, 64)

    def test_a_provider_that_says_nothing_reports_zero(self) -> None:
        """Silence is not a cache miss, but it is all we can honestly report."""
        assert cache_tokens({"prompt_tokens": 2885, "completion_tokens": 12}) == (0, 0)

    @pytest.mark.parametrize("usage", [None, {}, {"prompt_tokens_details": None}, "nonsense"])
    def test_junk_never_raises(self, usage) -> None:
        assert cache_tokens(usage) == (0, 0)

    def test_a_float_count_is_accepted(self) -> None:
        assert cache_tokens({"prompt_tokens_details": {"cached_tokens": 128.0}}) == (128, 0)


class TestItReachesTheLedger:
    """The number is only useful if it survives all the way to where it is read."""

    @pytest.mark.asyncio
    async def test_a_provider_reply_carries_cache_counts_through(self, tmp_path) -> None:
        import httpx

        from inference.models import CompletionRequest
        from inference.providers.openai_compat import OpenAICompatibleProvider

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "model": "m",
                    "choices": [{"message": {"content": "hi"}}],
                    "usage": {
                        "prompt_tokens": 2000,
                        "completion_tokens": 5,
                        "prompt_tokens_details": {"cached_tokens": 1800},
                    },
                },
            )

        client = httpx.AsyncClient(base_url="https://example.test", transport=httpx.MockTransport(handler))
        provider = OpenAICompatibleProvider(name="test", base_url="https://example.test", api_key="k")
        provider._client = client  # noqa: SLF001
        response = await provider.complete("m", CompletionRequest(prompt="x", component="test"))
        await client.aclose()

        assert response.tokens_in == 2000
        assert response.cached_tokens == 1800

    @pytest.mark.asyncio
    async def test_an_old_ledger_gains_the_column_without_losing_rows(self, tmp_path) -> None:
        import sqlite3

        from ledger.models import LedgerEntry, LedgerSource, LedgerStatus
        from ledger.sqlite_writer import _SCHEMA, SQLiteLedgerWriter

        db = tmp_path / "ledger.db"
        with sqlite3.connect(db) as conn:
            conn.executescript(_SCHEMA.replace("    cached_tokens   INTEGER,\n", ""))
            conn.execute(
                "INSERT INTO ledger (id, timestamp, source, action, status)"
                " VALUES ('old', '2026-09-01', 'system', 'pre-existing', 'completed')"
            )

        await SQLiteLedgerWriter(db).write(
            LedgerEntry.new(
                source=LedgerSource.SYSTEM,
                action="new",
                status=LedgerStatus.COMPLETED,
                tokens_in=100,
                cached_tokens=80,
            )
        )

        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT id FROM ledger WHERE id='old'").fetchone() is not None
            row = conn.execute("SELECT tokens_in, cached_tokens FROM ledger WHERE action='new'").fetchone()
        assert row == (100, 80)
