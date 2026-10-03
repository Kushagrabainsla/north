"""The Claude backend against a fake `claude` that replays what the real one printed."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from coding_agents import ClaudeBackend, CodingAgentError, EventKind, FailureKind, Mode, RunEvent, RunSpec
from coding_agents.models import GateAccess

SESSION = "11111111-2222-4333-8444-555555555555"


def _spec(workspace: Path, **overrides) -> RunSpec:
    return RunSpec(task="Plan a subtract function.", workspace=str(workspace), session_id=SESSION, **overrides)


async def _run(backend: ClaudeBackend, spec: RunSpec) -> tuple[object, list[RunEvent]]:
    events: list[RunEvent] = []

    async def sink(event: RunEvent) -> None:
        events.append(event)

    return await backend.run(spec, sink), events


def _call(workspace: Path) -> dict:
    return json.loads((workspace / ".fake_claude_call.json").read_text())


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    directory = tmp_path / "repo"
    directory.mkdir()
    return directory


class TestProbe:
    async def test_an_installed_logged_in_claude_is_available(self, make_fake_claude) -> None:
        availability = await ClaudeBackend(str(make_fake_claude())).probe()

        assert availability.available and availability.version == "2.1.286"

    async def test_a_claude_that_is_not_installed_says_so(self) -> None:
        availability = await ClaudeBackend("claude-that-does-not-exist").probe()

        assert not availability.available and "not installed" in availability.reason

    async def test_a_claude_too_old_for_the_flags_we_need_is_refused(self, make_fake_claude) -> None:
        availability = await ClaudeBackend(str(make_fake_claude(version="2.1.200 (Claude Code)"))).probe()

        assert not availability.available and "claude update" in availability.reason

    async def test_a_logged_out_claude_says_how_to_log_in(self, make_fake_claude) -> None:
        availability = await ClaudeBackend(str(make_fake_claude(logged_in=False))).probe()

        assert not availability.available and "auth login" in availability.reason


class TestHowItIsCalled:
    async def test_it_runs_read_only_with_nothing_pre_allowed_and_only_the_users_settings(
        self, make_fake_claude, workspace
    ) -> None:
        await _run(ClaudeBackend(str(make_fake_claude())), _spec(workspace))
        argv = _call(workspace)["argv"]

        def value(flag: str) -> str:
            return argv[argv.index(flag) + 1]

        assert value("--permission-mode") == "plan"
        assert value("--permission-prompts") == "none"
        assert value("--setting-sources") == "user"
        assert "--strict-mcp-config" in argv
        assert json.loads(value("--mcp-config")) == {"mcpServers": {}}
        assert value("--session-id") == SESSION
        for never in ("--allowedTools", "--dangerously-skip-permissions", "bypassPermissions", "acceptEdits"):
            assert never not in argv

    async def test_the_vendor_sandbox_is_strict_and_hides_north_s_secrets(self, make_fake_claude, workspace) -> None:
        backend = ClaudeBackend(str(make_fake_claude()), protected_paths=["/custom/north-home"])

        await _run(backend, _spec(workspace))
        argv = _call(workspace)["argv"]
        sandbox = json.loads(argv[argv.index("--settings") + 1])["sandbox"]

        assert sandbox["enabled"] and sandbox["failIfUnavailable"] and not sandbox["allowUnsandboxedCommands"]
        assert sandbox["network"] == {"strictAllowlist": True, "allowedDomains": []}
        assert {"~/.north", "~/.ssh", "~/.codex", "/custom/north-home"} <= set(sandbox["filesystem"]["denyRead"])

    async def test_the_task_goes_in_on_stdin_and_the_guidance_is_appended(self, make_fake_claude, workspace) -> None:
        await _run(ClaudeBackend(str(make_fake_claude())), _spec(workspace, guidance="Use type hints."))
        call = _call(workspace)

        assert call["stdin"] == "Plan a subtract function."
        assert call["argv"][call["argv"].index("--append-system-prompt") + 1] == "Use type hints."
        assert call["cwd"] == str(workspace.resolve())

    async def test_resuming_asks_for_the_same_session_instead_of_a_new_one(self, make_fake_claude, workspace) -> None:
        await _run(ClaudeBackend(str(make_fake_claude())), _spec(workspace, resume=True))
        argv = _call(workspace)["argv"]

        assert argv[argv.index("--resume") + 1] == SESSION and "--session-id" not in argv

    async def test_it_starts_with_a_narrow_environment_and_none_of_norths_secrets(
        self, make_fake_claude, workspace, monkeypatch
    ) -> None:
        monkeypatch.setenv("NORTH_SECRET", "s3cret")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-other")
        monkeypatch.setenv("HOME", os.environ["HOME"])

        await _run(ClaudeBackend(str(make_fake_claude())), _spec(workspace))
        names = set(_call(workspace)["env"])

        assert "NORTH_SECRET" not in names and "OPENAI_API_KEY" not in names
        assert "HOME" in names and "PATH" in names


class TestEditMode:
    def _edit(self, workspace: Path) -> RunSpec:
        return _spec(workspace, mode=Mode.EDIT, gate=GateAccess("http://127.0.0.1:8000/gate", "run-token-xyz"))

    @staticmethod
    def _settings(argv: list[str]) -> dict:
        return json.loads(argv[argv.index("--settings") + 1])

    async def test_it_runs_in_the_default_permission_mode_with_nothing_pre_allowed(
        self, make_fake_claude, workspace
    ) -> None:
        await _run(ClaudeBackend(str(make_fake_claude())), self._edit(workspace))
        argv = _call(workspace)["argv"]

        assert argv[argv.index("--permission-mode") + 1] == "default"
        assert argv[argv.index("--permission-prompts") + 1] == "none"
        for never in ("--allowedTools", "--dangerously-skip-permissions", "bypassPermissions", "acceptEdits"):
            assert never not in argv

    async def test_the_hook_asks_north_before_every_action_that_changes_something(
        self, make_fake_claude, workspace
    ) -> None:
        await _run(ClaudeBackend(str(make_fake_claude())), self._edit(workspace))
        (entry,) = self._settings(_call(workspace)["argv"])["hooks"]["PreToolUse"]
        (hook,) = entry["hooks"]

        for tool in ("Bash", "Write", "Edit", "MultiEdit", "NotebookEdit", "WebFetch", "mcp__.*"):
            assert tool in entry["matcher"].split("|")
        assert hook["type"] == "command" and hook["timeout"] >= 86_400
        assert hook["command"].startswith(sys.executable) and hook["command"].endswith("hook.py")

    async def test_a_sandboxed_command_is_not_auto_allowed_so_a_broken_hook_blocks_it(
        self, make_fake_claude, workspace
    ) -> None:
        await _run(ClaudeBackend(str(make_fake_claude())), self._edit(workspace))

        assert self._settings(_call(workspace)["argv"])["sandbox"]["autoAllowBashIfSandboxed"] is False

    async def test_the_token_reaches_the_hook_through_the_environment_not_the_command_line(
        self, make_fake_claude, workspace
    ) -> None:
        await _run(ClaudeBackend(str(make_fake_claude())), self._edit(workspace))
        call = _call(workspace)

        assert {"NORTH_GATE_URL", "NORTH_GATE_TOKEN"} <= set(call["env"])
        assert "run-token-xyz" not in " ".join(call["argv"])

    async def test_a_plan_run_has_no_hook_and_no_gate_environment(self, make_fake_claude, workspace) -> None:
        await _run(ClaudeBackend(str(make_fake_claude())), _spec(workspace))
        call = _call(workspace)

        assert "hooks" not in self._settings(call["argv"])
        assert "NORTH_GATE_TOKEN" not in call["env"]

    async def test_an_edit_run_without_the_gate_is_refused_before_anything_starts(
        self, make_fake_claude, workspace
    ) -> None:
        with pytest.raises(CodingAgentError, match="needs the gate"):
            await _run(ClaudeBackend(str(make_fake_claude())), _spec(workspace, mode=Mode.EDIT))

        assert not (workspace / ".fake_claude_call.json").exists()

    def test_the_gate_token_does_not_appear_when_the_spec_is_printed(self) -> None:
        assert "run-token-xyz" not in repr(GateAccess("http://x", "run-token-xyz"))


class TestWhatItReports:
    async def test_a_finished_plan_is_the_agents_answer_with_its_cost_and_the_session(
        self, make_fake_claude, workspace
    ) -> None:
        outcome, events = await _run(ClaudeBackend(str(make_fake_claude())), _spec(workspace))

        assert outcome.ok and outcome.failure is None
        assert "calc.py" in outcome.text and outcome.session_id == SESSION
        assert outcome.cost_usd > 0 and outcome.turns == 4
        kinds = [event.kind for event in events]
        assert kinds[0] is EventKind.STARTED and EventKind.INIT in kinds and EventKind.TEXT in kinds
        used = [event.data["tool"] for event in events if event.kind is EventKind.TOOL_USE]
        assert used == ["Bash", "Read", "Read"]

    async def test_the_pid_is_reported_so_a_crash_can_find_the_process(self, make_fake_claude, workspace) -> None:
        _, events = await _run(ClaudeBackend(str(make_fake_claude())), _spec(workspace))

        started = events[0]
        assert started.kind is EventKind.STARTED and started.data["pid"] > 0

    async def test_what_the_agent_was_refused_is_reported(self, make_fake_claude, workspace) -> None:
        outcome, events = await _run(ClaudeBackend(str(make_fake_claude(fixture="denials"))), _spec(workspace))

        assert [denial.tool for denial in outcome.denials] == ["Read", "Read", "Bash", "Bash"]
        assert any("secret.txt" in denial.detail for denial in outcome.denials)
        assert any(e.kind is EventKind.TOOL_RESULT and e.data["is_error"] for e in events)


class TestTokens:
    def test_prompt_tokens_count_what_the_provider_served_from_its_cache(self) -> None:
        from coding_agents.claude import _tokens

        usage = {
            "input_tokens": 10,
            "cache_creation_input_tokens": 200,
            "cache_read_input_tokens": 3000,
            "output_tokens": 45,
        }

        assert _tokens({"usage": usage}) == (3210, 45)

    def test_a_result_with_no_usage_counts_nothing(self) -> None:
        from coding_agents.claude import _tokens

        assert _tokens({}) == (0, 0) and _tokens({"usage": "garbage"}) == (0, 0)


class TestWhyARunFails:
    @pytest.mark.parametrize(
        ("fixture", "exit_code", "failure"),
        [
            ("bad_model", 1, FailureKind.CONFIG),
            ("bad_resume", 1, FailureKind.SESSION_LOST),
            ("max_turns", 1, FailureKind.LIMIT),
            ("rate_limited_synthetic", 1, FailureKind.RESOURCE),
        ],
    )
    async def test_each_real_failure_is_told_apart(
        self, make_fake_claude, workspace, fixture, exit_code, failure
    ) -> None:
        fake = make_fake_claude(fixture=fixture, exit_code=exit_code)

        outcome, _ = await _run(ClaudeBackend(str(fake)), _spec(workspace))

        assert not outcome.ok and outcome.failure is failure and outcome.error

    async def test_a_lost_session_is_reported_as_lost_not_started_over(self, make_fake_claude, workspace) -> None:
        outcome, _ = await _run(
            ClaudeBackend(str(make_fake_claude(fixture="bad_resume", exit_code=1))), _spec(workspace, resume=True)
        )

        assert outcome.failure is FailureKind.SESSION_LOST and "No conversation found" in outcome.error

    async def test_a_crash_with_no_result_reports_what_it_wrote_to_stderr(self, make_fake_claude, workspace) -> None:
        fake = make_fake_claude(fixture="empty", exit_code=3, stderr="segfault-ish")

        outcome, _ = await _run(ClaudeBackend(str(fake)), _spec(workspace))

        assert outcome.failure is FailureKind.ERROR and "segfault-ish" in outcome.error


class TestStopping:
    async def test_a_run_that_never_ends_is_stopped_at_the_time_limit(
        self, make_fake_claude, workspace, monkeypatch
    ) -> None:
        monkeypatch.setattr("coding_agents.claude.RUN_TIMEOUT_SECONDS", 0.5)

        outcome, _ = await _run(ClaudeBackend(str(make_fake_claude(hang=True))), _spec(workspace))

        assert outcome.failure is FailureKind.LIMIT and "did not finish" in outcome.error
        assert not _alive(_call(workspace)["pid"])

    async def test_cancelling_the_run_stops_the_agent(self, make_fake_claude, workspace) -> None:
        task = asyncio.create_task(_run(ClaudeBackend(str(make_fake_claude(hang=True))), _spec(workspace)))
        for _ in range(100):
            if (workspace / ".fake_claude_call.json").exists():
                break
            await asyncio.sleep(0.05)
        pid = _call(workspace)["pid"]

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert not _alive(pid)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
