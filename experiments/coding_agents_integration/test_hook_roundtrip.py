"""Question: can Claude Code's hook reach north's approval layer over loopback, and fail closed?

Runs the real shim as a subprocess against a loopback server in front of the real `Approvals`.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from types import SimpleNamespace

import pytest

from approval.approvals import Approvals
from approval.interaction import UserInteraction
from approval.models import ApprovalDecision
from approval.policy import ApprovalPolicy
from approval.store import ApprovalStore
from approval.unattended import UnattendedPolicy
from config.approval_mode import ApprovalMode

from .prototypes import GateServer, free_port, hook_payload, record, run_shim, until, write_shim

TOKEN = "run-token-1"
BASH = hook_payload("Bash", {"command": "make build"}, "/work/repo-wt")


def _approvals(store: ApprovalStore) -> Approvals:
    policy = ApprovalPolicy(mode_provider=lambda: ApprovalMode.ASK, unattended=UnattendedPolicy())
    return Approvals(policy, UserInteraction(store))


@pytest.fixture
async def gate(tmp_path):
    store = ApprovalStore()
    server = GateServer(_approvals(store), {TOKEN: "t-run"})
    await server.start()
    yield SimpleNamespace(store=store, server=server, shim=write_shim(tmp_path))
    await server.stop()


def _decision(stdout: str) -> str:
    return json.loads(stdout)["hookSpecificOutput"]["permissionDecision"]


async def test_the_shim_allows_after_you_approve_and_the_card_names_the_task(gate) -> None:
    running = asyncio.create_task(run_shim(gate.shim, gate.server.url, TOKEN, BASH))
    card = (await until(gate.store.pending))[0]
    gate.store.resolve(card.id, ApprovalDecision.APPROVED)
    code, out, _ = await running

    assert (card.task_id, code, _decision(out)) == ("t-run", 0, "allow")


async def test_the_shim_denies_after_you_reject(gate) -> None:
    running = asyncio.create_task(run_shim(gate.shim, gate.server.url, TOKEN, BASH))
    card = (await until(gate.store.pending))[0]
    gate.store.resolve(card.id, ApprovalDecision.REJECTED)
    code, out, _ = await running

    assert (code, _decision(out)) == (0, "deny")


async def test_a_slow_human_is_still_answered(gate) -> None:
    """The shim has no client timeout; the wait is the card's, which never expires."""
    running = asyncio.create_task(run_shim(gate.shim, gate.server.url, TOKEN, BASH))
    card = (await until(gate.store.pending))[0]
    await asyncio.sleep(2.0)
    assert not running.done()
    gate.store.resolve(card.id, ApprovalDecision.APPROVED)
    code, out, _ = await running

    assert (code, _decision(out)) == (0, "allow")


async def test_the_shim_fails_closed_when_the_server_is_down(gate) -> None:
    code, out, err = await run_shim(gate.shim, f"http://127.0.0.1:{free_port()}/gate", TOKEN, BASH)

    assert (code, out) == (2, "") and "north gate" in err


async def test_the_shim_fails_closed_on_an_unknown_run_token(gate) -> None:
    code, out, _ = await run_shim(gate.shim, gate.server.url, "not-a-token", BASH)

    assert (code, out) == (2, "")


async def test_the_shim_fails_closed_on_a_garbage_answer(tmp_path) -> None:
    server = GateServer(_approvals(ApprovalStore()), {TOKEN: "t-run"}, garbage=True)
    await server.start()
    try:
        code, out, _ = await run_shim(write_shim(tmp_path), server.url, TOKEN, BASH)
    finally:
        await server.stop()

    assert (code, out) == (2, "")


async def test_how_long_one_gate_call_takes_for_a_read_only_request(gate) -> None:
    status = hook_payload("Bash", {"command": "git status"}, "/work/repo-wt")
    timings = []
    for _ in range(15):
        start = time.perf_counter()
        code, out, _ = await run_shim(gate.shim, gate.server.url, TOKEN, status)
        timings.append((time.perf_counter() - start) * 1000)
        assert (code, _decision(out)) == (0, "allow")

    record(
        "H_latency_ms",
        {"median": round(statistics.median(timings), 1), "p95": round(sorted(timings)[-2], 1), "runs": len(timings)},
    )
