"""Offline benchmark for the three gates the 2026-09-26 review found open.

Each case drives production code end to end - the real tool registry, the real
agent dispatch, the real approval store and `Orchestrator.respond_approval`, the
real Telegram gateway - and checks one observable outcome: did an action run,
was it asked about, was a stranger let in. Nothing here re-implements a gate.

Run:
    .venv/bin/python -m experiments.approval_integrity.benchmark --details
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from agents.agentic_llm_agent import AgenticLLMAgent
from agents.models import AgentPayload
from approval.approval_memory import ApprovalMemory
from approval.approvals import Approvals, Request
from approval.interaction import UserInteraction
from approval.models import Card, CardType
from approval.policy import Action, ActionKind, ApprovalPolicy, Verdict
from approval.store import ApprovalStore
from config.approval_mode import ApprovalMode
from config.settings import settings
from gateways import telegram as telegram_gateway
from inference.models import ToolCall
from orchestrator.orchestrator import Orchestrator

# Long enough for a pending card to be seen and answered. Cards never expire, so
# "nobody answers" is the benchmark giving up after _UNANSWERED, not the card.
_ANSWER_WINDOW = 5.0
_UNANSWERED = 0.3


# ── Approval replay ──────────────────────────────────────────────────────────


def _fake_orchestrator(store: ApprovalStore, memory: ApprovalMemory) -> SimpleNamespace:
    """The attributes `Orchestrator.respond_approval` reads, and nothing else."""
    return SimpleNamespace(
        _approval_store=store,
        _approval_memory=memory,
        _decision_log=None,
        _journal=SimpleNamespace(record=AsyncMock()),
    )


async def _answer_first_card(store: ApprovalStore, memory: ApprovalMemory, decision: str) -> None:
    """Wait for a card to surface, then answer it the way the web UI does."""
    deadline = time.monotonic() + _ANSWER_WINDOW
    while time.monotonic() < deadline:
        pending = store.pending()
        if pending:
            option = "Run" if decision == "approved" else ""
            await Orchestrator.respond_approval(_fake_orchestrator(store, memory), pending[0].id, decision, option)  # type: ignore[arg-type]
            return
        await asyncio.sleep(0.01)
    raise TimeoutError("no card surfaced")


def _bash_action(command: str) -> Action:
    return Action(agent="bash", kind=ActionKind.SHELL_COMMAND, summary=command, command=command)


async def _tool_card_replay(tmp: Path, decision: str, mode: ApprovalMode, second: str) -> str:
    """A tool's gate raises a card; the user answers; the same tool asks again."""
    store = ApprovalStore(tmp / "approvals.db")
    memory = ApprovalMemory(tmp / "memory.db")
    policy = ApprovalPolicy(mode_provider=lambda: mode, approval_memory=memory)
    approvals = Approvals(policy, UserInteraction(store))
    first = "make lint"
    request = Request(_bash_action(first), "Shell Command - Approval Required", f"```\n{first}\n```")
    await asyncio.gather(
        approvals.decide(request, task_id=None),
        _answer_first_card(store, memory, decision),
    )
    return (await policy.rule(_bash_action(second))).verdict.value


async def _direct_card_replay(tmp: Path) -> str:
    """An agent raises an approval card directly; the user approves; it is raised again."""
    store = ApprovalStore(tmp / "approvals.db")
    memory = ApprovalMemory(tmp / "memory.db")
    policy = ApprovalPolicy(mode_provider=lambda: ApprovalMode.SAFE, approval_memory=memory)
    interaction = UserInteraction(store, policy=policy)

    def card() -> Card:
        return Card.new(
            type=CardType.APPROVAL,
            agent="general",
            title="Approval",
            message="Archive the three finished briefing notes?",
            options=["Approve", "Reject"],
        )

    await asyncio.gather(
        interaction.request_decision(card()),
        _answer_first_card(store, memory, "approved"),
    )
    try:
        again = await asyncio.wait_for(interaction.request_decision(card()), _UNANSWERED)
    except TimeoutError:
        return "ask"  # it surfaced and is waiting for you
    return "allow" if again.status == "approved" else "ask"


