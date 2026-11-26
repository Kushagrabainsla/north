"""request_secret: north asks for a secret on a card, and the value goes straight to the secret store.

The model knows a secret by its name only (CODING_STYLE §7.6). Before this tool, the only way in was the
Secrets panel: north asked the user to go there, type the exact name and come back. A value typed as an
answer instead reached the model, the card store and the ledger. Now the card has a password field, the
dashboard saves the value under the name the card asks for, and nothing that keeps a card ever holds it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agents.agentic_llm_agent import _tool_schemas
from agents.models import AgentPayload
from agents.schemas import REQUEST_SECRET_SCHEMA
from approval.models import ApprovalDecision, Card, CardField, CardFieldType, CardType
from approval.store import ApprovalStore
from approval.telegram import TelegramNotifier
from config import secret_store
from config.approval_mode import ApprovalMode
from tests.conftest import StubDecider, deciding
from tests.unit.agents.test_autonomous_ask_user import _agent
from tests.unit.approval.test_overrule import _orchestrator
from utils.runtime_resources import builtin_skills_dir

PASSWORD = "hunter2-correct-horse"
WHY = "The kasa_lights tool logs in to your TP-Link account to reach your bulbs."


def _request(agent, secrets=None, **extra):
    params = {"why": WHY, "secrets": secrets or [{"name": "kasa_password", "label": "TP-Link password"}], **extra}
    return agent._request_secret(AgentPayload(task_id="t1", prompt="p"), params)


def _secret_card(*names: str) -> Card:
    return Card.new(
        type=CardType.QUESTION,
        agent="north",
        title="North - Secret needed",
        message=WHY,
        task_id="t1",
        options=["Done", "Not now"],
        fields=[CardField(name=name, label=name, type=CardFieldType.SECRET) for name in names],
        requires_user_action=True,
    )


def _on_disk(folder: Path) -> bytes:
    """Every byte the card store wrote, the -wal file included: SQLite keeps new rows there first."""
    return b"".join(part.read_bytes() for part in sorted(folder.glob("approvals.db*")))


async def _pending(store: ApprovalStore) -> Card:
    for _ in range(50):
        if store.pending():
            return store.pending()[0]
        await asyncio.sleep(0)
    raise AssertionError("no card was raised")


async def test_the_value_saved_on_the_card_reaches_the_store_and_the_model_sees_only_the_name(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path / "approvals.db")
    agent = _agent(tmp_path, ApprovalMode.ASK, StubDecider(None), store)
    orch = _orchestrator(store)

    asking = asyncio.create_task(_request(agent))
    card = await _pending(store)
    assert [(f.name, f.type) for f in card.fields] == [("kasa_password", CardFieldType.SECRET)]
    assert card.requires_user_action and card.message == WHY

    # What the dashboard's secret card sends: the typed value, by the card's own name.
    await orch.respond_approval(card.id, "answered", "Done", values={"kasa_password": PASSWORD})
    out = await asyncio.wait_for(asking, timeout=1)

    assert secret_store.store().value("kasa_password") == PASSWORD
    assert json.loads(out)["success"] is True and json.loads(out)["saved"] == ["kasa_password"]
    assert PASSWORD not in out
    [resolved] = store.all()
    assert resolved.chosen_option == "Saved"
    assert PASSWORD not in resolved.model_dump_json()
    assert PASSWORD.encode() not in _on_disk(tmp_path)
    orch._ledger.write.assert_not_called()


async def test_a_value_typed_as_the_answer_is_never_kept_anywhere(tmp_path: Path) -> None:
    """Telegram, the terminal and the chat send a reply as text; for a secret card the text is dropped."""
    store = ApprovalStore(tmp_path / "approvals.db")
    agent = _agent(tmp_path, ApprovalMode.ASK, StubDecider(None), store)
    orch = _orchestrator(store)

    asking = asyncio.create_task(_request(agent))
    card = await _pending(store)
    await orch.respond_approval(card.id, "answered", PASSWORD)
    out = await asyncio.wait_for(asking, timeout=1)

    assert PASSWORD not in out
    assert json.loads(out)["success"] is False, "a reply is not a saved secret"
    assert secret_store.store().value("kasa_password") is None
    [resolved] = store.all()
    assert resolved.chosen_option == "Not saved"
    assert PASSWORD not in resolved.model_dump_json()
    assert PASSWORD.encode() not in _on_disk(tmp_path)
    orch._ledger.write.assert_not_called()


async def test_done_after_saving_on_the_secrets_panel_counts_as_saved(tmp_path: Path) -> None:
    store = ApprovalStore()
    agent = _agent(tmp_path, ApprovalMode.ASK, StubDecider(None), store)

    asking = asyncio.create_task(_request(agent))
    card = await _pending(store)
    secret_store.store().save("kasa_password", PASSWORD)
    await _orchestrator(store).respond_approval(card.id, "answered", "Done")

    assert json.loads(await asyncio.wait_for(asking, timeout=1))["success"] is True


async def test_not_now_tells_the_model_what_to_do_instead(tmp_path: Path) -> None:
    store = ApprovalStore()
    agent = _agent(tmp_path, ApprovalMode.ASK, StubDecider(None), store)

    asking = asyncio.create_task(_request(agent))
    card = await _pending(store)
    await _orchestrator(store).respond_approval(card.id, "answered", "Not now")
    out = json.loads(await asyncio.wait_for(asking, timeout=1))

    assert out["success"] is False and out["not_saved"] == ["kasa_password"]
    assert "never ask for its value in chat" in out["error"].lower()


@pytest.mark.parametrize("mode", [ApprovalMode.AUTONOMOUS, ApprovalMode.YOLO])
async def test_no_mode_answers_a_secret_card_for_the_user(tmp_path: Path, mode: ApprovalMode) -> None:
    store = ApprovalStore()
    decider = deciding(ApprovalDecision.ANSWERED, "Done")
    agent = _agent(tmp_path, mode, decider, store)

    asking = asyncio.create_task(_request(agent))
    try:
        card = await _pending(store)
        assert card.status == "pending"
        assert not decider.asked
    finally:
        asking.cancel()
        await asyncio.gather(asking, return_exceptions=True)


async def test_a_saved_secret_is_not_asked_for_again(tmp_path: Path) -> None:
    store = ApprovalStore()
    secret_store.store().save("kasa_password", PASSWORD)
    agent = _agent(tmp_path, ApprovalMode.ASK, StubDecider(None), store)

    out = json.loads(await _request(agent))

    assert out["success"] is True and out["already_saved"] == ["kasa_password"]
    assert not store.all()


async def test_replace_asks_again_and_needs_a_new_value(tmp_path: Path) -> None:
    store = ApprovalStore()
    secret_store.store().save("kasa_password", PASSWORD)
    agent = _agent(tmp_path, ApprovalMode.ASK, StubDecider(None), store)

    asking = asyncio.create_task(_request(agent, replace=True))
    card = await _pending(store)
    await _orchestrator(store).respond_approval(card.id, "answered", "Done")
    out = json.loads(await asyncio.wait_for(asking, timeout=1))

    assert out["success"] is False, "Done without a new value is not a replaced secret"


async def test_two_secrets_are_asked_on_one_card(tmp_path: Path) -> None:
    store = ApprovalStore()
    agent = _agent(tmp_path, ApprovalMode.ASK, StubDecider(None), store)
    names = [{"name": "kasa_email", "label": "TP-Link email"}, {"name": "kasa_password", "label": "TP-Link password"}]

    asking = asyncio.create_task(_request(agent, secrets=names))
    card = await _pending(store)
    typed = {"kasa_email": "me@example.com", "kasa_password": PASSWORD}
    await _orchestrator(store).respond_approval(card.id, "answered", "Done", values=typed)

    assert json.loads(await asyncio.wait_for(asking, timeout=1))["saved"] == ["kasa_email", "kasa_password"]


@pytest.mark.parametrize(
    "secrets",
    [[], [{"name": "Kasa Password"}], [{"name": f"s{i}"} for i in range(5)], "kasa_password"],
)
async def test_bad_requests_raise_no_card(tmp_path: Path, secrets) -> None:
    store = ApprovalStore()
    agent = _agent(tmp_path, ApprovalMode.ASK, StubDecider(None), store)

    params = {"why": WHY, "secrets": secrets}
    out = json.loads(await agent._request_secret(AgentPayload(task_id="t1", prompt="p"), params))

    assert out["success"] is False
    assert not store.all()


async def test_a_request_needs_a_reason(tmp_path: Path) -> None:
    store = ApprovalStore()
    agent = _agent(tmp_path, ApprovalMode.ASK, StubDecider(None), store)

    out = json.loads(await _request(agent, why=" "))

    assert out["success"] is False and not store.all()


async def test_saving_checks_the_card_and_the_value() -> None:
    store = ApprovalStore()
    card = _secret_card("kasa_password")
    plain = Card.new(type=CardType.QUESTION, agent="north", title="q", message="Which DB?", task_id="t1")
    store.add(card)
    store.add(plain)
    orch = _orchestrator(store)

    with pytest.raises(ValueError, match="at least"):
        await orch.respond_approval(card.id, "answered", "Done", values={"kasa_password": "short"})
    with pytest.raises(ValueError, match="does not ask"):
        await orch.respond_approval(card.id, "answered", "Done", values={"other_name": PASSWORD})
    with pytest.raises(ValueError, match="does not ask for a secret"):
        await orch.save_secret_answer(plain.id, {"kasa_password": PASSWORD})
    with pytest.raises(LookupError):
        await orch.save_secret_answer("no-such-card", {"kasa_password": PASSWORD})

    assert store.get(card.id).status == "pending"
    assert secret_store.store().value("kasa_password") is None


def test_telegram_sends_the_user_to_the_dashboard_and_asks_for_no_value() -> None:
    text = TelegramNotifier._render(_secret_card("kasa_password"))

    assert WHY in text
    assert "kasa_password" in text
    assert "do not send" in text.lower()
    assert "Reply with your answer" not in text and "reply with your own" not in text


def test_the_terminal_says_where_to_type_it_and_asks_for_no_value() -> None:
    from cli.tui import _question_text

    card = _secret_card("kasa_password")
    text = _question_text({"question": card.message, "fields": [f.model_dump(mode="json") for f in card.fields]})

    assert "saved as kasa_password" in text
    assert "on the dashboard" in text and "not here" in text
    assert "separated by ;" not in text


def test_the_tool_is_offered_only_where_the_run_may_ask() -> None:
    names = {schema["function"]["name"] for schema in _tool_schemas({})}
    quiet = {schema["function"]["name"] for schema in _tool_schemas({}, allow_questions=False)}

    assert "request_secret" in names and "request_secret" not in quiet
    assert REQUEST_SECRET_SCHEMA["function"]["parameters"]["required"] == ["secrets", "why"]


def test_every_place_that_mentions_a_missing_secret_points_to_request_secret() -> None:
    from config.secret_store import SecretUnavailable, released_to, secret

    with released_to("kasa_lights", ("kasa_password",)), pytest.raises(SecretUnavailable) as missing:
        secret("kasa_password")
    guide = (builtin_skills_dir() / "adding-a-north-tool" / "SKILL.md").read_text(encoding="utf-8")

    assert "request_secret" in str(missing.value)
    assert "request_secret" in guide
