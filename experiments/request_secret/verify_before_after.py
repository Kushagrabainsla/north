"""Before and after for request_secret: run one checkout's own code on the same three cases.

Usage (from any directory):
    uv run --project <checkout> python <this file> <checkout> <label>

On main, north gets a secret the way its guides say: an ask_user handoff (requires_user_action)
that sends the user to Settings, Secrets. On the branch it calls request_secret. Each case uses
the checkout's own agent, approval store, policy and orchestrator, with a throwaway north home.

Cases:
  dashboard  the user does what the card asks, on the dashboard
  reply      the user types the password as the answer (Telegram, the terminal, the chat)
  yolo       the approval mode is yolo, and the user is away

The results hold counts and booleans only.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

CHECKOUT = Path(sys.argv[1]).resolve()
LABEL = sys.argv[2]
HOME = Path(tempfile.mkdtemp(prefix="north-verify-"))
os.environ["NORTH_HOME"] = str(HOME)
sys.path.insert(0, str(CHECKOUT))

from agents.models import AgentPayload  # noqa: E402
from approval.store import ApprovalStore  # noqa: E402
from config import secret_store  # noqa: E402
from config.approval_mode import ApprovalMode  # noqa: E402
from config.settings import settings  # noqa: E402
from tests.conftest import StubDecider  # noqa: E402
from tests.unit.agents.test_autonomous_ask_user import _agent  # noqa: E402
from tests.unit.approval.test_overrule import _orchestrator  # noqa: E402

settings.north_home = HOME
PASSWORD = "verify-pass-8c41d2"
NAME = "kasa_password"
WHY = "The kasa_lights tool logs in to your TP-Link account."
HAS_TOOL = hasattr(_agent(HOME, ApprovalMode.ASK, StubDecider(None), ApprovalStore()), "_request_secret")


async def _ask(agent) -> str:
    payload = AgentPayload(task_id="t1", prompt="Add my Kasa lights")
    if HAS_TOOL:
        return await agent._request_secret(payload, {"why": WHY, "secrets": [{"name": NAME}]})
    question = f"{WHY} Add it on the dashboard under Settings, Secrets, named {NAME}, then choose Done."
    return await agent._ask_user(payload, {"question": question, "options": ["Done"], "requires_user_action": True})


async def _pending(store: ApprovalStore):
    for _ in range(100):
        if store.pending():
            return store.pending()[0]
        await asyncio.sleep(0)
    return None


async def case(name: str) -> dict:
    secret_store.store().remove(NAME)
    db = HOME / f"{name}.db"
    store = ApprovalStore(db)
    mode = ApprovalMode.YOLO if name == "yolo" else ApprovalMode.ASK
    agent = _agent(HOME, mode, StubDecider(None), store)
    orch = _orchestrator(store)
    asking = asyncio.create_task(_ask(agent))
    card = await _pending(store)
    user_actions = 0
    if card is not None and name == "dashboard":
        if HAS_TOOL:
            # Type the value on the card, press Save.
            await orch.respond_approval(card.id, "answered", "Done", values={NAME: PASSWORD})
            user_actions = 2
        else:
            # Open Settings, open Secrets, type the name, type the value, save, come back, press Done.
            secret_store.store().save(NAME, PASSWORD)
            await orch.respond_approval(card.id, "answered", "Done")
            user_actions = 7
    elif card is not None and name == "reply":
        await orch.respond_approval(card.id, "answered", PASSWORD)
        user_actions = 1
    elif card is not None and name == "yolo":
        await asyncio.sleep(0.05)
    try:
        result = await asyncio.wait_for(asking, timeout=1)
    except TimeoutError:
        result = ""
    finished = bool(result)
    ledger_text = " ".join(str(call) for call in orch._ledger.write.call_args_list)
    return {
        "case": name,
        "card_waits_for_user": card is not None and not finished,
        "task_went_on": finished,
        "task_told_secret_saved": finished and bool(json.loads(result).get("success")),
        "secret_saved": secret_store.store().value(NAME) == PASSWORD,
        "user_actions": user_actions,
        "value_reached_model": PASSWORD in result,
        # SQLite keeps new rows in the -wal file until a checkpoint, so read every file of the store.
        "value_in_card_store": any(PASSWORD.encode() in part.read_bytes() for part in HOME.glob(f"{name}.db*")),
        "value_in_ledger": PASSWORD in ledger_text,
    }


async def main() -> None:
    results = [await case(name) for name in ("dashboard", "reply", "yolo")]
    out = Path(__file__).parent / "results" / f"verify_{LABEL}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"checkout": LABEL, "request_secret": HAS_TOOL, "cases": results}, indent=1) + "\n")
    print(json.dumps(results, indent=1))


asyncio.run(main())
