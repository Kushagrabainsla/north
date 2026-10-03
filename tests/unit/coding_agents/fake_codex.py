#!/usr/bin/env python3
"""A stand-in for `codex` that replays recorded app-server traffic.

Copied next to a `codex.json` config by `make_fake_codex`. It answers `--version`, `login status` and
`app-server generate-json-schema`, and as `app-server` speaks JSON-RPC over stdio: it answers the client's
requests, then plays a fixture - messages to emit, and points where it waits for the client's reply to a
request it made. Everything the client sent is written to `.fake_codex_calls.json`.
"""

import json
import os
import sys
import time
from pathlib import Path

here = Path(__file__).resolve()
config_path = here.with_suffix(".json")
config = json.loads(config_path.read_text()) if config_path.exists() else {}
args = sys.argv[1:]

if args[:1] == ["--version"]:
    print(config.get("version", "codex-cli 0.159.2"))
    sys.exit(0)
if args[:2] == ["login", "status"]:
    print(config.get("login", "Logged in using ChatGPT"))
    sys.exit(0)
if args[:2] == ["app-server", "generate-json-schema"]:
    out = Path(args[args.index("--out") + 1])
    client = ["thread/start", "thread/resume", "turn/start", "turn/interrupt"]
    server = ["item/commandExecution/requestApproval", "item/fileChange/requestApproval"]
    for name in config.get("schema_lacks", []):
        client = [n for n in client if n != name]
        server = [n for n in server if n != name]
    (out / "ClientRequest.json").write_text(json.dumps(client))
    (out / "ServerRequest.json").write_text(json.dumps(server))
    sys.exit(0)
if args[:1] != ["app-server"]:
    sys.exit(2)

received: list = []
env = sorted(os.environ)


def save() -> None:
    """Write the call log atomically: the backend may stop this process mid-write, and a reader must never
    see a half-written file."""
    temporary = Path(".fake_codex_calls.json.tmp")
    temporary.write_text(json.dumps({"messages": received, "env": env, "cwd": os.getcwd(), "pid": os.getpid()}))
    os.replace(temporary, ".fake_codex_calls.json")


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def swap(value):
    if isinstance(value, dict):
        return {k: swap(v) for k, v in value.items()}
    if isinstance(value, list):
        return [swap(v) for v in value]
    if isinstance(value, str):
        return value.replace("THREAD-1", thread_id).replace("TURN-1", "TURN-1").replace("/work/repo", os.getcwd())
    return value


thread_id = config.get("thread_id", "THREAD-1")
fixture = Path(config["fixtures_dir"]) / (config.get("fixture", "codex_edit") + ".jsonl")
steps = [json.loads(line) for line in fixture.read_text().splitlines() if line.strip()]
turn_started = False


def read() -> dict | None:
    line = sys.stdin.readline()
    return json.loads(line) if line else None


def play() -> None:
    for step in steps:
        if "emit" in step:
            if config.get("hang") and step["emit"].get("method") == "turn/completed":
                continue  # a run that never finishes
            send(swap(step["emit"]))
        elif "await" in step:
            while True:
                message = read()
                if message is None:
                    return
                received.append(message)
                save()
                if message.get("id") == step["await"] and "method" not in message:
                    break
    if config.get("hang"):
        while True:
            message = read()
            if message is None:
                return
            received.append(message)
            save()
            if message.get("method") == "turn/interrupt":
                send({"id": message["id"], "result": {}})
                send(
                    {
                        "method": "turn/completed",
                        "params": {"threadId": thread_id, "turn": {"id": "TURN-1", "status": "interrupted"}},
                    }
                )
                return


while (message := read()) is not None:
    received.append(message)
    save()
    method = message.get("method")
    if method == "initialize":
        send({"id": message["id"], "result": {"userAgent": "fake"}})
    elif method in ("thread/start", "thread/resume"):
        if method == "thread/resume" and config.get("resume_error"):
            send({"id": message["id"], "error": {"code": -32600, "message": config["resume_error"]}})
            continue
        send({"id": message["id"], "result": {"thread": {"id": thread_id}, "model": "gpt-test"}})
    elif method == "turn/start":
        send({"id": message["id"], "result": {"turn": {"id": "TURN-1", "status": "inProgress"}}})
        play()
        save()
        time.sleep(0.1)
        break
