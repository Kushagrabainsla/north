"""How many replayed items did not exist yet when their task ran, and does it change the #85 numbers?

A fact counts as existing from its observed_at (or, without one, its updated_at, which
can only be later than its creation, so this check may drop a fact that did exist).
A past-task note exists from its timestamp.

Run: .venv/bin/python -m experiments.memory_recall.leak_check
"""

from __future__ import annotations

import sqlite3

from experiments.memory_recall.common import DATA_DIR, SNAPSHOT_DIR, read_jsonl, write_result


def known_since() -> dict[str, str]:
    out: dict[str, str] = {}
    with sqlite3.connect(SNAPSHOT_DIR / "facts.db") as conn:
        for fid, observed, updated in conn.execute("SELECT id, observed_at, updated_at FROM context_facts"):
            out[f"fact:{fid}"] = (observed or updated or "")[:19]
    with sqlite3.connect(SNAPSHOT_DIR / "episodic.db") as conn:
        for eid, stamp in conn.execute("SELECT id, timestamp FROM episodes"):
            out[f"episode:{eid}"] = (stamp or "")[:19]
    return out


def existed(item_id: str, started_at: str, since: dict[str, str]) -> bool:
    if item_id.startswith("profile:"):
        return True
    stamp = since.get(item_id, "")
    return not stamp or stamp <= started_at[:19]


def main() -> None:
    since = known_since()
    cases = {c["run_id"]: c for c in read_jsonl(DATA_DIR / "cases.jsonl")}
    future = {"fact": 0, "episode": 0}
    loaded = {"fact": 0, "episode": 0}
    for case in cases.values():
        for item in case["facts"] + case["episodes"]:
            loaded[item["kind"]] += 1
            future[item["kind"]] += not existed(item["id"], case["started_at"], since)
    report = {"loaded": loaded, "did_not_exist_yet": future}

    labels = read_jsonl(DATA_DIR / "e85_labels.jsonl")
    share = {}
    for kind in ("fact", "episode", "profile"):
        n_all = u_all = n_old = u_old = 0
        for row in labels:
            start = cases[row["run_id"]]["started_at"]
            for item_id, used in row["reference"].items():
                if row["kinds"][item_id] != kind:
                    continue
                n_all += 1
                u_all += used
                if existed(item_id, start, since):
                    n_old += 1
                    u_old += used
        share[kind] = {
            "all_items": f"{u_all}/{n_all}",
            "existing_items_only": f"{u_old}/{n_old}",
            "used_share_existing_only": round(u_old / n_old, 3) if n_old else None,
        }
    report["e85_used_share"] = share
    write_result("leak_check", report)
    print(report)


if __name__ == "__main__":
    main()
