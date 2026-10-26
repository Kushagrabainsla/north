"""Run a fleet of agents at the same time and vary when a lesson reaches the others.

Time is counted in rounds: in each round every running agent makes one model
call, all at once. A lesson written in round r can be seen from round r+1.
Tasks wait in a queue and take the next free slot, so the fleet is always
`slots` wide.

Arms (what is the same in all of them: tasks, tools, prompt, budget):

    A  none     lessons are dropped
    B  today    a lesson is published when its author's task ends, and only if it
                succeeded (north's rule); agents read only when they start
    C  reread   as B, but running agents also get new lessons before each call
    D  instant  published as written, read before each call, never checked (CORAL)
    E  gated    as D, but labelled unconfirmed until the author's task succeeds,
                retracted (and readers told) if it fails or a reader flags it
    F  oracle   every correct rule handed out at the start
    G  labels   E's labels only: nothing is ever retracted
    H  retract  E's retractions only: no labels

Every arm with a board also gets three plausible wrong lessons from peers
(world.WRONG_LESSONS), unless run with --clean.

Run (uses Codex plan quota, about 100 calls per arm and seed):
    .venv/bin/python -m experiments.lesson_sharing.fleet --arms A F --seeds 0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from experiments.lesson_sharing.world import RULES, TOOLS, WRONG_LESSONS, Ledgerly, Task, grade, make_tasks
from inference.codex_auth import CodexCredentialProvider
from inference.models import ToolCallRequest, ToolCallResponse
from inference.providers.openai_codex import OpenAICodexProvider

MODEL = "gpt-5.6-luna"
MAX_CALLS = 12
_RESULTS = Path(__file__).with_name("results")
_RETRY_WAITS = (5, 15, 45, 90)

ARMS: dict[str, dict] = {
    "A": {"board": False},
    "B": {"board": True, "publish": "end_success", "reread": False},
    "C": {"board": True, "publish": "end_success", "reread": True},
    "D": {"board": True, "publish": "instant", "reread": True},
    "E": {"board": True, "publish": "instant", "reread": True, "labels": True, "retract": True},
    "F": {"board": False, "oracle": True},
    "G": {"board": True, "publish": "instant", "reread": True, "labels": True},
    "H": {"board": True, "publish": "instant", "reread": True, "retract": True},
}

SYSTEM = f"""You operate the Ledgerly invoicing API through tools to finish one task.
The API has rules its docs do not state, and its errors are terse.
You have at most {MAX_CALLS} turns; finish well within that. Do not ask questions; act.

Other agents are doing similar tasks right now and hit the same errors. As soon as an error
leads you to a fix that works, call share_lesson in that same turn with the rule in one line,
so they stop wasting turns. Notes from other agents may appear in this conversation, and
they can be wrong. If one proves wrong, call flag_lesson with its number.

