"""The arms of the comparison. Each runs one task in a prepared workspace and reports what it did.

raw_claude / raw_codex   the vendor CLI used directly, headless, in its usual unattended setting.
new_claude / new_codex   north's `coding_agent` tool with the real runner, gate, approval layer, tests, landing
                         and cross-review, started directly (north's own model is not asked to pick it).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from workspace import Prepared, Task


@dataclass
class ArmResult:
    arm: str
    answer: str = ""
    error: str = ""
    seconds: float = 0.0
    cost_usd: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    # Where the finished change is, when it is not the workspace itself (a kept branch).
    branch_path: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    # What happened, in order: {t seconds, kind, name?, detail}.
    timeline: list[dict[str, Any]] = field(default_factory=list)


def _run(argv: list[str], cwd: Path, timeout: int, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    try:
        done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        return 124, str(exc.stdout or ""), f"timed out after {timeout}s"
    return done.returncode, done.stdout, done.stderr


def stream_process(argv: list[str], cwd: Path, timeout: int) -> tuple[int, list[tuple[float, str]], str]:
    """Run *argv*, returning (exit code, [(seconds since start, stdout line)], stderr), killing it at *timeout*."""
    started = time.monotonic()
    process = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    errors: list[str] = []
    drain = threading.Thread(target=lambda: errors.append(process.stderr.read()), daemon=True)
    drain.start()
    timer = threading.Timer(timeout, process.kill)
    timer.start()
    lines: list[tuple[float, str]] = []
    try:
        for line in process.stdout:
            lines.append((round(time.monotonic() - started, 2), line.rstrip("\n")))
        process.wait()
    finally:
        timer.cancel()
    drain.join(timeout=2)
    code = 124 if time.monotonic() - started >= timeout else process.returncode
    return code, lines, "".join(errors)


def _clip(value: Any, limit: int = 240) -> str:
    return " ".join(str(value).split())[:limit]


def _jsonl(lines: list[tuple[float, str]]) -> list[tuple[float, dict]]:
    events = []
    for at, line in lines:
        try:
            events.append((at, json.loads(line)))
        except ValueError:
            continue
    return events


def raw_claude(task: Task, prepared: Prepared, log: Path) -> ArmResult:
    code, lines, err = stream_process(
        [
            "claude",
            "-p",
            task.prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "acceptEdits",
            "--allowedTools",
            "Bash",
        ],
        prepared.path,
        task.timeout_s,
    )
    log.write_text("\n".join(line for _, line in lines) + "\n--- stderr ---\n" + err)
    result = ArmResult("raw_claude")
    final: dict = {}
    for at, event in _jsonl(lines):
        kind = event.get("type")
        if kind == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use":
                    result.timeline.append(
                        {"t": at, "kind": "tool", "name": block.get("name"), "detail": _clip(block.get("input"))}
                    )
                elif block.get("type") == "text" and block.get("text", "").strip():
                    result.timeline.append({"t": at, "kind": "text", "detail": _clip(block["text"])})
        elif kind == "user":
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                    result.timeline.append({"t": at, "kind": "tool_error", "detail": _clip(block.get("content"))})
        elif kind == "result":
            final = event
    if not final:
        result.error = f"exit {code}: {err[-300:]}"
        return result
    usage = final.get("usage") or {}
    result.answer = str(final.get("result") or "")
    result.cost_usd = final.get("total_cost_usd")
    result.tokens_in = sum(
        int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )
    result.tokens_out = int(usage.get("output_tokens") or 0)
    result.seconds = round((final.get("duration_ms") or 0) / 1000, 1)
    result.extra = {"turns": final.get("num_turns"), "denials": final.get("permission_denials") or []}
    if final.get("is_error"):
        result.error = str(final.get("result") or "claude reported an error")
    return result


def raw_codex(task: Task, prepared: Prepared, log: Path) -> ArmResult:
    last = prepared.path.parent / f"{prepared.path.name}-codex-last.txt"
    code, lines, err = stream_process(
        ["codex", "exec", "--json", "-s", "workspace-write", "-C", str(prepared.path), "-o", str(last), task.prompt],
        prepared.path,
        task.timeout_s,
    )
    log.write_text("\n".join(line for _, line in lines) + "\n--- stderr ---\n" + err)
    result = ArmResult("raw_codex")
    result.answer = last.read_text() if last.exists() else ""
    tokens_in = tokens_out = 0
    for at, event in _jsonl(lines):
        item = event.get("item") or {}
        if event.get("type") == "item.completed":
            if item.get("type") == "command_execution":
                result.timeline.append(
                    {
                        "t": at,
                        "kind": "tool",
                        "name": "command",
                        "detail": _clip(item.get("command")),
                        "exit": item.get("exit_code"),
                    }
                )
            elif item.get("type") == "file_change":
                result.timeline.append(
                    {"t": at, "kind": "tool", "name": "file_change", "detail": _clip(item.get("changes"))}
                )
            elif item.get("type") == "agent_message":
                result.timeline.append({"t": at, "kind": "text", "detail": _clip(item.get("text"))})
        usage = event.get("usage") or {}
        if event.get("type") == "turn.completed":
            tokens_in += int(usage.get("input_tokens") or 0)
            tokens_out += int(usage.get("output_tokens") or 0)
    result.tokens_in, result.tokens_out = tokens_in or None, tokens_out or None
    result.seconds = lines[-1][0] if lines else 0.0
    if code != 0:
        result.error = f"exit {code}: {err[-300:]}"
    return result


class AutoDecider:
    """Stands in for north's memory in autonomous mode: yes to anything that stays inside the workspace.

    What leaves it (a push, a host the agent was not given, money) abstains, so a card waits, and the
    eval's card bot then says no, the way a person who stated nothing covering it would.
    """

    async def rule(self, action):
        from approval.models import ApprovalDecision

        if action.leaves_sandbox or action.reaches_third_party or action.spends_money:
            return None
        from approval.policy import Answer

        return Answer(ApprovalDecision.APPROVED, "Approve", "stand-in memory: inside the workspace", "eval")

    async def answer(self, card):
        return None


def new_path(backend: str):
    def run(task: Task, prepared: Prepared, log: Path) -> ArmResult:
        return asyncio.run(_new_path(backend, task, prepared, log))

    return run


async def _new_path(backend: str, task: Task, prepared: Prepared, log: Path) -> ArmResult:
    from approval.models import ApprovalDecision
    from coding_agents import ClaudeBackend, CodexBackend, CodingRunner, CommandVerifier
    from config.approval_mode import ApprovalMode
    from orchestrator.agent_runs import AgentRunStore
    from orchestrator.coding_landing import LandingDesk
    from orchestrator.coding_run_recorder import AgentRunRecorder
    from orchestrator.coding_workspaces import GitWorkspaces
    from orchestrator.verify_command import detect_verify_command
    from tests.live.support import start_gate
    from tools.models import ToolInput
    from tools.registry import ToolRegistry
    from tools.specialized.bash import BashTool
    from tools.specialized.coding_agent import CodingAgentTool
    from tools.specialized.coding_shell import BashShell

    scratch = Path(tempfile.mkdtemp(prefix="north-eval-new-"))
    gate = await start_gate(ApprovalMode.AUTONOMOUS, ApprovalDecision.REJECTED, decider=AutoDecider())
    started = time.monotonic()
    try:
        store = AgentRunStore(scratch / "tasks.db")
        protected = [str(Path.home() / ".north")]
        registry = ToolRegistry(approvals=gate.approvals)
        registry.register(BashTool())
        runner = CodingRunner(
            {"claude": ClaudeBackend(protected_paths=protected), "codex": CodexBackend(protected_paths=protected)},
            AgentRunRecorder(store),
            workspaces=GitWorkspaces(scratch / "copies"),
            sessions=gate.sessions,
            gate_url=gate.url,
            verifier=CommandVerifier(detect_verify_command, BashShell(registry)),
            lander=LandingDesk(gate.approvals),
        )
        tool = CodingAgentTool(runner)
        tool.approvals = gate.approvals
        output = await asyncio.wait_for(
            tool.execute(
                ToolInput(
                    params={"task_id": "eval", "task": task.prompt, "mode": "edit", "backend": backend},
                    granted_workspace=str(prepared.path),
                )
            ),
            timeout=task.timeout_s,
        )
        result = ArmResult(f"new_{backend}", seconds=time.monotonic() - started)
        data = output.data or {}
        log.write_text(
            json.dumps({"success": output.success, "error": output.error, "data": data}, indent=2, default=str)
        )
        result.answer = str(data.get("answer") or "")
        result.error = "" if output.success else (output.error or "failed")
        runs = await store.list_for_task("eval")
        result.cost_usd = sum(r.cost_usd or 0 for r in runs) or None
        result.tokens_in = sum(r.tokens_in for r in runs) or None
        result.tokens_out = sum(r.tokens_out for r in runs) or None
        landing = data.get("landing") or {}
        change = data.get("change") or {}
        if change and landing.get("state") != "applied" and change.get("path"):
            result.branch_path = Path(change["path"])
        for run in runs:
            for event in await store.list_events(run.run_id):
                result.timeline.append(
                    {
                        "t": event.get("timestamp"),
                        "kind": event.get("event"),
                        "run": run.agent,
                        "detail": _clip(event.get("data")),
                    }
                )
        result.extra = {
            "gate_decisions": gate.judge.verdicts,
            "landing": landing,
            "verification": data.get("verification"),
            "review": data.get("review"),
            "denied": data.get("denied"),
            "cards": [{"title": c.title, "message": c.message[:200]} for c in gate.cards],
            "runs": [r.agent for r in runs],
        }
        return result
    except TimeoutError:
        return ArmResult(
            f"new_{backend}", error=f"timed out after {task.timeout_s}s", seconds=time.monotonic() - started
        )
    finally:
        await gate.stop()
        keep = os.environ.get("EVAL_KEEP_SCRATCH")
        if not keep:
            shutil.rmtree(scratch, ignore_errors=True)
