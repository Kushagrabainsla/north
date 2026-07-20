"""Tests for fix 5a: the orchestrator-driven engineering conductor loop."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from approval.store import ApprovalStore
from orchestrator import orchestrator as orch_mod
from orchestrator.orchestrator import _DESIGN_ARCHITECT_PREAMBLE, Orchestrator, _parse_spec_tasks
from orchestrator.review import ReviewResult

_PASS = ReviewResult.parse({"status": "PASS", "must_fix": [], "tests": {"passed": True}})
_FAIL = ReviewResult.parse({"status": "FAIL", "must_fix": ["foo.py:1 - bug"]})
_PREAMBLE = "implement it"  # loop-behavior tests don't care about the coder framing


def _orch(*, names=("coder", "reviewer")):
    registry = MagicMock()
    registry.names.return_value = list(names)
    registry.get.side_effect = lambda n: type("A", (), {"name": n})()
    stream = MagicMock()
    stream.emit = AsyncMock()
    ledger = MagicMock()
    ledger.write = AsyncMock()
    return Orchestrator(
        ledger=ledger,
        agent_registry=registry,
        north_star_checker=MagicMock(),
        execution_planner=MagicMock(),
        task_context_store=MagicMock(),
        failure_handler=MagicMock(),
        notifier=MagicMock(),
        stream_manager=stream,
        approval_store=ApprovalStore(),
    )


def _script_group(orch, returns: list[list[str]]):
    """Replace _execute_agent_group with a recorder returning scripted failures."""
    calls: list[str] = []
    seq = list(returns)

    async def fake(task_id, prompt, agents, workspace="", context="", allow_delegation=True):
        calls.append(agents[0].name)
        return seq.pop(0) if seq else []

    orch._execute_agent_group = fake
    return calls


def _script_reviews(monkeypatch, verdicts):
    seq = list(verdicts)
    monkeypatch.setattr(orch_mod, "read_review_result", lambda task_id: seq.pop(0) if seq else None)


# ---------------------------------------------------------------- gating


def test_use_conductor_requires_domain_agents_and_code():
    from orchestrator.models import ExecutionMode, ExecutionPlan

    code_plan = ExecutionPlan(
        task_id="t", agents=["coder", "reviewer"], parallel_groups=[["coder"], ["reviewer"]],
        dependencies={}, mode=ExecutionMode.HIERARCHICAL,
    )
    noncode_plan = ExecutionPlan(
        task_id="t", agents=["researcher"], parallel_groups=[["researcher"]],
        dependencies={}, mode=ExecutionMode.SINGLE_AGENT,
    )
    on = _orch()
    assert on._use_conductor("engineering", code_plan) is True
    # A no-code engineering kind (question/research) must NOT use the conductor.
    assert on._use_conductor("engineering", noncode_plan) is False
    assert on._use_conductor("finance", code_plan) is False

    missing = _orch(names=("coder",))  # no reviewer registered
    assert missing._use_conductor("engineering", code_plan) is False


def test_deploy_uses_deploy_flow_not_conductor():
    from orchestrator.models import ExecutionMode, ExecutionPlan

    deploy_plan = ExecutionPlan(
        task_id="t", agents=["coder"], parallel_groups=[["coder"]],
        dependencies={}, mode=ExecutionMode.SINGLE_AGENT, engineering_kind="deploy",
    )
    orch = _orch()
    # Deploy has a coder, but must NOT be handled by the conductor (no code DoD) -
    # it is a distinct human-gated shipping flow.
    assert orch._use_conductor("engineering", deploy_plan) is False
    assert orch._use_deploy_flow("engineering", deploy_plan) is True
    # A normal code plan is not a deploy flow.
    code_plan = ExecutionPlan(
        task_id="t", agents=["coder", "reviewer"], parallel_groups=[["coder"], ["reviewer"]],
        dependencies={}, mode=ExecutionMode.HIERARCHICAL, engineering_kind="bugfix",
    )
    assert orch._use_deploy_flow("engineering", code_plan) is False


async def test_run_deploy_flow_runs_coder_with_ship_framing():
    prompts: dict[str, str] = {}

    async def fake(task_id, prompt, agents, workspace="", context="", allow_delegation=True):
        prompts[agents[0].name] = prompt
        return []

    orch = _orch()
    orch._execute_agent_group = fake
    failures = await orch._run_deploy_flow("t1", "ship the changes", "/ws")
    assert failures == []
    assert "coder" in prompts
    body = prompts["coder"].lower()
    assert "shipping task" in body  # deploy framing, not a coding loop
    assert "request_approval" in body  # semantic ship checkpoint before external actions


# ---------------------------------------------------------------- design phase (cockpit)


def _plan(kind: str):
    from orchestrator.models import ExecutionMode, ExecutionPlan

    return ExecutionPlan(
        task_id="t", agents=["coder", "reviewer"], parallel_groups=[["coder"]],
        dependencies={}, mode=ExecutionMode.HIERARCHICAL, engineering_kind=kind,
    )


def test_use_design_phase_only_for_feature_and_refactor():
    orch = _orch(names=("researcher", "architect", "coder", "reviewer"))
    assert orch._use_design_phase(_plan("feature")) is True
    assert orch._use_design_phase(_plan("refactor")) is True
    # Small/localized kinds skip the design discussion (conductor clarifies if stuck).
    for kind in ("bugfix", "debug", "test"):
        assert orch._use_design_phase(_plan(kind)) is False, kind
    # Without researcher+architect registered there is no design phase.
    assert _orch(names=("coder", "reviewer"))._use_design_phase(_plan("feature")) is False


def test_design_phase_skipped_in_autonomous_mode():
    from approval.mode import ApprovalMode

    orch = _orch(names=("researcher", "architect", "coder", "reviewer"))
    orch._north_settings = type("S", (), {"autonomy": ApprovalMode.AUTONOMOUS})()
    assert orch._human_available() is False
    assert orch._use_design_phase(_plan("feature")) is False


async def test_run_design_phase_runs_researcher_then_architect(tmp_path, monkeypatch):
    calls: list[str] = []

    async def fake(task_id, prompt, agents, workspace="", context="", allow_delegation=True):
        calls.append(agents[0].name)
        return []

    orch = _orch(names=("researcher", "architect", "coder", "reviewer"))
    orch._execute_agent_group = fake
    # No real research artifact; a nonexistent path makes the read a harmless no-op.
    monkeypatch.setattr(orch, "_primary_artifact_path", lambda name, tid: Path("/nonexistent/x.md"))
    # The architect is "successful" only if it wrote a usable spec, so write one.
    monkeypatch.setattr("orchestrator.orchestrator.handoff_dir_for", lambda tid: str(tmp_path / tid))
    spec_dir = tmp_path / "t1" / "architecture"
    spec_dir.mkdir(parents=True)
    (spec_dir / "spec.md").write_text("# Spec\n\n" + ("detailed agreed design. " * 20), encoding="utf-8")

    failures = await orch._run_design_phase("t1", "build a thing", "/ws")
    assert failures == []
    assert calls == ["researcher", "architect"]


async def test_run_design_phase_fails_when_architect_writes_no_spec(tmp_path, monkeypatch):
    """A 'successful' architect that produced no usable spec must fail the design
    phase, so the coder is never sent to implement a spec that doesn't exist."""

    async def fake(task_id, prompt, agents, workspace="", context="", allow_delegation=True):
        return []  # both agents "succeed"

    orch = _orch(names=("researcher", "architect", "coder", "reviewer"))
    orch._execute_agent_group = fake
    monkeypatch.setattr(orch, "_primary_artifact_path", lambda name, tid: Path("/nonexistent/x.md"))
    monkeypatch.setattr("orchestrator.orchestrator.handoff_dir_for", lambda tid: str(tmp_path / tid))
    # No spec.md is written.
    failures = await orch._run_design_phase("t1", "build a thing", "/ws")
    assert failures == ["architect"]


