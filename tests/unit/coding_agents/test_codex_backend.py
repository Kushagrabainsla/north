"""The Codex backend against a fake `codex app-server` that replays what the real one sent."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from coding_agents import (
    CodexBackend,
    CodingAgentError,
    EventKind,
    FailureKind,
    Mode,
    RunEvent,
    RunSpec,
)
from coding_agents.models import AskAccess, GateAccess

SESSION = "11111111-2222-4333-8444-555555555555"
ALLOW = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}
DENY = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny"}}


class GateServer:
    """A throwaway gate: answers every question the same way, and remembers what it was asked."""

    def __init__(self, answer: dict | None = None) -> None:
        self.asked: list[tuple[str | None, dict]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.asked.append((self.headers.get("X-Gate-Token"), body))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(answer or ALLOW).encode())

            def log_message(self, *args) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}/gate"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def gate_server():
    servers: list[GateServer] = []

    def start(answer: dict | None = None) -> GateServer:
        servers.append(GateServer(answer))
        return servers[-1]

    yield start
    for server in servers:
        server.close()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    directory = tmp_path / "repo"
    directory.mkdir()
    return directory


def _spec(workspace: Path, **overrides) -> RunSpec:
    return RunSpec(task="Add a subtract function.", workspace=str(workspace), session_id=SESSION, **overrides)


def _edit(workspace: Path, url: str = "http://127.0.0.1:9/gate") -> RunSpec:
    return _spec(workspace, mode=Mode.EDIT, gate=GateAccess(url, "run-token-xyz"))


async def _run(backend: CodexBackend, spec: RunSpec):
    events: list[RunEvent] = []

    async def sink(event: RunEvent) -> None:
        events.append(event)

    return await backend.run(spec, sink), events


def _calls(workspace: Path) -> dict:
    return json.loads((workspace / ".fake_codex_calls.json").read_text())


def _sent(workspace: Path, method: str) -> list[dict]:
    return [m for m in _calls(workspace)["messages"] if m.get("method") == method]


def _replies(workspace: Path) -> list[dict]:
    """What north answered to the requests the server made of it."""
    return [
        m for m in _calls(workspace)["messages"] if "method" not in m and "id" in m and "result" in m or "error" in m
    ]


class TestProbe:
    async def test_an_installed_logged_in_codex_that_speaks_the_protocol_is_available(self, make_fake_codex) -> None:
        availability = await CodexBackend(str(make_fake_codex())).probe()

        assert availability.available and availability.version == "0.159.2"

    async def test_a_codex_that_is_not_installed_says_so(self) -> None:
        availability = await CodexBackend("codex-that-does-not-exist").probe()

        assert not availability.available and "not installed" in availability.reason

    async def test_a_codex_too_old_is_refused(self, make_fake_codex) -> None:
        availability = await CodexBackend(str(make_fake_codex(version="codex-cli 0.100.0"))).probe()

        assert not availability.available and "too old" in availability.reason

    async def test_a_logged_out_codex_says_how_to_log_in(self, make_fake_codex) -> None:
        availability = await CodexBackend(str(make_fake_codex(login="Not logged in"))).probe()

        assert not availability.available and "codex login" in availability.reason

    @pytest.mark.parametrize("missing", ["item/fileChange/requestApproval", "thread/resume", "turn/interrupt"])
    async def test_a_codex_whose_protocol_lost_a_method_north_uses_is_refused(self, make_fake_codex, missing) -> None:
        availability = await CodexBackend(str(make_fake_codex(schema_lacks=[missing]))).probe()

        assert not availability.available and "protocol has changed" in availability.reason


class TestHowItIsStarted:
    async def test_the_profile_confines_the_agent_and_no_sandbox_is_passed_to_override_it(
        self, make_fake_codex, workspace
    ) -> None:
        backend = CodexBackend(str(make_fake_codex()), protected_paths=["/custom/north-home"])

        await _run(backend, _edit(workspace))
        params = _sent(workspace, "thread/start")[0]["params"]

        assert "sandbox" not in params, "an explicit sandbox overrides the profile and the deny-read stops working"
        assert (params["approvalPolicy"], params["cwd"]) == ("on-request", str(workspace.resolve()))
        profile = params["config"]
        assert profile["default_permissions"] == "northworker"
        permissions = profile["permissions"]["northworker"]
        assert permissions["extends"] == ":workspace"
        rules = permissions["filesystem"]
        home = Path.home()
        denied = {path for path, access in rules.items() if access == "deny"}
        assert {str(home / ".north"), str(home / ".ssh"), str(home / ".claude"), "/custom/north-home"} <= denied
        assert rules[str(home / ".codex")] == "write", "it needs its own login"
        assert rules[str(workspace.resolve())] == "write"

    async def test_plan_mode_is_a_read_only_profile(self, make_fake_codex, workspace) -> None:
        await _run(CodexBackend(str(make_fake_codex())), _spec(workspace))

        params = _sent(workspace, "thread/start")[0]["params"]
        assert params["config"]["permissions"]["northworker"]["extends"] == ":read-only"

    async def test_the_guidance_and_model_go_in_as_instructions_and_a_setting(self, make_fake_codex, workspace) -> None:
        spec = _spec(workspace, guidance="Use type hints.", model="gpt-test")

        await _run(CodexBackend(str(make_fake_codex())), spec)
        params = _sent(workspace, "thread/start")[0]["params"]

        assert (params["developerInstructions"], params["model"]) == ("Use type hints.", "gpt-test")

    async def test_the_task_is_the_turn_s_input_and_noisy_notifications_are_switched_off(
        self, make_fake_codex, workspace
    ) -> None:
        await _run(CodexBackend(str(make_fake_codex())), _spec(workspace))

        turn = _sent(workspace, "turn/start")[0]["params"]
        assert turn["input"] == [{"type": "text", "text": "Add a subtract function."}]
        capabilities = _sent(workspace, "initialize")[0]["params"]["capabilities"]
        assert "item/agentMessage/delta" in capabilities["optOutNotificationMethods"]

    async def test_resuming_asks_for_the_same_thread_instead_of_a_new_one(self, make_fake_codex, workspace) -> None:
        await _run(CodexBackend(str(make_fake_codex())), _spec(workspace, resume=True))

        assert _sent(workspace, "thread/resume")[0]["params"]["threadId"] == SESSION
        assert not _sent(workspace, "thread/start")

    async def test_it_starts_with_a_narrow_environment_and_none_of_norths_secrets(
        self, make_fake_codex, workspace, monkeypatch
    ) -> None:
        monkeypatch.setenv("NORTH_SECRET", "s3cret")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-other")
        monkeypatch.setenv("HOME", os.environ["HOME"])

        await _run(CodexBackend(str(make_fake_codex())), _spec(workspace))
        names = set(_calls(workspace)["env"])

        assert "NORTH_SECRET" not in names and "ANTHROPIC_API_KEY" not in names and "HOME" in names

    async def test_an_edit_run_without_the_gate_is_refused_before_anything_starts(
        self, make_fake_codex, workspace
    ) -> None:
        with pytest.raises(CodingAgentError, match="needs the gate"):
            await _run(CodexBackend(str(make_fake_codex())), _spec(workspace, mode=Mode.EDIT))

        assert not (workspace / ".fake_codex_calls.json").exists()


class TestWhatItReports:
    async def test_a_finished_edit_is_the_final_answer_with_the_threads_id_and_the_tokens_used(
        self, make_fake_codex, workspace
    ) -> None:
        outcome, events = await _run(CodexBackend(str(make_fake_codex(fixture="codex_edit"))), _edit(workspace))

        assert outcome.ok and outcome.failure is None and outcome.text == "DONE"
        assert outcome.session_id == "THREAD-1", "the agent's own thread, not the id north made up"
        assert outcome.tokens_in > 0 and outcome.tokens_out > 0
        kinds = [event.kind for event in events]
        assert kinds[:2] == [EventKind.STARTED, EventKind.INIT] and EventKind.TEXT in kinds
        used = [event.data["tool"] for event in events if event.kind is EventKind.TOOL_USE]
        assert used[:2] == ["Bash", "Edit"], "it looked at the file, then edited it"

    async def test_the_thread_id_is_reported_as_soon_as_the_agent_has_one(self, make_fake_codex, workspace) -> None:
        _, events = await _run(CodexBackend(str(make_fake_codex())), _spec(workspace))

        init = next(event for event in events if event.kind is EventKind.INIT)
        assert init.data["session_id"] == "THREAD-1"

    async def test_the_pid_is_reported_so_a_crash_can_find_the_process(self, make_fake_codex, workspace) -> None:
        _, events = await _run(CodexBackend(str(make_fake_codex())), _spec(workspace))

        assert events[0].kind is EventKind.STARTED and events[0].data["pid"] > 0


class TestWhenTheAgentAsksForMore:
    """Under a profile, anything the sandbox refuses arrives as a request north must answer."""

    async def test_plan_mode_declines_every_request_and_lists_what_was_refused(
        self, make_fake_codex, workspace
    ) -> None:
        outcome, events = await _run(CodexBackend(str(make_fake_codex(fixture="codex_approvals"))), _spec(workspace))

        decisions = [reply["result"]["decision"] for reply in _replies(workspace)]
        assert decisions == ["decline", "decline", "decline"]
        assert [d.tool for d in outcome.denials] == ["Write", "Write", "Bash"]
        assert any("hello.txt" in d.detail for d in outcome.denials)
        assert any(e.kind is EventKind.TOOL_RESULT and e.data["is_error"] for e in events)

    async def test_edit_mode_asks_north_s_gate_with_the_token_and_the_real_paths(
        self, make_fake_codex, workspace, gate_server
    ) -> None:
        server = gate_server(ALLOW)

        outcome, _ = await _run(
            CodexBackend(str(make_fake_codex(fixture="codex_approvals"))), _edit(workspace, server.url)
        )

        assert [r["result"]["decision"] for r in _replies(workspace)] == ["accept", "accept", "accept"]
        assert not outcome.denials
        assert {token for token, _ in server.asked} == {"run-token-xyz"}
        tools = [body["tool_name"] for _, body in server.asked]
        assert tools == ["Write", "Write", "Bash"]
        first = server.asked[0][1]
        assert first["hook_event_name"] == "PreToolUse" and first["tool_input"]["file_path"].endswith("hello.txt")
        assert "printf" in server.asked[2][1]["tool_input"]["command"]

    async def test_a_denial_from_the_gate_is_a_decline(self, make_fake_codex, workspace, gate_server) -> None:
        server = gate_server(DENY)

        outcome, _ = await _run(
            CodexBackend(str(make_fake_codex(fixture="codex_approvals"))), _edit(workspace, server.url)
        )

        assert {r["result"]["decision"] for r in _replies(workspace)} == {"decline"}
        assert len(outcome.denials) == 3

    async def test_a_pass_is_a_decline_because_the_agent_already_said_its_own_rules_need_an_answer(
        self, make_fake_codex, workspace, gate_server
    ) -> None:
        server = gate_server({"pass": True})

        await _run(CodexBackend(str(make_fake_codex(fixture="codex_approvals"))), _edit(workspace, server.url))

        assert {r["result"]["decision"] for r in _replies(workspace)} == {"decline"}

    async def test_an_unreachable_gate_declines_everything(self, make_fake_codex, workspace) -> None:
        outcome, _ = await _run(CodexBackend(str(make_fake_codex(fixture="codex_approvals"))), _edit(workspace))

        assert {r["result"]["decision"] for r in _replies(workspace)} == {"decline"}
        assert outcome.denials


class TestWhyARunFails:
    @pytest.mark.parametrize(
        ("fixture", "failure"),
        [("codex_bad_model", FailureKind.CONFIG), ("codex_usage_limit_synthetic", FailureKind.RESOURCE)],
    )
    async def test_each_failure_is_told_apart(self, make_fake_codex, workspace, fixture, failure) -> None:
        outcome, _ = await _run(CodexBackend(str(make_fake_codex(fixture=fixture))), _spec(workspace))

        assert not outcome.ok and outcome.failure is failure and outcome.error

    async def test_the_providers_own_message_is_unwrapped(self, make_fake_codex, workspace) -> None:
        outcome, _ = await _run(CodexBackend(str(make_fake_codex(fixture="codex_bad_model"))), _spec(workspace))

        assert "not supported when using Codex with a ChatGPT account" in outcome.error
        assert not outcome.error.startswith("{")

    async def test_a_thread_that_is_gone_is_reported_as_lost_not_started_over(self, make_fake_codex, workspace) -> None:
        fake = make_fake_codex(resume_error="thread not found: THREAD-1")

        outcome, _ = await _run(CodexBackend(str(fake)), _spec(workspace, resume=True))

        assert outcome.failure is FailureKind.SESSION_LOST and not _sent(workspace, "thread/start")

    async def test_a_server_that_exits_before_the_turn_finishes_is_an_error(self, make_fake_codex, workspace) -> None:
        (Path(make_fake_codex()).parent / "codex_short.jsonl").write_text("")
        fake = make_fake_codex(fixture="codex_short")
        # point the fixtures directory at where the empty script is
        (Path(fake).parent / "codex.json").write_text(
            json.dumps({"fixtures_dir": str(Path(fake).parent), "fixture": "codex_short"})
        )

        outcome, _ = await _run(CodexBackend(str(fake)), _spec(workspace))

        assert not outcome.ok and outcome.failure is FailureKind.ERROR


class TestStopping:
    async def test_cancelling_asks_the_agent_to_stop_the_turn_and_ends_the_process(
        self, make_fake_codex, workspace
    ) -> None:
        backend = CodexBackend(str(make_fake_codex(fixture="codex_edit", hang=True)))
        task = asyncio.create_task(_run(backend, _spec(workspace)))
        for _ in range(200):
            if (workspace / ".fake_codex_calls.json").exists() and _sent(workspace, "turn/start"):
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)
        pid = _calls(workspace)["pid"]

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert _sent(workspace, "turn/interrupt")[0]["params"] == {"threadId": "THREAD-1", "turnId": "TURN-1"}
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    async def test_a_run_that_never_ends_is_stopped_at_the_time_limit(
        self, make_fake_codex, workspace, monkeypatch
    ) -> None:
        monkeypatch.setattr("coding_agents.codex.RUN_TIMEOUT_SECONDS", 1.0)

        outcome, _ = await _run(CodexBackend(str(make_fake_codex(fixture="codex_edit", hang=True))), _spec(workspace))

        assert outcome.failure is FailureKind.LIMIT and "did not finish" in outcome.error


class TestAskingNorth:
    ASK = AskAccess("http://127.0.0.1:8000/orchestrator/coding/ask", "ask-token-xyz")

    async def _config(self, make_fake_codex, workspace, **spec) -> dict:
        await _run(CodexBackend(str(make_fake_codex())), _spec(workspace, **spec))
        return _sent(workspace, "thread/start")[0]["params"]["config"]

    async def test_a_run_that_may_ask_gets_north_as_its_only_mcp_server_with_that_one_tool_approved(
        self, make_fake_codex, workspace
    ) -> None:
        config = await self._config(make_fake_codex, workspace, ask=self.ASK)

        assert list(config["mcp_servers"]) == ["north"]
        server = config["mcp_servers"]["north"]
        assert server["url"] == self.ASK.url
        assert server["http_headers"] == {"Authorization": "Bearer ask-token-xyz"}
        assert server["tools"] == {"ask_north": {"approval_mode": "approve"}}

    async def test_the_token_is_in_the_config_message_not_the_environment_or_the_command_line(
        self, make_fake_codex, workspace
    ) -> None:
        await _run(CodexBackend(str(make_fake_codex())), _spec(workspace, ask=self.ASK))
        call = _calls(workspace)

        assert "ask-token-xyz" not in " ".join(call["argv"])
        assert "NORTH_ASK_TOKEN" not in call["env"]

    async def test_the_confinement_profile_is_still_there_beside_it(self, make_fake_codex, workspace) -> None:
        config = await self._config(make_fake_codex, workspace, ask=self.ASK)

        assert config["default_permissions"] == "northworker" and "mcp_servers" in config

    async def test_without_the_door_there_is_no_mcp_server(self, make_fake_codex, workspace) -> None:
        config = await self._config(make_fake_codex, workspace)

        assert "mcp_servers" not in config
