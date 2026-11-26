"""Issue #39: how many active facts read like a permission, and are they confirmed? (counts only)

Reads ~/.north/facts.db read-only.

Run: .venv/bin/python -m experiments.approval_trust.permission_facts
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from pathlib import Path

DB = Path.home() / ".north" / "facts.db"
PERMISSION = re.compile(
    r"authori[sz]|approv|permission|permits?\b|allowed to|allows north|without asking|standing"
    r"|go ahead|ok to\b|okay to\b",
    re.IGNORECASE,
)


def main() -> None:
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT category, confirmed, source_path IS NOT NULL AND source_path != '', content "
            "FROM context_facts WHERE status = 'active'"
        ).fetchall()
    hits = [(cat, conf, from_file) for cat, conf, from_file, content in rows if PERMISSION.search(content)]
    print(f"active facts: {len(rows)}; read like a permission: {len(hits)}")
    print(f"  confirmed: {sum(1 for _, c, _ in hits if c)}, unconfirmed: {sum(1 for _, c, _ in hits if not c)}")
    print(f"  with a source file: {sum(1 for _, _, f in hits if f)}")
    print(f"  by category: {dict(Counter(cat for cat, _, _ in hits))}")


if __name__ == "__main__":
    main()