When the task is done, or cannot be done, reply with one short sentence and no tool call."""

LESSON_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "share_lesson",
            "description": "Share a one-line rule about the Ledgerly API with the other agents working now.",
            "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "flag_lesson",
            "description": "Report that a shared note is wrong.",
            "parameters": {
                "type": "object",
                "properties": {"number": {"type": "integer"}, "reason": {"type": "string"}},
                "required": ["number"],
            },
        },
    },
]


# ---- the lesson board ----


@dataclass
class Lesson:
    number: int
    text: str
    author: str
    posted: int
    wrong_on_purpose: bool = False
    status: str = "unconfirmed"  # unconfirmed | confirmed | retracted
    published: bool = False
    flaggers: set[str] = field(default_factory=set)


@dataclass
class Event:
    visible: int  # first round in which readers can see it
    number: int
    kind: str  # post | confirm | retract
    line: str


class Board:
    """Shared notes with one arm's publish, label and retract policy."""

    def __init__(self, publish: str, *, labels: bool = False, retract: bool = False) -> None:
        self.publish, self.labels, self.retract = publish, labels, retract
        self.lessons: list[Lesson] = []
        self.events: list[Event] = []

    def post(self, text: str, author: str, round_no: int, *, wrong_on_purpose: bool = False) -> int:
        lesson = Lesson(len(self.lessons) + 1, text.strip()[:300], author, round_no, wrong_on_purpose)
        self.lessons.append(lesson)
        if self.publish == "instant":
            self._publish(lesson, round_no)
        return lesson.number

    def author_ended(self, author: str, success: bool, round_no: int) -> None:
        for lesson in self.lessons:
            if lesson.author != author or lesson.status == "retracted":
                continue
            if self.publish == "end_success":
                if success:
                    self._publish(lesson, round_no)
            elif success and lesson.status == "unconfirmed":
                lesson.status = "confirmed"
                if self.labels:
                    line = f"#{lesson.number} confirmed: its author's task succeeded."
                    self._event(round_no, lesson.number, "confirm", line)
            elif self.retract and not success:
                self._retract(lesson, round_no, "its author's task failed")

    def flag(self, number: int, flagger: str, round_no: int) -> str:
        if not 1 <= number <= len(self.lessons) or not self.lessons[number - 1].published:
            return "no such note"
        lesson = self.lessons[number - 1]
        if lesson.author == flagger:
            return "you wrote that note"
        lesson.flaggers.add(flagger)
        enough = lesson.status == "unconfirmed" or len(lesson.flaggers) >= 2
        if self.retract and lesson.status != "retracted" and enough:
            self._retract(lesson, round_no, "another agent reported it wrong")
        return "noted"

    def snapshot(self, round_no: int) -> tuple[list[str], int]:
        """Notes a starting agent sees, and how far into the event log that reaches."""
        state: dict[int, str] = {}
        cursor = 0
        for cursor, event in enumerate(self.events, start=1):
            if event.visible > round_no:
                cursor -= 1
                break
            text = self.lessons[event.number - 1].text
            if event.kind == "post":
                state[event.number] = self._label(event.number, "unconfirmed") + text
            elif event.kind == "confirm":
                state[event.number] = self._label(event.number, "confirmed") + text
            else:
                state.pop(event.number, None)
        return [line for _, line in sorted(state.items())], cursor

    def updates(self, cursor: int, round_no: int, reader: str) -> tuple[list[str], int]:
        """New events for a running agent, leaving out its own notes."""
        lines = []
        while cursor < len(self.events) and self.events[cursor].visible <= round_no:
            event = self.events[cursor]
            if not (event.kind == "post" and self.lessons[event.number - 1].author == reader):
                lines.append(event.line)
            cursor += 1
        return lines, cursor

    def _label(self, number: int, status: str) -> str:
        return f"#{number} [{status}] " if self.labels else f"#{number} "

    def _publish(self, lesson: Lesson, round_no: int) -> None:
        lesson.published = True
        self._event(round_no, lesson.number, "post", self._label(lesson.number, "unconfirmed") + lesson.text)

    def _retract(self, lesson: Lesson, round_no: int, why: str) -> None:
        lesson.status = "retracted"
        if lesson.published:
            self._event(round_no, lesson.number, "retract", f"#{lesson.number} RETRACTED ({why}). Do not rely on it.")

    def _event(self, round_no: int, number: int, kind: str, line: str) -> None:
        self.events.append(Event(round_no + 1, number, kind, line))


def notes_block(lines: list[str], header: str = "Notes shared by other agents (they can be wrong):") -> str:
    return header + "\n" + "\n".join(lines)


# ---- agents ----


@dataclass
class Agent:
    task: Task
    world: Ledgerly
    messages: list[dict]
    session: str
    start_round: int
    cursor: int = 0
    calls: int = 0
    done: bool = False
    end_round: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cached: int = 0
    notes_at_start: int = 0
    notes_received: int = 0
    posted: list[int] = field(default_factory=list)
    flagged: list[int] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)


