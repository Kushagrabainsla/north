"""Before and after for custom panels: which of 10 real panels can a checkout's own view checker accept?

Usage (from any directory):
    uv run --project <checkout> python <this file> <checkout> <label>

`panels.json` holds 10 panels, one per need found in the user's own prompts (see README.md).
Each runs through the checkout's own `check_view`. Whether a tool or flow exists is a separate
question (some panels read a tool north would make first), so every tool and flow a panel names
is taken to exist, and a data source to change nothing. The results hold counts and the first
words of each problem only.
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

CHECKOUT = Path(sys.argv[1]).resolve()
LABEL = sys.argv[2]
sys.path.insert(0, str(CHECKOUT))

from views import check_view  # noqa: E402

HERE = Path(__file__).parent


def _exists(name: str, args: dict | None = None) -> None:
    del name, args


def main() -> None:
    panels = json.loads((HERE / "panels.json").read_text(encoding="utf-8"))
    # main's checker has no check for a tool an action calls; give it every check it takes.
    takes = inspect.signature(check_view).parameters
    checks = {name: _exists for name in ("tool_problem", "flow_problem", "action_tool_problem") if name in takes}
    rows = []
    for panel in panels:
        problems = check_view(panel["view"], **checks)
        rows.append(
            {
                "panel": panel["view"]["id"],
                "need": panel["need"],
                "accepted": not problems,
                "problems": len(problems),
                "first_problem": problems[0][:140] if problems else "",
            }
        )
    out = {"checkout": LABEL, "accepted": sum(row["accepted"] for row in rows), "of": len(rows), "panels": rows}
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / f"verify_{LABEL}.json").write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"accepted": out["accepted"], "of": out["of"]}))
    for row in rows:
        print(f"{'yes' if row['accepted'] else 'no ':3} {row['panel']:18} {row['first_problem']}")


main()
