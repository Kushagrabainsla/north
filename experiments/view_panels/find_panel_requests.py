"""Find the user's own requests that a custom panel could serve, in north's records (read-only).

Reads every distinct chat prompt (web.db) and task prompt (ledger.db), and keeps the ones
that ask to see, track, control or check something more than once: the kind of work a panel
holds. The prompts stay private, in ~/.north/experiments/view_panels/candidates.jsonl; this
prints counts only.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

HOME = Path.home() / ".north"
OUT = HOME / "experiments" / "view_panels"

# Words that ask for something to look at or act on again: a page, a list, a status, a control.
PANEL_WORDS = re.compile(
    r"\b(dashboard|panel|page|view|widget|tracker|track|tracking|monitor|status|chart|graph|list|"
    r"toggle|switch|turn (on|off)|lights?|bulbs?|brightness|show me|how many|summary|progress|"
    r"schedule|remind|budget|expenses?|habit|inbox|queue|board|todo|to-do)\b",
    re.IGNORECASE,
)


def _prompts() -> list[str]:
    seen: dict[str, None] = {}
    with sqlite3.connect(f"file:{HOME / 'web.db'}?mode=ro", uri=True) as conn:
        for (prompt,) in conn.execute("SELECT prompt FROM web_turns"):
            seen.setdefault(" ".join(str(prompt).split()), None)
    with sqlite3.connect(f"file:{HOME / 'ledger.db'}?mode=ro", uri=True) as conn:
        for (prompt,) in conn.execute("SELECT input FROM ledger WHERE action = 'task_received'"):
            seen.setdefault(" ".join(str(prompt or "").split()), None)
    return [prompt for prompt in seen if prompt]


def main() -> None:
    prompts = _prompts()
    candidates = [prompt for prompt in prompts if PANEL_WORDS.search(prompt)]
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "candidates.jsonl").open("w", encoding="utf-8") as handle:
        for prompt in candidates:
            handle.write(json.dumps({"prompt": prompt}) + "\n")
    print(json.dumps({"distinct_prompts": len(prompts), "panel_word_matches": len(candidates)}))


if __name__ == "__main__":
    main()
