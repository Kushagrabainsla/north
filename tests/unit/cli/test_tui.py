"""Regression tests for the north TUI (cli/tui.py).

These guard two UX bugs found by a headless runtime probe and fixed:
  1. a paste stashed as a placeholder was sent even after the user edited the
     input to a different message (input and action diverged);
  2. Tab on an empty prompt moved focus onto the read-only chat log, stranding
     the user's keystrokes.
Plus a smoke test that the full SSE event lifecycle renders without crashing.

The app is driven via Textual's headless run_test(); all HTTP is best-effort in
the app and caught, so no server is needed (base_url points at a dead port).
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

from cli.tui import NorthApp

_DEAD = "http://127.0.0.1:1"  # nothing listening -> immediate ConnectError, caught
_HEADERS = {"X-North-Secret": "x"}


class _FakeResp:
    def __init__(self, js):
        self._js = js
        self.status_code = 200

    def json(self):
        return self._js

    def raise_for_status(self):
        pass


class _CapturingClient:
    """Records POST bodies so a test can assert what the app *would* send."""

    def __init__(self, sink: list[dict]):
        self.sink = sink

    async def post(self, url, **kw):
        self.sink.append({"url": url, "json": kw.get("json")})
        return _FakeResp({"task_id": "task_test"})

    async def get(self, url, **kw):
        return _FakeResp([])

    async def delete(self, url, **kw):
        return _FakeResp({})


def _install_capturing_http(app: NorthApp) -> list[dict]:
    sink: list[dict] = []

    @contextlib.asynccontextmanager
    async def fake_http():
        yield _CapturingClient(sink)

    app._http = fake_http
    return sink


async def test_full_sse_lifecycle_renders_without_crashing():
    app = NorthApp(base_url=_DEAD, headers=_HEADERS)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        tid = "task_test"
        app._user_task_ids.add(tid)
        app._pending_user_messages[tid] = "hi"
        events = [
            ("classifying", {}),
            ("classified", {"domain": "general", "is_consequential": True}),
            ("routed", {"agents": ["general"]}),
            ("model", {"model": "meta-llama/llama-4-scout-17b:free"}),
            ("agent_started", {"agent": "coder", "task": "do the thing"}),
            ("tool_called", {"tool": "web_search", "params": {"query": "x", "workspace": "/tmp"}}),
            ("tool_result", {"tool": "web_search", "success": True, "formatted": "a\nb"}),
            ("reasoning", {"text": "thinking"}),
            ("token", {"text": "Hello "}),
            ("token", {"text": "world\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"}),
            ("compaction", {}),
            ("task_completed", {"cost_usd": 0.001}),
        ]
        for ev, extra in events:
            await app._handle_event(ev, {"task_id": tid, **extra})
            await pilot.pause()
        # task cleaned up from the active set on completion
        assert tid not in app._user_task_ids


async def test_edited_paste_sends_visible_text_not_stale_paste():
    app = NorthApp(base_url=_DEAD, headers=_HEADERS)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt")
        big = "line1\nline2\nline3\nline4\n" * 3
        app.on_paste(SimpleNamespace(text=big, prevent_default=lambda: None, stop=lambda: None))
        await pilot.pause()
        assert app._pending_paste is not None  # stashed, placeholder shown
        # user overtypes the placeholder with a new message
        prompt.value = "actually never mind"
        sink = _install_capturing_http(app)
        await app.on_input_submitted(SimpleNamespace(value=prompt.value, input=prompt))
        await pilot.pause()
        assert sink, "a task should have been submitted"
        assert sink[-1]["json"]["prompt"].strip() == "actually never mind"
        assert app._pending_paste is None and app._paste_placeholder is None


async def test_untouched_paste_placeholder_still_sends_the_paste():
    app = NorthApp(base_url=_DEAD, headers=_HEADERS)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt")
        big = "alpha\nbeta\ngamma\ndelta\n" * 3
        app.on_paste(SimpleNamespace(text=big, prevent_default=lambda: None, stop=lambda: None))
        await pilot.pause()
        sink = _install_capturing_http(app)
        # submit with the placeholder untouched -> the real paste is sent
        await app.on_input_submitted(SimpleNamespace(value=prompt.value, input=prompt))
        await pilot.pause()
        assert sink and sink[-1]["json"]["prompt"].strip() == big.strip()


async def test_read_only_panes_are_not_focusable():
    app = NorthApp(base_url=_DEAD, headers=_HEADERS)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert app.query_one("#log").can_focus is False
        assert app.query_one("#streaming-wrap").can_focus is False


async def test_tab_on_empty_prompt_keeps_focus_on_input():
    app = NorthApp(base_url=_DEAD, headers=_HEADERS)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt")
        prompt.value = ""
        app.set_focus(prompt)
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "prompt"


async def test_status_bar_renders_on_a_narrow_terminal():
    app = NorthApp(base_url=_DEAD, headers=_HEADERS)
    async with app.run_test(size=(24, 8)) as pilot:
        await pilot.pause()
        app._model = "meta-llama/llama-4-scout-17b:free"
        app._session_tokens = 123456
        app._session_cost = 1.2345
        app._compactions = 3
        app._user_task_ids = {"a", "b"}
        app.yolo = True
        app._render_status_bar()  # must not raise on the width-drop loop
        await pilot.pause()


async def test_live_stream_uses_same_markdown_engine_as_final_message():
    # Guards the streaming->final fix: the live stream renders through the SAME
    # rich Markdown engine used for the finalized message in the log, so tables and
    # headings do not re-flow or change chrome when the stream ends.
    from rich.markdown import Markdown as RichMarkdown

    app = NorthApp(base_url=_DEAD, headers=_HEADERS)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        tid = "task_test"
        app._user_task_ids.add(tid)
        await app._handle_event("token", {"task_id": tid, "text": "# H\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"})
        await pilot.pause()
        stream = app.query_one("#streaming")
        assert isinstance(stream.content, RichMarkdown)  # live == same engine as final
        assert app.query_one("#streaming-wrap").display is True
        await app._handle_event("task_completed", {"task_id": tid, "cost_usd": 0.0})
        await pilot.pause()
        assert app.query_one("#streaming-wrap").display is False  # handed off to the log


async def test_approval_diff_syntax_highlighted():
    app = NorthApp(base_url=_DEAD, headers=_HEADERS)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        tid = "t1"
        app._user_task_ids.add(tid)
        diff_msg = "Apply changes?\n```diff\n--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-old\n+new\n```"
        await app._handle_event(
            "approval_required",
            {"task_id": tid, "card_id": "c1", "message": diff_msg, "options": ["Approve", "Reject"]},
        )
        await pilot.pause()
        assert app._approval_pending is not None
        assert app._pending_card_id == "c1"


async def test_slash_jobs_and_context_and_help():
    app = NorthApp(base_url=_DEAD, headers=_HEADERS)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        # Test /help
        await app._handle_slash("/help")
        await pilot.pause()
        # Test /jobs
        await app._handle_slash("/jobs")
        await pilot.pause()
        # Test /context
        await app._handle_slash("/context")
        await pilot.pause()


async def test_streaming_tok_per_sec_throughput():
    app = NorthApp(base_url=_DEAD, headers=_HEADERS)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        tid = "t_speed"
        app._user_task_ids.add(tid)
        # First token registers start time
        await app._handle_event("token", {"task_id": tid, "text": "hello "})
        await pilot.pause()
        assert tid in app._stream_start_times
        # Subsequent tokens update token count
        await app._handle_event("token", {"task_id": tid, "text": "world this is a live test "})
        await pilot.pause()
        assert app._stream_token_counts[tid] > 0
        # Complete task cleans up speed tracking
        await app._handle_event("task_completed", {"task_id": tid, "cost_usd": 0.0})
        await pilot.pause()
        assert tid not in app._stream_start_times
        assert app._streaming_tok_per_sec == 0.0
        assert app._last_assistant_response != ""


async def test_copy_last_response_action():
    app = NorthApp(base_url=_DEAD, headers=_HEADERS)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        # With no response yet
        app.action_copy_last_response()
        # With a completed response
        app._last_assistant_response = "Here is the response code"
        app.action_copy_last_response()
        await pilot.pause()


async def test_lifecycle_extended_events_render():
    app = NorthApp(base_url=_DEAD, headers=_HEADERS)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        tid = "t_life"
        app._user_task_ids.add(tid)
        events = [
            ("design_phase", {"step": "research"}),
            ("plan_seeded", {"tasks": 4}),
            ("conductor_fix_round", {"round": 1}),
            ("auto_verify_started", {"command": "pytest -q"}),
            ("auto_verify", {"command": "pytest -q", "passed": True}),
            ("dod_evaluated", {"passed": True, "reasons": ["Tests passed"]}),
            ("stream_reset", {}),
        ]
        for ev, extra in events:
            await app._handle_event(ev, {"task_id": tid, **extra})
            await pilot.pause()