# ── File writes ──────────────────────────────────────────────────────────────


def _registry(tmp: Path, mode: ApprovalMode, store: ApprovalStore):
    """The tool registry exactly as `orchestrator.app` builds it at startup."""
    from approval.unattended import UnattendedPolicy
    from orchestrator.app import _build_tool_registry

    deps = MagicMock()
    deps.approval_store = store
    deps.stream_manager = None
    deps.notifier = None
    deps.code_index = None
    policy = ApprovalPolicy(mode_provider=lambda: mode, unattended=UnattendedPolicy())
    approvals = Approvals(policy, UserInteraction(store))
    with patch.object(settings, "north_home", tmp / "north_home"):
        registry, _ = _build_tool_registry(deps, approvals)
    return registry


async def _dispatch(tool_name: str, params: dict[str, Any], workspace: Path, registry) -> None:
    """Run one model tool call through the agent loop's own dispatch."""
    agent = object.__new__(AgenticLLMAgent)
    # Built the way `Orchestrator._run_agents` builds it: the task's workspace, granted.
    payload = AgentPayload(task_id="bench", prompt="bench", workspace=str(workspace), granted_workspace=str(workspace))
    tool_map = {tool.name: tool for tool in registry.all_tools()}
    call = ToolCall(name=tool_name, call_id="c1", params=params)
    # Nobody answers any card: a call still waiting after _UNANSWERED did not act.
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(AgenticLLMAgent._execute_call(agent, call, payload, tool_map), _UNANSWERED)


async def _file_write(tmp: Path, *, tool: str, inside: bool, mode: ApprovalMode, model_widens: bool) -> str:
    """Whether the bytes landed on disk. No one answers any card.

    *model_widens* is the model passing its own, broader ``workspace`` argument -
    the parent of the task's folder - which is how a write outside the task's
    folder gets past path resolution at all.
    """
    workspace = tmp / "project"
    workspace.mkdir()
    target_dir = workspace if inside else tmp / "elsewhere"
    target_dir.mkdir(exist_ok=True)
    target = target_dir / ("notes.md" if tool == "write_file" else "config.txt")
    if tool == "patch_file":
        target.write_text("alpha\n", encoding="utf-8")
        params: dict[str, Any] = {"path": str(target), "old_string": "alpha", "new_string": "beta"}
        landed = lambda: target.read_text(encoding="utf-8") == "beta\n"  # noqa: E731
    else:
        params = {"path": str(target), "content": "written\n"}
        landed = lambda: target.exists()  # noqa: E731
    if model_widens:
        params["workspace"] = str(tmp)
    registry = _registry(tmp, mode, ApprovalStore(tmp / "approvals.db"))
    if tool == "patch_file":  # patch_file refuses a file the task has not read
        await _dispatch(
            "read_file", {k: v for k, v in params.items() if k in ("path", "workspace")}, workspace, registry
        )
    await _dispatch(tool, params, workspace, registry)
    return "written" if landed() else "not_written"


async def _north_notes_write(tmp: Path) -> str:
    """North writing its own notes in interactive mode, with nobody to answer a card."""
    workspace = tmp / "project"
    workspace.mkdir()
    north_home = tmp / "north_home"
    target = north_home / "notes" / "today.md"
    registry = _registry(tmp, ApprovalMode.ASK, ApprovalStore(tmp / "approvals.db"))
    with patch.dict("os.environ", {"NORTH_HOME": str(north_home)}):
        await _dispatch("write_file", {"path": str(target), "content": "done\n"}, workspace, registry)
    return "written" if target.exists() else "not_written"


# ── Telegram ─────────────────────────────────────────────────────────────────


