"""What this install has measured about how fast each model actually is.

Routing ranked on quality and price alone. On a live install two free models
averaged over two minutes per agent run and kept winning on price, because
nothing anywhere recorded how long a model took. These tests cover the signal
that fixes that: how a rate is measured, when a model counts as slow, and the
cases where calling one slow would be wrong.
"""

from __future__ import annotations

import pytest

from inference.constants import (
    _MODEL_SPEED_FLOOR_TOK_PER_SEC,
    _MODEL_SPEED_MIN_SAMPLES,
)
from inference.dispatcher import ModelDispatcher
from tools.confidence import ConfidenceTracker


@pytest.fixture
def dispatcher(tmp_path) -> ModelDispatcher:
    return ModelDispatcher(providers=[], cooldowns_path=tmp_path / "cd.json")


def _observe(dispatcher: ModelDispatcher, model: str, *, tok_per_sec: float, times: int) -> None:
    """Feed *times* identical observations at the given rate."""
    for _ in range(times):
        dispatcher._record_chain_speed(model, "openrouter", seconds=1.0, tokens_out=int(tok_per_sec))


class TestMeasuringARate:
    def test_speed_is_tokens_per_second_not_wall_clock(self, dispatcher) -> None:
        """A long answer legitimately takes longer.

        Ranking on raw duration would punish a model for being asked a bigger
        question, so what is recorded is the rate it generated at.
        """
        dispatcher._record_chain_speed("fast", "openrouter", seconds=10.0, tokens_out=1000)
        dispatcher._record_chain_speed("slow", "openrouter", seconds=10.0, tokens_out=20)

        fast_rate, _ = dispatcher._model_speed[("fast", "openrouter")]
        slow_rate, _ = dispatcher._model_speed[("slow", "openrouter")]
        assert fast_rate == pytest.approx(100.0)
        assert slow_rate == pytest.approx(2.0)

    def test_the_first_observation_is_taken_at_face_value(self, dispatcher) -> None:
        """Seeding the EMA with a default would blend a real measurement with a
        number nobody measured, and take several calls to shake off."""
        dispatcher._record_chain_speed("m", "openrouter", seconds=2.0, tokens_out=100)

        rate, samples = dispatcher._model_speed[("m", "openrouter")]
        assert rate == pytest.approx(50.0)
        assert samples == 1

    def test_a_rate_moves_toward_later_observations(self, dispatcher) -> None:
        _observe(dispatcher, "m", tok_per_sec=100, times=1)
        _observe(dispatcher, "m", tok_per_sec=1, times=6)

        rate, samples = dispatcher._model_speed[("m", "openrouter")]
        assert samples == 7
        assert rate < 30.0, "a model that turned slow must be seen to have turned slow"

    @pytest.mark.parametrize(
        "seconds,tokens_out",
        [(1.0, 0), (0.0, 100), (1.0, -5), (-1.0, 100)],
        ids=["no tokens", "no elapsed time", "negative tokens", "negative time"],
    )
    def test_a_call_with_no_measurable_rate_is_ignored(self, dispatcher, seconds, tokens_out) -> None:
        """An embedding or an empty reply carries no rate. Recording it as zero
        would tail a model for doing work that has no token output at all."""
        dispatcher._record_chain_speed("m", "openrouter", seconds=seconds, tokens_out=tokens_out)

        assert ("m", "openrouter") not in dispatcher._model_speed


