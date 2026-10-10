"""Local receiver + durable claims. Never sends to a real external service."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ARMS = ("current", "atomic_pause", "atomic_idempotent", "atomic_receipt")
FAULTS = ("none", "before_claim", "after_claim", "after_effect", "lost_ack", "known_no_effect", "after_success")


def sql(path: Path, command: str, args: tuple = ()) -> list:
    with sqlite3.connect(path, timeout=3) as db:
        return db.execute(command, args).fetchall()


def initialize(root: Path) -> None:
    sql(root / "receiver.db", "CREATE TABLE IF NOT EXISTS effects (id INTEGER PRIMARY KEY, op TEXT, payload TEXT)")
    sql(root / "intent.db", "CREATE TABLE IF NOT EXISTS operations (op TEXT PRIMARY KEY, payload TEXT, state TEXT)")


def claim(path: Path, op: str, payload: str, arm: str) -> str:
    """Commit before dispatch; one owner. A pending operation has no lease expiry."""
    with sqlite3.connect(path, timeout=3) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT payload, state FROM operations WHERE op=?", (op,)).fetchone()
        if row is not None and row[0] != payload:
            return "payload_mismatch"
        if row is not None:
            if row[1] == "succeeded":
                return "succeeded"
            if row[1] != "known_no_effect":
                # Pause blocks both retry and concurrent callers. The two other
                # arms permit replay/check ONLY because their receiver supports it.
                return "unknown" if arm == "atomic_pause" else "reconcile"
            db.execute("UPDATE operations SET state='pending' WHERE op=?", (op,))
            return "dispatch"
        db.execute("INSERT INTO operations VALUES (?, ?, 'pending')", (op, payload))
        return "dispatch"


def receive(root: Path, op: str, payload: str, *, dedup: bool) -> None:
    with sqlite3.connect(root / "receiver.db", timeout=3) as db:
        db.execute("BEGIN IMMEDIATE")
        if dedup:
            existing = db.execute("SELECT payload FROM effects WHERE op=?", (op,)).fetchone()
            if existing:
                if existing[0] != payload:
                    raise ValueError("receiver payload mismatch")
                return
        db.execute("INSERT INTO effects(op, payload) VALUES (?, ?)", (op, payload))


def receipt(root: Path, op: str, payload: str) -> bool:
    # Fixture capability: complete, strongly consistent lookup by operation ID.
    # This is NOT a claim about arbitrary websites or eventually consistent APIs.
    return bool(sql(root / "receiver.db", "SELECT 1 FROM effects WHERE op=? AND payload=?", (op, payload)))


def attempt(root: Path, arm: str, fault: str, op: str = "operation-1", payload: str = "account=A;amount=10") -> str:
    if fault == "before_claim":
        os._exit(33)
    state = claim(root / "intent.db", op, payload, arm)
    if state in {"succeeded", "unknown", "payload_mismatch"}:
        return state
    if fault == "after_claim":
        os._exit(33)
    if state == "reconcile" and arm == "atomic_receipt":
        if not receipt(root, op, payload):
            # Absence is not permission to resend: an older sender may still be
            # executing. No fencing/termination proof was supplied by this fixture.
            return "unknown"
    elif fault == "known_no_effect":
        sql(root / "intent.db", "UPDATE operations SET state='known_no_effect' WHERE op=?", (op,))
        return "known_no_effect"
    else:
        receive(root, op, payload, dedup=arm == "atomic_idempotent")
    if fault == "after_effect":
        os._exit(33)
    if fault == "lost_ack":
        return "unknown"
    sql(root / "intent.db", "UPDATE operations SET state='succeeded' WHERE op=?", (op,))
    if fault == "after_success":
        os._exit(33)
    return "succeeded"


async def current_attempt(root: Path, fault: str) -> None:
    from approval.approvals import Approvals, Request
    from approval.effects import EffectLog
    from approval.policy import Action, ActionKind, ApprovalPolicy
    from config.approval_mode import ApprovalMode
    from tools.base import Tool
    from tools.models import ToolInput, ToolOutput

    class Submit(Tool):
        name = "fixture_submit"
        description = "Local receiver only"
        is_mutating = True

        async def describe(self, input: ToolInput) -> Request:
            return Request(
                action=Action(
                    agent=self.name,
                    kind=ActionKind.BROWSER,
                    summary="fixture",
                    args="operation-1",
                    reaches_third_party=True,
                ),
                title="Fixture",
                message="Fixture",
            )

        async def run(self, input: ToolInput) -> ToolOutput:
            if fault == "known_no_effect":
                return ToolOutput(success=False, error="proven failure before dispatch")
            receive(root, "operation-1", "account=A;amount=10", dedup=False)
            if fault == "after_effect":
                os._exit(33)
            if fault == "lost_ack":
                return ToolOutput(success=False, error="response lost, outcome unknown")
            return ToolOutput(success=True)

    if fault in {"before_claim", "after_claim"}:
        os._exit(33)
    tool = Submit()
    tool.approvals = Approvals(
        ApprovalPolicy(mode_provider=lambda: ApprovalMode.YOLO), None, effects=EffectLog(root / "completed.db")
    )
    await tool.execute(ToolInput(params={"task_id": "fixture-task"}))
    if fault == "after_success":
        os._exit(33)


def subprocess_faults(root: Path) -> list:
    rows = []
    for arm in ARMS:
        for fault in FAULTS:
            directory = root / f"{arm}-{fault}"
            directory.mkdir()
            initialize(directory)
            argv = [sys.executable, "-m", "experiments.lean_trials.recovery_validation", "--child", str(directory), arm]
            initial = subprocess.run([*argv, fault], capture_output=True, text=True, timeout=15)
            assert initial.returncode == (
                33 if fault in {"before_claim", "after_claim", "after_effect", "after_success"} else 0
            ), initial.stderr
            resumed = subprocess.run([*argv, "none"], capture_output=True, text=True, timeout=15)
            assert resumed.returncode == 0, resumed.stderr
            count = sql(directory / "receiver.db", "SELECT count(*) FROM effects")[0][0]
            rows.append(
                {
                    "arm": arm,
                    "fault": fault,
                    "effects": count,
                    "recovery": resumed.stdout.strip() if arm != "current" else "current Tool.execute",
                }
            )
    return rows


async def concurrent_current(root: Path) -> int:
    """Force both workers past the real approval check before either reports success."""
    from approval.approvals import Approvals, Request
    from approval.effects import EffectLog
    from approval.policy import Action, ActionKind, ApprovalPolicy
    from config.approval_mode import ApprovalMode
    from tools.base import Tool
    from tools.models import ToolInput, ToolOutput

    entered, release = 0, asyncio.Event()

    class Submit(Tool):
        name = "fixture_concurrent_submit"
        description = "local fixture"
        is_mutating = True

        async def describe(self, input: ToolInput) -> Request:
            return Request(
                action=Action(
                    agent=self.name,
                    kind=ActionKind.BROWSER,
                    summary="fixture",
                    args="same-operation",
                    reaches_third_party=True,
                ),
                title="Fixture",
                message="Fixture",
            )

        async def run(self, input: ToolInput) -> ToolOutput:
            nonlocal entered
            entered += 1
            if entered == 2:
                release.set()
            await asyncio.wait_for(release.wait(), timeout=5)
            receive(root, "same-operation", "A", dedup=False)
            return ToolOutput(success=True)

    tools = [Submit(), Submit()]
    for tool in tools:
        tool.approvals = Approvals(
            ApprovalPolicy(mode_provider=lambda: ApprovalMode.YOLO), None, effects=EffectLog(root / "completed.db")
        )
    await asyncio.gather(*(tool.execute(ToolInput(params={"task_id": "same-task"})) for tool in tools))
    return sql(root / "receiver.db", "SELECT count(*) FROM effects")[0][0]


def concurrency(root: Path, repeats: int = 20) -> list:
    rows = []
    for arm in ARMS:
        for repeat in range(repeats):
            directory = root / f"race-{arm}-{repeat}"
            directory.mkdir()
            initialize(directory)
            if arm == "current":
                count = asyncio.run(concurrent_current(directory))
            else:
                barrier = threading.Barrier(2)

                def synchronized(selected_arm=arm, fixture=directory, start=barrier):
                    start.wait(timeout=5)
                    return attempt(fixture, selected_arm, "none", "same-operation", "A")

                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [executor.submit(synchronized) for _ in range(2)]
                    for future in futures:
                        future.result()
                count = sql(directory / "receiver.db", "SELECT count(*) FROM effects")[0][0]
            rows.append({"arm": arm, "repeat": repeat, "effects": count})
    return rows


def integrity(root: Path) -> list:
    """SQL failures and logical identity changes, independent of process cuts."""
    rows = []
    for arm in ARMS[1:]:
        for case in (
            "ledger_unavailable",
            "changed_payload",
            "separate_identical_operations",
            "old_sender_still_running",
            "response_record_failure",
        ):
            directory = root / f"integrity-{arm}-{case}"
            directory.mkdir()
            initialize(directory)
            rejected = False
            if case == "ledger_unavailable":
                # An actual SQLite immutable read-only connection rejects claim.
                original = sqlite3.connect

                def read_only(path, original_connect=original, **kwargs):
                    return original_connect(f"file:{path}?mode=ro&immutable=1", uri=True, **kwargs)

                try:
                    with patch.object(sqlite3, "connect", read_only):
                        attempt(directory, arm, "none")
                except sqlite3.OperationalError:
                    rejected = True
            elif case == "changed_payload":
                attempt(directory, arm, "none")
                rejected = attempt(directory, arm, "none", payload="account=B;amount=100") == "payload_mismatch"
            elif case == "separate_identical_operations":
                attempt(directory, arm, "none", op="intentionally-new-1")
                attempt(directory, arm, "none", op="intentionally-new-2")
            elif case == "old_sender_still_running":
                assert claim(directory / "intent.db", "operation-1", "account=A;amount=10", arm) == "dispatch"
                # New worker arrives before old worker commits its external effect.
                recovery = attempt(directory, arm, "none")
                receive(directory, "operation-1", "account=A;amount=10", dedup=arm == "atomic_idempotent")
                rejected = recovery == "unknown"
            else:
                original_sql = sql

                def failed_record(path, statement, args=(), original_execute=original_sql):
                    if statement.startswith("UPDATE operations SET state='succeeded'"):
                        raise sqlite3.OperationalError("injected completion-record failure")
                    return original_execute(path, statement, args)

                try:
                    with patch.dict(attempt.__globals__, {"sql": failed_record}):
                        attempt(directory, arm, "none")
                except sqlite3.OperationalError:
                    rejected = True
                attempt(directory, arm, "none")
            effects = sql(directory / "receiver.db", "SELECT count(*) FROM effects")[0][0]
            rows.append({"arm": arm, "case": case, "effects": effects, "paused_or_rejected": rejected})
    return rows


def run(root: Path) -> dict:
    faults = subprocess_faults(root)
    races = concurrency(root)
    checks = integrity(root)
    return {
        "kind": "real Tool.execute baseline; experimental atomic ledger; synthetic receiver only",
        "fault_rows": faults,
        "concurrency_rows": races,
        "integrity_rows": checks,
        "summary": {
            arm: {
                "duplicate_faults": sum(r["effects"] > 1 for r in faults if r["arm"] == arm),
                "unfinished_faults": sum(r["effects"] == 0 for r in faults if r["arm"] == arm),
                "duplicate_races": sum(r["effects"] > 1 for r in races if r["arm"] == arm),
            }
            for arm in ARMS
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", nargs=3)
    parser.add_argument("--output", type=Path, default=HERE / "recovery_validation_results.json")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="north-recovery-validation-") as temporary:
        root = Path(temporary).resolve()
        os.environ["NORTH_HOME"] = str(root / "north")
        with patch.object(Path, "home", return_value=root / "synthetic-home"):
            if args.child:
                directory, arm, fault = args.child
                if arm == "current":
                    asyncio.run(current_attempt(Path(directory), fault))
                else:
                    print(attempt(Path(directory), arm, fault))
                return
            results = run(root)
    results["source_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    args.output.write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results["summary"], indent=2))


if __name__ == "__main__":
    main()