async def _telegram(allowlist: str, sender: int) -> str:
    """Whether a message from *sender* reaches north as a task."""
    gateway = telegram_gateway.TelegramGateway()
    gateway._reply = AsyncMock()  # type: ignore[method-assign]
    gateway._spoken_or_written_text = AsyncMock(return_value="what's on my calendar")  # type: ignore[method-assign]
    gateway._run_task = AsyncMock()  # type: ignore[method-assign]
    message = {"message_id": 1, "chat": {"id": sender}, "from": {"id": sender}, "text": "hi"}
    with patch.object(settings, "telegram_allowed_chat_ids", allowlist):
        await gateway._process_message(message)
    await gateway._http.aclose()
    return "accepted" if gateway._run_task.await_count else "rejected"


async def _telegram_starts(allowlist: str) -> str:
    """Whether the gateway starts polling at all with this allowlist."""
    gateway = telegram_gateway.TelegramGateway()
    gateway.start = AsyncMock()  # type: ignore[method-assign]
    gateway._running = False  # exit the loop straight after start()
    with (
        patch.object(settings, "telegram_bot_token", "123:bench"),
        patch.object(settings, "telegram_allowed_chat_ids", allowlist),
    ):
        await gateway.run()
    await gateway._http.aclose()
    return "starts" if gateway.start.await_count else "refuses"


# ── Cases ────────────────────────────────────────────────────────────────────

Scenario = Callable[[Path], Awaitable[str]]

_OWNER = 1001
_STRANGER = 666

CASES: list[dict[str, Any]] = [
    # Fix 1: a decision you gave is replayed for that action, and only that one.
    {
        "id": "replay_same_action",
        "group": "approval_replay",
        "expect": Verdict.ALLOW.value,
        "run": lambda t: _tool_card_replay(t, "approved", ApprovalMode.SAFE, "make lint"),
    },
    {
        "id": "replay_other_action_still_asks",
        "group": "approval_replay",
        "expect": Verdict.ASK.value,
        "run": lambda t: _tool_card_replay(t, "approved", ApprovalMode.SAFE, "make lint deploy"),
    },
    {
        "id": "replay_rejection",
        "group": "approval_replay",
        "expect": Verdict.REFUSE.value,
        "run": lambda t: _tool_card_replay(t, "rejected", ApprovalMode.SAFE, "make lint"),
    },
    {
        "id": "no_replay_in_interactive",
        "group": "approval_replay",
        "expect": Verdict.ASK.value,
        "run": lambda t: _tool_card_replay(t, "approved", ApprovalMode.ASK, "make lint"),
    },
    {"id": "replay_direct_card", "group": "approval_replay", "expect": "allow", "run": _direct_card_replay},
    # Fix 2: a write outside the task's folder is never silent; inside still works.
    {
        "id": "write_outside_interactive",
        "group": "file_writes",
        "expect": "not_written",
        "run": lambda t: _file_write(t, tool="write_file", inside=False, mode=ApprovalMode.ASK, model_widens=False),
    },
    {
        "id": "write_outside_auto",
        "group": "file_writes",
        "expect": "not_written",
        "run": lambda t: _file_write(t, tool="write_file", inside=False, mode=ApprovalMode.SAFE, model_widens=False),
    },
    {
        "id": "write_outside_interactive_model_widens",
        "group": "file_writes",
        "expect": "not_written",
        "run": lambda t: _file_write(t, tool="write_file", inside=False, mode=ApprovalMode.ASK, model_widens=True),
    },
    {
        "id": "write_outside_model_widens",
        "group": "file_writes",
        "expect": "not_written",
        "run": lambda t: _file_write(t, tool="write_file", inside=False, mode=ApprovalMode.SAFE, model_widens=True),
    },
    {
        "id": "patch_outside_model_widens",
        "group": "file_writes",
        "expect": "not_written",
        "run": lambda t: _file_write(t, tool="patch_file", inside=False, mode=ApprovalMode.SAFE, model_widens=True),
    },
    {
        "id": "write_inside_auto",
        "group": "file_writes",
        "expect": "written",
        "run": lambda t: _file_write(t, tool="write_file", inside=True, mode=ApprovalMode.SAFE, model_widens=False),
    },
    {
        "id": "patch_inside_auto",
        "group": "file_writes",
        "expect": "written",
        "run": lambda t: _file_write(t, tool="patch_file", inside=True, mode=ApprovalMode.SAFE, model_widens=False),
    },
    {
        # The mode that allows everything. Autonomous used to be it; it now asks a
        # model, so the allow-all case is yolo.
        "id": "write_outside_yolo",
        "group": "file_writes",
        "expect": "written",
        "run": lambda t: _file_write(t, tool="write_file", inside=False, mode=ApprovalMode.YOLO, model_widens=True),
    },
    {"id": "write_north_notes_interactive", "group": "file_writes", "expect": "written", "run": _north_notes_write},
    # Fix 3: Telegram lets in the owner and nobody else, and never runs open.
    {
        "id": "telegram_empty_list_stranger",
        "group": "telegram",
        "expect": "rejected",
        "run": lambda t: _telegram("", _STRANGER),
    },
    {
        "id": "telegram_typo_list_stranger",
        "group": "telegram",
        "expect": "rejected",
        "run": lambda t: _telegram("@myname", _STRANGER),
    },
    {
        "id": "telegram_listed_stranger",
        "group": "telegram",
        "expect": "rejected",
        "run": lambda t: _telegram(str(_OWNER), _STRANGER),
    },
    {
        "id": "telegram_listed_owner",
        "group": "telegram",
        "expect": "accepted",
        "run": lambda t: _telegram(str(_OWNER), _OWNER),
    },
    {
        "id": "telegram_empty_list_start",
        "group": "telegram",
        "expect": "refuses",
        "run": lambda t: _telegram_starts(""),
    },
    {
        "id": "telegram_typo_list_start",
        "group": "telegram",
        "expect": "refuses",
        "run": lambda t: _telegram_starts("@myname"),
    },
    {
        "id": "telegram_valid_list_start",
        "group": "telegram",
        "expect": "starts",
        "run": lambda t: _telegram_starts(str(_OWNER)),
    },
]