def test_coder_preamble_for_kind_selects_framing():
    # test/debug are task-framings of the one coder loop - the kind picks the coder's
    # preamble (reproduce-first / tests-only), with a default for everything else.
    orch = _orch()
    assert "reproduce" in orch._coder_preamble_for_kind("debug").lower()
    assert "do not change production code" in orch._coder_preamble_for_kind("test").lower()
    assert "own this task end to end" in orch._coder_preamble_for_kind("feature").lower()
    assert "own this task end to end" in orch._coder_preamble_for_kind("").lower()


def test_coder_preamble_for_agreed_spec_points_to_spec():
    preamble = _orch()._coder_preamble_for_agreed_spec("t1")
    assert "agreed design spec" in preamble.lower()
    assert "spec.md" in preamble.lower()


def test_parse_spec_tasks_extracts_checkbox_items_under_tasks():
    spec = "## Why\nx\n## Tasks\n- [ ] 1. First step\n- [ ] Second step\nignored prose\n## Notes\n- [ ] not a task"
    assert _parse_spec_tasks(spec) == ["First step", "Second step"]


def test_parse_spec_tasks_empty_when_no_tasks_section():
    assert _parse_spec_tasks("## Design\napproach only, no tasks section") == []


def test_architect_preamble_requires_structured_spec():
    for section in ("## Why", "## Requirements", "## Design", "## Tasks"):
        assert section in _DESIGN_ARCHITECT_PREAMBLE
    assert "[ ]" in _DESIGN_ARCHITECT_PREAMBLE  # checkbox tasks