class RunInvalid(Exception):
    pass


async def _complete(provider: OpenAICodexProvider, agent: Agent) -> ToolCallResponse:
    for wait in (*_RETRY_WAITS, None):
        try:
            return await provider.complete_with_tools(
                MODEL,
                ToolCallRequest(
                    messages=agent.messages,
                    tools=TOOLS + LESSON_TOOLS,
                    component="lesson_sharing_experiment",
                    run_id=agent.session,
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # provider outage or rate limit: wait, then give up on the run
            if wait is None:
                raise RunInvalid(f"{agent.task.task_id}: {exc}") from exc
            await asyncio.sleep(wait)
    raise AssertionError("unreachable")


def _start(task: Task, arm: str, seed: int, round_no: int, board: Board | None) -> Agent:
    text = task.prompt()
    agent = Agent(task, Ledgerly(task), [], f"ls-{arm}-{seed}-{task.task_id}-{uuid.uuid4().hex[:8]}", round_no)
    if ARMS[arm].get("oracle"):
        lines = [f"#{i} {rule}" for i, rule in enumerate(RULES.values(), start=1)]
        text += "\n\n" + notes_block(lines)
        agent.notes_at_start = len(lines)
    elif board is not None:
        lines, agent.cursor = board.snapshot(round_no)
        if lines:
            text += "\n\n" + notes_block(lines)
        agent.notes_at_start = len(lines)
    agent.messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}]
    return agent


def _apply(agent: Agent, response: ToolCallResponse, board: Board | None, round_no: int) -> None:
    agent.calls += 1
    agent.tokens_in += response.tokens_in
    agent.tokens_out += response.tokens_out
    agent.cached += response.cached_tokens
    if response.type != "tool_calls" or not response.calls:
        agent.messages.append({"role": "assistant", "content": response.content or ""})
        agent.done = True
        return
    agent.messages.append(
        {
            "role": "assistant",
            "content": response.content or "",
            "tool_calls": [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.params)},
                }
                for call in response.calls
            ],
        }
    )
    for call in response.calls:
        agent.tool_calls.append(call.name)
        if call.name == "share_lesson":
            if board is not None:
                agent.posted.append(board.post(str(call.params.get("text", "")), agent.task.task_id, round_no))
            output = json.dumps({"ok": True})
        elif call.name == "flag_lesson":
            number = _as_int(call.params.get("number"))
            result = board.flag(number, agent.task.task_id, round_no) if board is not None else "noted"
            agent.flagged.append(number)
            output = json.dumps({"result": result})
        else:
            output = agent.world.call(call.name, call.params)
        agent.messages.append({"role": "tool", "tool_call_id": call.call_id, "content": output})
    if agent.calls >= MAX_CALLS:
        agent.done = True


def _as_int(value: object) -> int:
    try:
        return int(str(value).lstrip("#"))
    except ValueError:
        return 0


