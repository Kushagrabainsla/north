"""Your keys and passwords, which north's tools use by name (CODING_STYLE §7.6).

A custom tool had no place for a key: the guide told the model to stop and tell the
user. Now the user saves it on the dashboard under a name, a tool declares the names
it uses (shown on its approval card), and reads the value inside its own `run`. North
gives a tool only what it declared, and hides every stored value from what any tool
returns, so the model knows a secret by its name only, as Meta's Muse keeps its agent
apart from the user's passwords.
"""

from __future__ import annotations

import stat

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from config import secret_store
from config.secret_store import SecretUnavailable, secret
from config.settings import settings
from orchestrator.api_context import ApiServices, bind_services
from tests.unit.tools.test_tool_drafts import _GOOD, _block, _create, _guide
from tools.base import Tool
from tools.models import ToolInput, ToolOutput
from tools.registry import LearnedTool, ToolRegistry
from tools.universal._draft_checks import draft_problems
from tools.universal.create_tool import CreateToolTool
from tools.universal.read_file import ReadFileTool
from web import secrets as secrets_routes

KEY = "owm-1234-abcd-5678"


class _Forecast(Tool):
    name = "forecast"
    description = "the weather"
    uses_secrets = ("openweather",)

    async def run(self, input: ToolInput) -> ToolOutput:
        key = secret("openweather")
        return ToolOutput(success=True, data={"url": f"https://api.example.com/?appid={key}", "length": len(key)})


class _Snooping(Tool):
    name = "snooping"
    description = "reads a secret it never declared"

    async def run(self, input: ToolInput) -> ToolOutput:
        return ToolOutput(success=True, data={"key": secret("openweather")})


class _Failing(_Forecast):
    name = "failing"

    async def run(self, input: ToolInput) -> ToolOutput:
        raise RuntimeError(f"401 Unauthorized for https://api.example.com/?appid={secret('openweather')}")


@pytest.fixture
def saved() -> secret_store.SecretStore:
    store = secret_store.store()
    store.save("openweather", KEY, note="weather for get_forecast")
    return store


async def test_a_tool_gets_the_secret_it_declares_and_the_model_sees_only_its_name(saved) -> None:
    out = await _Forecast().execute(ToolInput(params={}))

    assert out.success
    assert out.data == {"url": "https://api.example.com/?appid=[secret openweather]", "length": len(KEY)}


async def test_a_tool_that_did_not_declare_a_secret_does_not_get_it(saved) -> None:
    out = await _Snooping().execute(ToolInput(params={}))

    assert not out.success
    assert "did not declare the secret 'openweather'" in out.error and KEY not in out.error


async def test_a_secret_that_is_not_saved_says_where_the_user_adds_it() -> None:
    out = await _Forecast().execute(ToolInput(params={}))

    assert not out.success
    assert "no secret named 'openweather'" in out.error and "Settings, Secrets" in out.error


async def test_an_error_that_holds_the_value_reaches_the_model_without_it(saved) -> None:
    out = await _Failing().execute(ToolInput(params={}))

    assert not out.success
    assert out.error == "RuntimeError: 401 Unauthorized for https://api.example.com/?appid=[secret openweather]"


async def test_any_tool_that_returns_a_stored_value_returns_its_name_instead(saved, tmp_path) -> None:
    project_env = tmp_path / "project" / ".env"
    project_env.parent.mkdir()
    project_env.write_text(f"OPENWEATHER_KEY={KEY}\n", encoding="utf-8")

    out = await ReadFileTool().execute(ToolInput(params={"path": str(project_env)}))

    assert out.success, out.error
    assert KEY not in str(out.data) and "[secret openweather]" in str(out.data)


def test_code_outside_a_tool_gets_no_secret(saved) -> None:
    with pytest.raises(SecretUnavailable, match="code outside a tool"):
        secret("openweather")


async def test_the_store_is_private_lists_no_value_and_file_tools_cannot_read_it(saved) -> None:
    path = settings.north_home / secret_store.FILE_NAME

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    [listed] = saved.saved()
    assert (listed.name, listed.note) == ("openweather", "weather for get_forecast") and KEY not in repr(listed)
    out = await ReadFileTool().execute(ToolInput(params={"path": str(path)}))
    assert not out.success and KEY not in str(out.model_dump())


def test_a_name_or_value_north_cannot_keep_is_refused_with_why() -> None:
    assert "lowercase" in secret_store.unusable("Open Weather", KEY)
    assert "at least 8" in secret_store.unusable("openweather", "short")
    assert secret_store.unusable("openweather", KEY) == ""


async def test_a_tool_north_wrote_gets_its_declared_secrets_and_its_card_names_them(saved) -> None:
    learned = LearnedTool(_Forecast())

    card = await learned.describe(ToolInput(params={}))

    assert learned.uses_secrets == ("openweather",)
    assert "It uses your secrets: openweather." in card.message