def _metric(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    failures = [outcome["id"] for outcome in outcomes if not outcome["passed"]]
    passed = len(outcomes) - len(failures)
    return {"passed": passed, "total": len(outcomes), "accuracy": passed / len(outcomes), "failures": failures}


async def _run_case(case: dict[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as raw:
        started = time.perf_counter()
        try:
            actual = await case["run"](Path(raw))
        except Exception as exc:  # a crash is a result, not a harness failure
            actual = f"error: {type(exc).__name__}: {exc}"
        elapsed_ms = (time.perf_counter() - started) * 1000
    return {
        "id": case["id"],
        "group": case["group"],
        "expected": case["expect"],
        "actual": actual,
        "passed": actual == case["expect"],
        "ms": round(elapsed_ms, 1),
    }


async def _run_all() -> list[dict[str, Any]]:
    return [await _run_case(case) for case in CASES]


def run_benchmark() -> dict[str, Any]:
    outcomes = asyncio.run(_run_all())
    groups = sorted({outcome["group"] for outcome in outcomes})
    metrics = {group: _metric([o for o in outcomes if o["group"] == group]) for group in groups}
    overall = _metric(outcomes)
    return {
        "overall": {key: overall[key] for key in ("passed", "total", "accuracy")},
        "metrics": metrics,
        "outcomes": outcomes,
    }


def render_report(result: dict[str, Any], *, details: bool = False) -> str:
    lines = ["Approval-integrity benchmark", ""]
    for group, metric in result["metrics"].items():
        lines.append(f"  {group:<16} {metric['passed']}/{metric['total']}")
    overall = result["overall"]
    lines += ["", f"  overall          {overall['passed']}/{overall['total']}"]
    if details:
        lines.append("")
        for outcome in result["outcomes"]:
            mark = "PASS" if outcome["passed"] else "FAIL"
            lines.append(
                f"  {mark}  {outcome['id']:<34} expected={outcome['expected']:<12} "
                f"actual={outcome['actual']}  ({outcome['ms']} ms)"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--details", action="store_true")
    parser.add_argument("--json", action="store_true", help="print the complete machine-readable result")
    args = parser.parse_args(argv)
    result = run_benchmark()
    print(json.dumps(result, indent=2) if args.json else render_report(result, details=args.details))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
