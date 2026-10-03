#!/usr/bin/env python3
"""The PreToolUse hook Claude Code runs before each tool call. Standard library only.

It runs as its own process under whatever Python north runs on, with nothing of north on its path,
so it must import nothing from north. It posts the call to the daemon and prints the answer.

It fails closed: any problem at all exits 2, the only exit code Claude Code treats as a block.
Every other failure (a timeout, exit 1, unreadable output) lets the call through, which is why a
broken hook cannot be left to chance.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request


def _fail(reason: str) -> None:
    sys.stderr.write(f"north gate: {reason}\n")
    sys.exit(2)


def main() -> None:
    try:
        payload = sys.stdin.read()
        request = urllib.request.Request(
            os.environ["NORTH_GATE_URL"],
            data=payload.encode(),
            headers={"X-Gate-Token": os.environ["NORTH_GATE_TOKEN"], "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request) as response:  # no client timeout: an approval waits for a person
            answer = json.loads(response.read().decode())
        if answer == {"pass": True}:
            return  # no decision: Claude Code's own rules apply
        decision = answer["hookSpecificOutput"]["permissionDecision"]
        if decision not in ("allow", "deny"):
            _fail(f"unknown decision {decision!r}")
        sys.stdout.write(json.dumps(answer))
    except SystemExit:
        raise
    except Exception as exc:
        _fail(f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
