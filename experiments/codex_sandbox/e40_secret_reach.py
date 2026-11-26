"""#40 before/after: can a command north runs, or a coding agent it starts, get north's credentials?

Usage: e40_secret_reach.py <label> <checkout>

Canary values go into this process's environment under north's own credential names, as the dashboard
does when you save a provider key or the Telegram token, and as `NORTH_SECRET` is set in Docker. Then,
with *checkout*'s own code:

- the bash tool's environment and macOS sandbox run `env` and `cat ~/.north/secret.key`, in the box a
  read-only command runs in without a card and in the box an approved command runs in;
- a coding agent's environment is built as the backends build it.

A canary in the output means the credential reached the command. Nothing is sent anywhere, and the
secret file is never printed: only whether `cat` was refused. The results hold counts only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

LABEL, CHECKOUT = sys.argv[1], Path(sys.argv[2]).resolve()
sys.path.insert(0, str(CHECKOUT))

TAG = uuid.uuid4().hex[:12]
CANARIES = {
    "NORTH_ANTHROPIC_API_KEY": f"canary-provider-{TAG}",  # saved from the dashboard's Providers page
    "NORTH_TELEGRAM_BOT_TOKEN": f"canary-telegram-{TAG}",  # saved from the dashboard's Telegram page
    "NORTH_SECRET": f"canary-secret-{TAG}",  # the shared secret, when it is set in the environment
}
os.environ.update(CANARIES)

from coding_agents.environment import agent_environment  # noqa: E402
from tools.specialized import _os_sandbox  # noqa: E402
from tools.specialized.bash import _environment  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"


def run(command: str, workspace: str, *, writable: bool) -> str:
    box = _os_sandbox.current()
    assert box is not None, "needs the macOS sandbox"
    argv = box.wrap(command, workspace, writable=writable)
    done = subprocess.run(argv, capture_output=True, text=True, env=_environment(), cwd=workspace, timeout=60)
    return done.stdout + done.stderr


def main() -> None:
    rows = []
    with tempfile.TemporaryDirectory(prefix="north-e40-") as workspace:
        for box, writable in (("read-only, no card", False), ("approved command", True)):
            seen = run("env", workspace, writable=writable)
            rows.append(
                {
                    "box": box,
                    "credentials_in_env": sum(value in seen for value in CANARIES.values()),
                    "of": len(CANARIES),
                }
            )
            key_file = run("cat ~/.north/secret.key > /dev/null && echo READ", workspace, writable=writable)
            rows[-1]["secret_key_file_read"] = "READ" in key_file
    agent = agent_environment()
    result = {
        "label": LABEL,
        "commands": rows,
        "coding_agent_env_credentials": sum(value in agent.values() for value in CANARIES.values()),
    }
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"e40_secret_reach_{LABEL}.json").write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
