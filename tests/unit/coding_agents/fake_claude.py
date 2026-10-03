#!/usr/bin/env python3
"""A stand-in for `claude` that replays recorded stream-json output.

Copied next to a `claude.json` config by `make_fake_claude` in the tests; it answers `--version`
and `auth status`, records how it was called, then replays a fixture with SESSION swapped for the
session id it was given.
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
    print(config.get("version", "2.1.286 (Claude Code)"))
    sys.exit(0)
if args[:2] == ["auth", "status"]:
    logged_in = config.get("logged_in", True)
    print(json.dumps({"loggedIn": logged_in}))
    sys.exit(0 if logged_in else 1)

prompt = sys.stdin.read()
session = next((args[i + 1] for i, arg in enumerate(args) if arg in ("--session-id", "--resume")), "")
Path(".fake_claude_call.json").write_text(
    json.dumps({"argv": args, "stdin": prompt, "env": sorted(os.environ), "cwd": os.getcwd(), "pid": os.getpid()})
)
if config.get("hang"):
    time.sleep(3600)
fixture = Path(config["fixtures_dir"]) / (config.get("fixture", "plan_success") + ".jsonl")
for line in fixture.read_text().splitlines():
    sys.stdout.write(line.replace("SESSION", session) + "\n")
    sys.stdout.flush()
if config.get("stderr"):
    sys.stderr.write(config["stderr"])
sys.exit(config.get("exit_code", 0))