def test_coder_spec_preamble_follows_the_seeded_plan():
    preamble = _orch()._coder_preamble_for_agreed_spec("t1").lower()
    assert "checklist" in preamble
    assert "pending" in preamble
    assert "do not rewrite" in preamble or "do not redesign" in preamble


# ---------------------------------------------------------------- loop behavior


async def test_passes_first_review(monkeypatch):
    orch = _orch()
    calls = _script_group(orch, [[], []])
    _script_reviews(monkeypatch, [_PASS])
    failures = await orch._run_engineering_conductor("t1", "build x", "/ws", _PREAMBLE)
    assert failures == []
    assert calls == ["coder", "reviewer"]  # coder once, reviewer once, no fix


async def test_reviewer_runs_in_report_only_mode(monkeypatch):
    """The conductor must run the reviewer with allow_delegation=False so it reports
    only - the orchestrator owns the fix loop. The coder keeps delegation (read-only
    research). A reviewer-initiated delegation would collide with the bounded loop."""
    orch = _orch()
    recorded: list[tuple[str, bool]] = []

    async def fake(task_id, prompt, agents, workspace="", context="", allow_delegation=True):
        recorded.append((agents[0].name, allow_delegation))
        return []

    orch._execute_agent_group = fake
    _script_reviews(monkeypatch, [_FAIL, _PASS])  # force a fix round too
    await orch._run_engineering_conductor("t1", "build x", "/ws", _PREAMBLE)
    assert ("coder", True) in recorded
    assert any(name == "reviewer" for name, _ in recorded)
    assert all(flag is False for name, flag in recorded if name == "reviewer")


async def test_fail_then_pass_runs_one_fix(monkeypatch):
    orch = _orch()
    calls = _script_group(orch, [[], [], [], []])
    _script_reviews(monkeypatch, [_FAIL, _PASS])
    failures = await orch._run_engineering_conductor("t1", "build x", "/ws", _PREAMBLE)
    assert failures == []
    assert calls == ["coder", "reviewer", "coder", "reviewer"]


async def test_persistent_failure_is_bounded(monkeypatch):
    orch = _orch()
    calls = _script_group(orch, [[]] * 10)
    _script_reviews(monkeypatch, [_FAIL, _FAIL, _FAIL, _FAIL])  # never passes
    failures = await orch._run_engineering_conductor("t1", "build x", "/ws", _PREAMBLE)
    assert failures == []  # stops (DoD gate will flag), does not loop forever
    # initial coder + 3 reviews + 2 fixes (MAX_FIX_ROUNDS=2)
    assert calls.count("reviewer") == 3
    assert calls.count("coder") == 3


async def test_coder_failure_skips_review(monkeypatch):
    orch = _orch()
    calls = _script_group(orch, [["coder"]])  # coder fails
    _script_reviews(monkeypatch, [_PASS])
    failures = await orch._run_engineering_conductor("t1", "build x", "/ws", _PREAMBLE)
    assert failures == ["coder"]
    assert calls == ["coder"]  # reviewer never ran


async def test_reviewer_failure_returns(monkeypatch):
    orch = _orch()
    calls = _script_group(orch, [[], ["reviewer"]])  # coder ok, reviewer fails
    _script_reviews(monkeypatch, [_PASS])
    failures = await orch._run_engineering_conductor("t1", "build x", "/ws", _PREAMBLE)
    assert failures == ["reviewer"]
    assert calls == ["coder", "reviewer"]


async def test_missing_verdict_retries_reviewer_then_stops(monkeypatch):
    # A missing structured verdict must NOT be treated as "done": the reviewer is
    # retried (demanding the JSON) up to the cap, then the DoD gate flags it.
    orch = _orch()
    calls = _script_group(orch, [[]] * 10)
    _script_reviews(monkeypatch, [None, None, None])  # reviewer never emits a verdict
    failures = await orch._run_engineering_conductor("t1", "build x", "/ws", _PREAMBLE)
    assert failures == []
    assert calls.count("coder") == 1  # coder ran once; no fixes (nothing structured to fix)
    assert calls.count("reviewer") == 3  # retried up to MAX_FIX_ROUNDS+1


async def test_missing_verdict_then_pass(monkeypatch):
    orch = _orch()
    calls = _script_group(orch, [[]] * 10)
    _script_reviews(monkeypatch, [None, _PASS])  # missing, then a real pass on retry
    failures = await orch._run_engineering_conductor("t1", "build x", "/ws", _PREAMBLE)
    assert failures == []
    assert calls == ["coder", "reviewer", "reviewer"]  # coder once, reviewer retried then passed
