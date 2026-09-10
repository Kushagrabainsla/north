"""Regression tests for the shared runtime inference-router construction.

Stage 3 collapsed three copies of the concrete ``build_router(...)`` call
(initial production build, ``web/api.py`` credential refresh, and the
``north_config`` set reload) onto the platform configuration boundary in
``config.runtime``:

* ``build_inference_router_from_settings`` — maps the live ``settings``
  singleton onto ``build_router``.
* ``rebuild_runtime_router`` — the reload convenience that pulls
  ``north_settings`` and ``confidence_tracker`` off a live ``Dependencies``
  and deliberately omits the models DB.

These tests pin the argument mapping and the surrounding reload behaviour
(``inference_router`` reassignment, ``cost_tracker.set_inner``, app ``merge``,
and the tool's provider summary) so the de-duplication cannot silently drift.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import inference.runtime as runtime_mod
from inference.runtime import build_inference_router_from_settings, rebuild_runtime_router


class _FakeRouter:
    def __init__(self, providers=()):  # noqa: ANN001
        self._providers = list(providers)


@pytest.fixture
def captured_build_router(monkeypatch):
    """Replace ``build_router`` in config.dependencies and capture its kwargs."""
    calls: list[dict] = []

    def _fake(**kwargs):  # noqa: ANN003
        calls.append(kwargs)
        return _FakeRouter(providers=kwargs.get("_providers", ()))

    monkeypatch.setattr(runtime_mod, "build_router", _fake)
    return calls


def test_from_settings_maps_all_credentials_and_models_db(monkeypatch, captured_build_router):
    fake_settings = SimpleNamespace(
        openrouter_api_key="or-key",
        groq_api_key="groq-key",
        gemini_api_key="gem-key",
        opencode_zen_api_key="zen-key",
        north_home=Path("/tmp/north-home"),
    )
    monkeypatch.setattr(runtime_mod, "settings", fake_settings)

    north_settings = object()
    confidence_tracker = object()
    models_db = Path("/tmp/north-home/models.db")

    build_inference_router_from_settings(
        north_settings=north_settings,
        confidence_tracker=confidence_tracker,
        models_db_path=models_db,
    )

    assert len(captured_build_router) == 1
    kwargs = captured_build_router[0]
    assert kwargs["openrouter_api_key"] == "or-key"
    assert kwargs["groq_api_key"] == "groq-key"
    assert kwargs["gemini_api_key"] == "gem-key"
    assert kwargs["opencode_zen_api_key"] == "zen-key"
    assert kwargs["provider_settings"] is fake_settings
    assert kwargs["north_settings"] is north_settings
    assert kwargs["confidence_tracker"] is confidence_tracker
    assert kwargs["cooldowns_path"] == Path("/tmp/north-home/cooldowns.json")
    assert kwargs["models_db_path"] == models_db


def test_from_settings_defaults_models_db_to_none(monkeypatch, captured_build_router):
    fake_settings = SimpleNamespace(
        openrouter_api_key="",
        groq_api_key="",
        gemini_api_key="",
        opencode_zen_api_key="",
        north_home=Path("/tmp/north-home"),
    )
    monkeypatch.setattr(runtime_mod, "settings", fake_settings)

    build_inference_router_from_settings(
        north_settings=object(),
        confidence_tracker=object(),
    )

    assert captured_build_router[0]["models_db_path"] is None


def test_rebuild_runtime_router_omits_models_db_and_reads_deps(monkeypatch, captured_build_router):
    """Reload rebuilds must NOT re-open the models DB (pre-Stage-3 behaviour)."""
    fake_settings = SimpleNamespace(
        openrouter_api_key="or",
        groq_api_key="",
        gemini_api_key="",
        opencode_zen_api_key="",
        north_home=Path("/tmp/north-home"),
    )
    monkeypatch.setattr(runtime_mod, "settings", fake_settings)

    north_settings = object()
    confidence_tracker = object()
    deps = SimpleNamespace(north_settings=north_settings, confidence_tracker=confidence_tracker)

    rebuild_runtime_router(deps)

    kwargs = captured_build_router[0]
    assert kwargs["models_db_path"] is None
    assert kwargs["north_settings"] is north_settings
    assert kwargs["confidence_tracker"] is confidence_tracker
    assert kwargs["cooldowns_path"] == Path("/tmp/north-home/cooldowns.json")


@pytest.mark.asyncio
async def test_web_refresh_swaps_router_container_and_merges(monkeypatch):
    """``_refresh_inference_runtime`` reloads, reassigns, set_inner, and merge."""
    from web import api as web_api

    new_router = _FakeRouter()
    cost_tracker = MagicMock()
    deps = SimpleNamespace(
        inference_router=_FakeRouter(),
        cost_tracker=cost_tracker,
        north_settings=object(),
        confidence_tracker=object(),
    )

    reloaded = {"called": False}

    def _reload():
        reloaded["called"] = True

    monkeypatch.setattr("config.settings.reload_settings", _reload)
    monkeypatch.setattr("config.runtime.get_runtime", lambda: deps)
    monkeypatch.setattr(runtime_mod, "rebuild_runtime_router", lambda d: new_router)

    merged: list[tuple] = []
    monkeypatch.setattr(web_api, "merge", lambda app, **kw: merged.append((app, kw)))

    app = object()
    await web_api._refresh_inference_runtime(app)

    assert reloaded["called"] is True
    assert deps.inference_router is new_router
    cost_tracker.set_inner.assert_called_once_with(new_router)
    assert merged == [(app, {"inference_router": new_router})]


@pytest.mark.asyncio
async def test_web_refresh_noop_when_runtime_absent(monkeypatch):
    from web import api as web_api

    monkeypatch.setattr("config.settings.reload_settings", lambda: None)
    monkeypatch.setattr("config.runtime.get_runtime", lambda: None)
    called = {"rebuilt": False}
    monkeypatch.setattr(runtime_mod, "rebuild_runtime_router", lambda d: called.__setitem__("rebuilt", True))
    # Must return quietly without touching the router boundary.
    await web_api._refresh_inference_runtime(object())
    assert called["rebuilt"] is False


@pytest.mark.asyncio
async def test_north_config_apply_runtime_set_inner_and_summary(monkeypatch):
    """The tool reload swaps the router in the CostTracker and reports providers.

    It must NOT reassign ``deps.inference_router`` (only web/api does that).
    """
    from tools.specialized.north_config import _INFERENCE_KEYS, NorthConfigTool

    inference_key = next(iter(_INFERENCE_KEYS))

    new_router = _FakeRouter(providers=[SimpleNamespace(name="openrouter"), SimpleNamespace(name="groq")])
    cost_tracker = MagicMock()
    original_router = _FakeRouter()
    deps = SimpleNamespace(
        inference_router=original_router,
        cost_tracker=cost_tracker,
        north_settings=object(),
        confidence_tracker=object(),
    )

    monkeypatch.setattr("config.settings.reload_settings", lambda: None)
    monkeypatch.setattr("config.runtime.get_runtime", lambda: deps)
    monkeypatch.setattr(runtime_mod, "rebuild_runtime_router", lambda d: new_router)

    tool = NorthConfigTool()
    note = tool._apply_runtime(inference_key)

    cost_tracker.set_inner.assert_called_once_with(new_router)
    assert deps.inference_router is original_router  # tool does not reassign
    assert note == "\n✅ Inference reloaded — providers: openrouter, groq"


@pytest.mark.asyncio
async def test_north_config_apply_runtime_no_server(monkeypatch):
    from tools.specialized.north_config import _INFERENCE_KEYS, NorthConfigTool

    monkeypatch.setattr("config.settings.reload_settings", lambda: None)
    monkeypatch.setattr("config.runtime.get_runtime", lambda: None)

    tool = NorthConfigTool()
    note = tool._apply_runtime(next(iter(_INFERENCE_KEYS)))
    assert "not running as a server" in note


@pytest.mark.asyncio
async def test_north_config_apply_runtime_non_inference_key(monkeypatch):
    from tools.specialized.north_config import NorthConfigTool

    monkeypatch.setattr("config.settings.reload_settings", lambda: None)
    tool = NorthConfigTool()
    note = tool._apply_runtime("NORTH_SOME_NON_INFERENCE_KEY")
    assert note == "\n✅ Settings reloaded from disk."