# ── Writing a tool that needs a key ─────────────────────────────────────────

_KEYED = (
    _GOOD.replace("from tools.base import Tool", "from config.secret_store import secret\nfrom tools.base import Tool")
    .replace('    description = "demo"', '    description = "demo"\n    uses_secrets = ("openweather",)')
    .replace(
        'city = input.params.get("city", "Pune")',
        'city = input.params.get("city", "Pune")\n        key = secret("openweather")',
    )
)


async def test_a_draft_that_needs_a_key_not_yet_saved_asks_the_user_to_add_it(tmp_path) -> None:
    secret_store.store().save("github", "ghp-not-the-one-it-needs")

    out, registry, _ = await _create(tmp_path, _KEYED)

    assert not out.success and "learned_demo" not in registry.tools
    assert "secret 'openweather', which is not saved yet" in out.error
    assert "Settings, Secrets" in out.error and "never ask for its value in chat" in out.error
    assert "Saved secrets: github." in out.error


async def test_once_the_key_is_saved_the_same_draft_goes_live_and_its_card_names_the_secret(saved, tmp_path) -> None:
    learned = tmp_path / "learned" / "tools"
    card = await CreateToolTool(tool_registry=None, tools_dir=learned).describe(
        ToolInput(params={"action": "create", "content": _KEYED, "test_params": {}})
    )

    out, registry, _ = await _create(tmp_path, _KEYED)

    assert "It uses your secrets: openweather." in card.message
    assert out.success, out.error
    assert "learned_demo" in registry.tools


@pytest.mark.parametrize(
    ("draft", "fix"),
    [
        (_KEYED.replace('    uses_secrets = ("openweather",)\n', ""), "add `uses_secrets = ('openweather',)`"),
        (_KEYED.replace('secret("openweather")', "secret(input.params['which'])"), "name it with a string"),
        (_KEYED.replace('uses_secrets = ("openweather",)', 'uses_secrets = ["openweather", 3]'), "tuple of the"),
        (
            _GOOD.replace('"Pune")', 'os.environ["KEY"])').replace("from tools", "import os\nfrom tools", 1),
            "secret('name')",
        ),
    ],
)
def test_a_draft_that_reads_a_key_the_wrong_way_comes_back_with_the_fix(draft, fix) -> None:
    problems = draft_problems(draft, saved_secrets={"openweather"})

    assert any(fix in problem for problem in problems), problems


def test_the_tool_guides_key_example_passes_the_checks() -> None:
    example = _block(_guide("adding-a-north-tool"), "python", "uses_secrets")

    assert draft_problems(example, saved_secrets={"openweather"}) == []


# ── The dashboard's Secrets panel ───────────────────────────────────────────


def _services(tmp_path) -> ApiServices:
    registry = ToolRegistry(auto_register=False)
    registry.register(_Forecast())
    registry.register(_Snooping())
    return ApiServices(north_home=tmp_path, tool_registry=registry)


async def test_the_panel_lists_names_and_users_and_never_a_value(tmp_path) -> None:
    with bind_services(_services(tmp_path)):
        assert (await secrets_routes.list_secrets())["missing"] == [{"name": "openweather", "used_by": ["forecast"]}]
        await secrets_routes.save_secret("openweather", secrets_routes.SecretUpdate(value=KEY, note="weather"))
        listed = await secrets_routes.list_secrets()

    assert listed["missing"] == []
    [entry] = listed["secrets"]
    assert entry["name"] == "openweather" and entry["used_by"] == ["forecast"] and entry["note"] == "weather"
    assert KEY not in str(listed) and "value" not in entry


async def test_the_panel_refuses_a_bad_name_and_says_when_there_is_nothing_to_remove(tmp_path) -> None:
    with bind_services(_services(tmp_path)):
        with pytest.raises(HTTPException) as bad:
            await secrets_routes.save_secret("Open Weather", secrets_routes.SecretUpdate(value=KEY))
        with pytest.raises(HTTPException) as gone:
            await secrets_routes.remove_secret("openweather")
        with pytest.raises(ValidationError):
            secrets_routes.SecretUpdate(value=KEY, extra="no")

    assert bad.value.status_code == 400 and "lowercase" in bad.value.detail
    assert gone.value.status_code == 404


async def test_listing_tools_names_the_saved_secrets_and_never_a_value(saved, tmp_path) -> None:
    """In a real run the model had no way to know which secrets were saved before it wrote a tool."""
    tool = CreateToolTool(tool_registry=None, tools_dir=tmp_path / "learned" / "tools")

    out = await tool.execute(ToolInput(params={"action": "list"}))

    assert out.data["secrets"] == [{"name": "openweather", "note": "weather for get_forecast"}]
    assert "Saved secrets (by name; a tool reads one with secret(name)): openweather - weather for get_forecast" in (
        tool.format_output(out.data)
    )
    assert KEY not in str(out.model_dump())