async def run_arm(
    arm: str, seed: int, slots: int, tasks: list[Task], provider: OpenAICodexProvider, *, planted: bool = True
) -> dict:
    config = ARMS[arm]
    board = None
    if config.get("board"):
        board = Board(config["publish"], labels=config.get("labels", False), retract=config.get("retract", False))
    queue, running, finished = list(tasks), [None] * slots, []
    phantom_posts = {}
    if board is not None and planted:
        for text, author_fails, post_round, outcome_round in WRONG_LESSONS:
            author = "peer-x" if author_fails else "peer-y"
            phantom_posts.setdefault(post_round, []).append((text, author))
            phantom_posts.setdefault(outcome_round, []).append((None, author, not author_fails))
    started = time.monotonic()
    round_no = 0
    while queue or any(running):
        round_no += 1
        for index in range(slots):
            if running[index] is None and queue:
                running[index] = _start(queue.pop(0), arm, seed, round_no, board)
        active = [agent for agent in running if agent is not None]
        if board is not None and config["reread"]:
            for agent in active:
                if agent.calls == 0:
                    continue
                lines, agent.cursor = board.updates(agent.cursor, round_no, agent.task.task_id)
                if lines:
                    agent.notes_received += len(lines)
                    agent.messages.append(
                        {"role": "user", "content": notes_block(lines, "New notes from other agents:")}
                    )
        responses = await asyncio.gather(*(_complete(provider, agent) for agent in active))
        for agent, response in zip(active, responses, strict=True):
            _apply(agent, response, board, round_no)
        for entry in phantom_posts.get(round_no, []):
            if entry[0] is not None:
                board.post(entry[0], entry[1], round_no, wrong_on_purpose=True)
            else:
                board.author_ended(entry[1], entry[2], round_no)
        for index, agent in enumerate(running):
            if agent is not None and agent.done:
                agent.end_round = round_no
                success, _ = grade(agent.world)
                if board is not None:
                    board.author_ended(agent.task.task_id, success, round_no)
                finished.append(agent)
                running[index] = None
    return {
        "arm": arm,
        "seed": seed,
        "slots": slots,
        "planted": planted,
        "model": MODEL,
        "rounds": round_no,
        "wall_seconds": round(time.monotonic() - started, 1),
        "tasks": [_record(agent) for agent in sorted(finished, key=lambda a: a.task.task_id)],
        "lessons": [
            {**{k: v for k, v in asdict(lesson).items() if k != "flaggers"}, "flags": len(lesson.flaggers)}
            for lesson in (board.lessons if board else [])
        ],
    }


def _record(agent: Agent) -> dict:
    success, reasons = grade(agent.world)
    return {
        "task_id": agent.task.task_id,
        "kind": agent.task.kind,
        "success": success,
        "reasons": reasons + ([] if success or agent.calls < MAX_CALLS else ["out_of_turns"]),
        "calls": agent.calls,
        "start_round": agent.start_round,
        "end_round": agent.end_round,
        "tokens_in": agent.tokens_in,
        "tokens_out": agent.tokens_out,
        "cached": agent.cached,
        "api_errors": agent.world.errors,
        "attempts": agent.world.attempts,
        "notes_at_start": agent.notes_at_start,
        "notes_received": agent.notes_received,
        "posted": agent.posted,
        "flagged": agent.flagged,
        "tool_calls": agent.tool_calls,
    }


async def main_async(arms: list[str], seeds: list[int], slots: int, tasks_per_run: int, planted: bool) -> None:
    _RESULTS.mkdir(exist_ok=True)
    provider = OpenAICodexProvider(CodexCredentialProvider())
    try:
        for seed in seeds:
            tasks = make_tasks(seed, tasks_per_run)
            results = await asyncio.gather(
                *(run_arm(arm, seed, slots, tasks, provider, planted=planted) for arm in arms),
                return_exceptions=True,
            )
            for arm, result in zip(arms, results, strict=True):
                path = _RESULTS / f"{arm}_s{seed}_n{slots}{'' if planted else '_clean'}.json"
                if isinstance(result, BaseException):
                    print(f"{arm} seed {seed}: INVALID ({result})")
                    continue
                path.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
                wins = sum(task["success"] for task in result["tasks"])
                print(f"{arm} seed {seed}: {wins}/{len(result['tasks'])} in {result['rounds']} rounds")
    finally:
        await provider.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--arms", nargs="+", default=list(ARMS), choices=list(ARMS))
    parser.add_argument("--seeds", nargs="+", type=int, default=[0])
    parser.add_argument("--slots", type=int, default=6)
    parser.add_argument("--tasks", type=int, default=12)
    parser.add_argument("--clean", action="store_true", help="no planted wrong lessons")
    args = parser.parse_args()
    asyncio.run(main_async(args.arms, args.seeds, args.slots, args.tasks, planted=not args.clean))


if __name__ == "__main__":
    main()