class TestDecidingAModelIsSlow:
    def test_a_model_measured_below_the_floor_is_slow(self, dispatcher) -> None:
        _observe(dispatcher, "crawler", tok_per_sec=1, times=_MODEL_SPEED_MIN_SAMPLES)

        assert dispatcher._is_slow("crawler") is True

    def test_a_model_above_the_floor_is_not(self, dispatcher) -> None:
        _observe(dispatcher, "quick", tok_per_sec=60, times=_MODEL_SPEED_MIN_SAMPLES)

        assert dispatcher._is_slow("quick") is False

    def test_a_model_nobody_has_timed_is_never_slow(self, dispatcher) -> None:
        """An untried model must not be tailed before it has ever run - that
        would freeze the chain at whatever happened to be measured first."""
        assert dispatcher._is_slow("never-run") is False

    def test_one_slow_reading_is_not_enough(self, dispatcher) -> None:
        """A single slow call is a busy minute, not a slow model."""
        _observe(dispatcher, "unlucky", tok_per_sec=1, times=_MODEL_SPEED_MIN_SAMPLES - 1)

        assert dispatcher._is_slow("unlucky") is False

    def test_a_model_that_is_quick_on_any_endpoint_is_not_slow(self, dispatcher) -> None:
        """Slowness belongs to an endpoint, not to a model. If one provider
        serves it quickly, the model is not the problem and must keep its rank."""
        for _ in range(_MODEL_SPEED_MIN_SAMPLES):
            dispatcher._record_chain_speed("m", "slowhost", seconds=1.0, tokens_out=1)
            dispatcher._record_chain_speed("m", "fasthost", seconds=1.0, tokens_out=90)

        assert dispatcher._is_slow("m") is False

    def test_the_floor_is_where_the_constant_says(self, dispatcher) -> None:
        _observe(dispatcher, "just-under", tok_per_sec=_MODEL_SPEED_FLOOR_TOK_PER_SEC - 1, times=5)
        _observe(dispatcher, "just-over", tok_per_sec=_MODEL_SPEED_FLOOR_TOK_PER_SEC + 1, times=5)

        assert dispatcher._is_slow("just-under") is True
        assert dispatcher._is_slow("just-over") is False

    def test_slowness_is_looked_up_by_canonical_id(self, dispatcher) -> None:
        """Endpoints record provider-specific ids; the chain asks in canonical
        form. Comparing the two raw would silently never match."""
        _observe(dispatcher, "z-ai/GLM_5.2:free", tok_per_sec=1, times=_MODEL_SPEED_MIN_SAMPLES)

        assert dispatcher._is_slow("glm-5-2") is True


class TestSurvivingARestart:
    """Speed is only useful if it outlives the process that measured it."""

    def test_a_measured_speed_is_persisted_and_read_back(self, tmp_path) -> None:
        tracker = ConfidenceTracker(db_path=tmp_path / "tools.db")
        tracker._save_model_scores_batch_sync([("m", "openrouter", 0.9, 12, 3.5, 4)])

        assert tracker.load_model_speeds_sync() == {("m", "openrouter"): (3.5, 4)}

    def test_a_model_nobody_timed_is_absent_not_zero(self, tmp_path) -> None:
        """Loading it as 0 tok/s would make every untimed model look unusable."""
        tracker = ConfidenceTracker(db_path=tmp_path / "tools.db")
        tracker._save_model_scores_batch_sync([("m", "openrouter", 0.9, 12, 0.0, 0)])

        assert tracker.load_model_speeds_sync() == {}
        assert tracker.load_model_scores_sync() == {("m", "openrouter"): (0.9, 12)}

    def test_writing_reliability_alone_does_not_erase_speed(self, tmp_path) -> None:
        """The two share a row. A whole-row replace carrying only one of them
        would silently reset the other on the next dispatch outcome."""
        tracker = ConfidenceTracker(db_path=tmp_path / "tools.db")
        tracker._save_model_scores_batch_sync([("m", "openrouter", 0.9, 12, 3.5, 4)])

        tracker._save_model_score_sync("m", "openrouter", 0.4, 13)

        assert tracker.load_model_speeds_sync() == {("m", "openrouter"): (3.5, 4)}
        assert tracker.load_model_scores_sync() == {("m", "openrouter"): (0.4, 13)}

    def test_a_dispatcher_starts_from_what_was_measured_before(self, tmp_path) -> None:
        tracker = ConfidenceTracker(db_path=tmp_path / "tools.db")
        tracker._save_model_scores_batch_sync(
            [("crawler", "openrouter", 0.9, 12, 1.0, _MODEL_SPEED_MIN_SAMPLES)]
        )

        dispatcher = ModelDispatcher(
            providers=[], cooldowns_path=tmp_path / "cd.json", confidence_tracker=tracker
        )

        assert dispatcher._is_slow("crawler") is True, "a restart must not forget a slow model"

    def test_an_existing_database_gains_the_columns(self, tmp_path) -> None:
        """The migration runs against a table created before speed existed."""
        import sqlite3

        db = tmp_path / "tools.db"
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE model_confidence ("
                "model_id TEXT NOT NULL, provider TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 0.5,"
                "uses_total INTEGER NOT NULL DEFAULT 0, last_updated DATETIME NOT NULL,"
                "PRIMARY KEY (model_id, provider))"
            )
            conn.execute(
                "INSERT INTO model_confidence VALUES ('legacy', 'openrouter', 0.7, 9, '2026-01-01')"
            )

        tracker = ConfidenceTracker(db_path=db)

        assert tracker.load_model_scores_sync() == {("legacy", "openrouter"): (0.7, 9)}
        assert tracker.load_model_speeds_sync() == {}, "an existing row starts untimed, not slow"
