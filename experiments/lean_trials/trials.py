"""Small counterexamples and paired simulations, not a production implementation.

Run: .venv/bin/python -m experiments.lean_trials.trials
Fixtures and databases live in TemporaryDirectory, never NORTH's real home.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
import json
import os
import random
import shlex
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
from collections import Counter
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


def db_execute(path: Path, statement: str, args: tuple = ()) -> list:
    with sqlite3.connect(path) as db:
        return db.execute(statement, args).fetchall()


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def permissions() -> dict:
    from approval.policy import Action, ActionKind, ApprovalPolicy
    from config.approval_mode import ApprovalMode

    # A run's mode is a prompt preference, not proof of account/resource authority.
    # Test the existing policy with global vs per-run mode injection, then a
    # deliberately minimal experimental grant guard. No model decider involved.
    cases = [
        ("research read", "research", "read", "public", 0, False, True),
        ("research must ask before submit", "research", "submit", "account-A", 0, False, False),
        ("approved autonomous submit", "auto", "submit", "account-A", 0, False, True),
        ("other account", "auto", "submit", "account-B", 0, False, False),
        ("ungranted purchase", "auto", "buy", "account-A", 20, False, False),
        ("approved bounded purchase", "auto", "buy", "account-A", 10, False, True),
        ("child cannot inherit wider spending", "child", "buy", "account-A", 10, False, False),
        ("revoked submit", "auto", "submit", "account-A", 0, True, False),
        ("child inherits limited scope", "child", "submit", "account-B", 0, False, False),
        ("child authorized submit", "child", "submit", "account-A", 0, False, True),
        ("ask-mode isolated edit", "coding", "edit", "copy", 0, False, True),
        ("ask-mode landing", "coding", "land", "repo", 0, False, False),
    ]
    modes = {
        "research": ApprovalMode.ASK,
        "coding": ApprovalMode.ASK,
        "auto": ApprovalMode.YOLO,
        "child": ApprovalMode.YOLO,
    }
    rows = []
    for name, run, operation, resource, amount, revoked, expected in cases:
        action = Action(
            agent="fixture",
            kind=ActionKind.FILE_EDIT if operation == "edit" else ActionKind.OTHER,
            summary=name,
            operation=operation,
            read_only=operation == "read",
            isolated=operation == "edit",
            spends_money=bool(amount),
            reaches_third_party=operation == "submit",
        )
        for arm in ("global_yolo", "global_ask", "per_run_mode", "per_run_and_grant"):
            mode = (
                ApprovalMode.YOLO if arm == "global_yolo" else ApprovalMode.ASK if arm == "global_ask" else modes[run]
            )
            allowed = (await ApprovalPolicy(mode_provider=lambda m=mode: m).rule(action)).allowed
            if arm == "per_run_and_grant" and operation in {"submit", "buy", "land"}:
                matches_grant = operation == "submit" or (operation == "buy" and run == "auto" and amount <= 10)
                allowed = allowed and matches_grant and resource == "account-A" and not revoked
            rows.append({"case": name, "arm": arm, "allowed": allowed, "expected": expected})
    return {
        "kind": "real ApprovalPolicy + experimental guards; owner choices are fixtures",
        "issues": [74, 39, 40],
        "rows": rows,
        "summary": {
            arm: {
                "unauthorized": sum(r["allowed"] and not r["expected"] for r in rows if r["arm"] == arm),
                "extra_prompt_or_block_cases": sum(not r["allowed"] and r["expected"] for r in rows if r["arm"] == arm),
            }
            for arm in sorted({r["arm"] for r in rows})
        },
    }


async def effect_child(root: Path, arm: str, fault: str) -> None:
    """Kill a real child at a boundary; committed receiver state survives it."""
    from approval.approvals import Approvals, Request
    from approval.effects import EffectLog
    from approval.policy import Action, ActionKind, ApprovalPolicy
    from config.approval_mode import ApprovalMode
    from tools.base import Tool
    from tools.models import ToolInput, ToolOutput

    receiver = root / "receiver.db"
    intent = root / "intent.db"
    db_execute(receiver, "CREATE TABLE IF NOT EXISTS submissions (id INTEGER PRIMARY KEY, key TEXT)")
    db_execute(intent, "CREATE TABLE IF NOT EXISTS intents (key TEXT PRIMARY KEY, state TEXT)")
    key = "logical-operation-1"
    state = db_execute(intent, "SELECT state FROM intents WHERE key=?", (key,))
    if arm == "intent_pause" and state:
        return  # Uncertain outcome: never blindly replay; needs reconciliation.
    if fault == "before_intent":
        os._exit(33)
    if arm != "current":
        db_execute(intent, "INSERT OR IGNORE INTO intents VALUES (?, 'pending')", (key,))
    if fault == "after_intent":
        os._exit(33)

    class Submit(Tool):
        name = "fixture_submit"
        description = "Local SQLite receiver only."
        is_mutating = True

        async def describe(self, input: ToolInput) -> Request:
            return Request(
                action=Action(
                    agent=self.name,
                    kind=ActionKind.BROWSER,
                    summary="submit fixture",
                    args=key,
                    reaches_third_party=True,
                ),
                title="Fixture",
                message="Fixture",
            )

        async def run(self, input: ToolInput) -> ToolOutput:
            with sqlite3.connect(receiver) as conn:
                if arm == "receiver_idempotent":
                    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS receiver_key ON submissions(key)")
                    conn.execute("INSERT OR IGNORE INTO submissions(key) VALUES (?)", (key,))
                else:
                    conn.execute("INSERT INTO submissions(key) VALUES (?)", (key,))
            if fault == "after_effect":
                os._exit(33)
            return ToolOutput(success=True)

    tool = Submit()
    tool.approvals = Approvals(
        ApprovalPolicy(mode_provider=lambda: ApprovalMode.YOLO), None, effects=EffectLog(root / "effects.db")
    )
    await tool.execute(ToolInput(params={"task_id": "fixture-task"}))
    if fault == "after_log":
        os._exit(33)
    if arm != "current":
        db_execute(intent, "UPDATE intents SET state='done' WHERE key=?", (key,))


def crashes(root: Path) -> dict:
    rows = []
    for arm in ("current", "intent_pause", "receiver_idempotent"):
        for fault in ("none", "before_intent", "after_intent", "after_effect", "after_log"):
            fixture = root / f"{arm}-{fault}"
            fixture.mkdir()
            argv = [sys.executable, "-m", "experiments.lean_trials.trials", "--child", str(fixture), arm]
            first = subprocess.run([*argv, fault], capture_output=True, text=True, timeout=20)
            assert first.returncode == (0 if fault == "none" else 33), first.stderr
            resumed = subprocess.run([*argv, "none"], capture_output=True, text=True, timeout=20)
            assert resumed.returncode == 0, resumed.stderr
            count = db_execute(fixture / "receiver.db", "SELECT count(*) FROM submissions")[0][0]
            rows.append(
                {"arm": arm, "fault": fault, "receiver_effects": count, "duplicate": count > 1, "missing": count == 0}
            )
    return {
        "kind": "real Tool.execute and EffectLog, real process deaths, synthetic receiver",
        "issues": [45, 36, 49, 70],
        "rows": rows,
        "summary": {
            arm: {
                "duplicate_cases": sum(r["duplicate"] for r in rows if r["arm"] == arm),
                "unfinished_cases": sum(r["missing"] for r in rows if r["arm"] == arm),
            }
            for arm in sorted({r["arm"] for r in rows})
        },
    }


def checklist(root: Path) -> dict:
    from orchestrator.plan_store import PlanStore

    # This does NOT test or claim a bypass of today's coding landing gate.
    # It asks whether TODOs alone can represent a new multi-round requirement.
    rows = []
    cases = [
        "valid",
        "restart",
        "edit_after_review",
        "tests_failed",
        "review_missing",
        "model_ticks_done",
        "old_attempt",
        "required_round_missing",
    ]
    for arm in ("current_todo", "persisted_ticks", "persisted_evidence"):
        for case in cases:
            path = root / f"checklist-{arm}-{case}.db"
            db_execute(path, "CREATE TABLE evidence (kind TEXT, hash TEXT, attempt INTEGER, round INTEGER, ok INTEGER)")
            expected = case in {"valid", "restart"}
            plan = PlanStore()
            plan.set_plan("t", [{"content": "test and review twice", "status": "done"}])
            old_hash = digest("code-v1")
            current_hash = digest("code-v2") if case == "edit_after_review" else old_hash
            attempt = 1
            evidence_attempt = 0 if case == "old_attempt" else attempt
            if case != "model_ticks_done":
                db_execute(
                    path,
                    "INSERT INTO evidence VALUES ('test', ?, ?, 0, ?)",
                    (old_hash, evidence_attempt, case != "tests_failed"),
                )
                if case != "review_missing":
                    for review_round in range(1, 2 if case == "required_round_missing" else 3):
                        db_execute(
                            path,
                            "INSERT INTO evidence VALUES ('review', ?, ?, ?, 1)",
                            (old_hash, evidence_attempt, review_round),
                        )
            if arm == "current_todo":
                if case == "restart":
                    plan = PlanStore()
                ready = plan.progress("t") == (1, 1)
            elif arm == "persisted_ticks":
                db_execute(path, "CREATE TABLE ticks (done INTEGER)")
                db_execute(path, "INSERT INTO ticks VALUES (1)")
                ready = db_execute(path, "SELECT done FROM ticks")[0][0] == 1
            else:
                valid = db_execute(
                    path,
                    "SELECT kind, round FROM evidence WHERE hash=? AND attempt=? AND ok=1",
                    (current_hash, attempt),
                )
                ready = {("test", 0), ("review", 1), ("review", 2)} <= set(valid)
            rows.append({"arm": arm, "case": case, "ready": ready, "expected": expected})
    return {
        "kind": "real PlanStore vs experimental SQLite requirement guards, not current landing path",
        "issues": [63, 45, 47],
        "rows": rows,
        "summary": {
            arm: {
                "false_ready": sum(r["ready"] and not r["expected"] for r in rows if r["arm"] == arm),
                "false_blocked": sum(not r["ready"] and r["expected"] for r in rows if r["arm"] == arm),
            }
            for arm in sorted({r["arm"] for r in rows})
        },
    }


class Receiver(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"fixture-ok")

    def log_message(self, *args) -> None:
        pass


def sandbox(root: Path) -> dict:
    from coding_agents.confinement import codex_filesystem
    from coding_agents.models import Mode
    from tools.specialized import _seatbelt

    home = root / "synthetic-home"
    workspace = home / "project"
    workspace.mkdir(parents=True)
    for name in (".config", ".cache", ".local/bin"):
        (home / name).mkdir(parents=True, exist_ok=True)
    token = "synthetic-" + digest(str(root))[:20]
    for name in (".config/secret", "new-credential", ".local/bin/tool"):
        (home / name).write_text(token)
    rules = codex_filesystem(str(workspace), Mode.EDIT, (), home=home)
    late = home / "created-after-start"
    late.write_text(token)
    covered = any(
        access == "deny" and (late == Path(path) or Path(path) in late.parents) for path, access in rules.items()
    )
    codex = {
        "kind": "actual generated rule-map inspection only; NOT a live Codex enforcement test",
        "late_file_has_explicit_deny": covered,
        "existing_file_denied": rules.get(str(home / "new-credential")) == "deny",
    }
    if not _seatbelt.available():
        return {"kind": "kernel probes", "status": "unavailable", "codex_rules": codex, "issues": [37, 64, 65]}
    servers = [ThreadingHTTPServer(("127.0.0.1", 0), Receiver) for _ in range(2)]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    allowed_port, denied_port = (server.server_port for server in servers)

    def http_command(port: int) -> str:
        script = (
            "import urllib.request; "
            "opener=urllib.request.build_opener(urllib.request.ProxyHandler({})); "
            f"print(opener.open('http://127.0.0.1:{port}', timeout=2).read().decode(), end='')"
        )
        return shlex.join([sys.executable, "-c", script])

    rows = []
    try:
        with patch.object(Path, "home", return_value=home):
            current = _seatbelt.profile(str(workspace), writable=True, proxy_port=allowed_port)
        narrow = current.replace('(remote ip "localhost:*")', f'(remote ip "localhost:{allowed_port}")')
        closed_home = narrow + f"\n(deny file-read-data (subpath {_seatbelt._quote(home)}))"
        closed_home += (
            "\n(allow file-read-data "
            + " ".join(f"(subpath {_seatbelt._quote(p)})" for p in (workspace, home / ".cache", home / ".local/bin"))
            + ")"
        )
        cases = [
            ("workspace_write", f"printf ok > {shlex.quote(str(workspace / 'output'))}", True, None),
            ("cache_write", f"printf ok > {shlex.quote(str(home / '.cache/output'))}", True, None),
            ("toolchain_read", f"/bin/cat {shlex.quote(str(home / '.local/bin/tool'))}", True, token),
            ("known_secret", f"/bin/cat {shlex.quote(str(home / '.config/secret'))}", False, token),
            ("late_unknown_secret", f"/bin/cat {shlex.quote(str(late))}", False, token),
            (
                "allowed_local_server",
                http_command(allowed_port),
                True,
                "fixture-ok",
            ),
            (
                "unapproved_local_server",
                http_command(denied_port),
                False,
                "fixture-ok",
            ),
        ]
        for name, command, expected, output in cases:
            control = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True, timeout=5)
            assert control.returncode == 0 and (output is None or control.stdout == output), control.stderr
            for arm, profile in (
                ("current", current),
                ("narrow_ports", narrow),
                ("narrow_ports_and_home", closed_home),
            ):
                completed = subprocess.run(
                    ["/usr/bin/sandbox-exec", "-p", profile, "/bin/sh", "-c", command],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if "sandbox_apply:" in completed.stderr:
                    return {
                        "kind": "kernel probes",
                        "status": "environment blocked",
                        "reason": completed.stderr.strip(),
                        "codex_rules": codex,
                        "issues": [37, 64, 65],
                    }
                allowed = completed.returncode == 0 and (output is None or completed.stdout == output)
                if not allowed:
                    assert _seatbelt.denied(completed.stderr), (name, arm, completed.stderr)
                rows.append(
                    {
                        "case": name,
                        "arm": arm,
                        "allowed": allowed,
                        "expected": expected,
                        "exit_code": completed.returncode,
                    }
                )
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
    return {
        "kind": "real macOS kernel, synthetic home and local HTTP fixtures, no external hosts",
        "issues": [37, 64, 65],
        "status": "tested",
        "codex_rules": codex,
        "rows": rows,
        "summary": {
            arm: {
                "boundary_failures": sum(r["allowed"] and not r["expected"] for r in rows if r["arm"] == arm),
                "legitimate_breakages": sum(not r["allowed"] and r["expected"] for r in rows if r["arm"] == arm),
            }
            for arm in sorted({r["arm"] for r in rows})
        },
    }


def scheduling(seed_start: int = 0, count: int = 200) -> dict:
    """Not North's scheduler. Same synthetic jobs for each policy, no APIs."""
    rows = []
    for seed in range(seed_start, seed_start + count):
        rng = random.Random(seed)
        # Include quiet/busy and single/multiple-provider cases, vary work cost.
        providers = rng.choice([1, 2])
        quota = rng.randint(3, 8)
        bg_rate = rng.choice([0.04, 0.12, 0.3, 0.6])
        jobs = []
        for tick in range(120):
            for foreground, rate in ((False, bg_rate), (True, rng.choice([0.04, 0.08, 0.15]))):
                if rng.random() < rate:
                    jobs.append(
                        {
                            "id": len(jobs),
                            "arrival": tick,
                            "fg": foreground,
                            "provider": rng.randrange(providers),
                            "duration": rng.randint(1, 4),
                        }
                    )
        if not any(j["fg"] for j in jobs):
            continue
        for arm in ("fifo", "foreground_priority", "priority_with_reserve"):
            pending, running, finished = [], [], []
            available = [quota] * providers
            for tick in range(2000):
                if tick % 10 == 0:
                    available = [quota] * providers
                finished.extend((j, tick - j["arrival"]) for j, end in running if end == tick)
                running = [(j, end) for j, end in running if end != tick]
                pending.extend(j for j in jobs if j["arrival"] == tick)
                occupied = Counter(j["provider"] for j, _ in running)
                eligible = [j for j in pending if available[j["provider"]] > 0 and occupied[j["provider"]] < 2]
                if arm != "fifo":
                    eligible.sort(key=lambda j: (not j["fg"], j["arrival"], j["id"]))
                for job in eligible:
                    provider = job["provider"]
                    if occupied[provider] >= 2 or available[provider] == 0:
                        continue
                    # Keep one provider admission for interactive work until late
                    # in the quota window; unused reserve then returns to batch.
                    if arm == "priority_with_reserve" and not job["fg"] and available[provider] == 1 and tick % 10 < 8:
                        continue
                    available[provider] -= 1
                    occupied[provider] += 1
                    pending.remove(job)
                    running.append((job, tick + job["duration"]))
                if tick >= 120 and not pending and not running:
                    break
            assert len(finished) == len(jobs)
            fg = sorted(latency for j, latency in finished if j["fg"])
            bg = sorted(latency for j, latency in finished if not j["fg"])
            rows.append(
                {
                    "seed": seed,
                    "arm": arm,
                    "providers": providers,
                    "quota_per_10_ticks": quota,
                    "bg_arrival_rate": bg_rate,
                    "fg_mean": statistics.mean(fg),
                    "fg_p95": fg[int((len(fg) - 1) * 0.95)],
                    "bg_mean": statistics.mean(bg) if bg else 0,
                    "drain_ticks": tick,
                }
            )
    pairs = []
    for lhs, rhs in (("fifo", "foreground_priority"), ("foreground_priority", "priority_with_reserve")):
        left = {r["seed"]: r for r in rows if r["arm"] == lhs}
        right = {r["seed"]: r for r in rows if r["arm"] == rhs}
        comparison = {"baseline": lhs, "candidate": rhs, "scenarios": len(left)}
        for metric in ("fg_mean", "fg_p95", "bg_mean", "drain_ticks"):
            comparison[metric] = {
                "wins": sum(right[s][metric] < left[s][metric] for s in left),
                "ties": sum(right[s][metric] == left[s][metric] for s in left),
                "losses": sum(right[s][metric] > left[s][metric] for s in left),
                "median_delta": statistics.median(right[s][metric] - left[s][metric] for s in left),
            }
        pairs.append(comparison)
    return {
        "kind": "seeded synthetic quota/queue workloads; not measured North performance",
        "seed_start": seed_start,
        "scenario_count": count,
        "issues": [8, 67],
        "rows": rows,
        "paired_comparisons": pairs,
    }


def delivery(root: Path) -> dict:
    # A local durable outbox cannot prevent a duplicate after the remote ack is
    # lost. Receiver cooperation is an explicit assumption in the third arm.
    rows = []
    for arm in ("direct", "outbox", "outbox_receiver_dedup"):
        for failure in ("none", "offline", "lost_ack", "restart_before_send"):
            path = root / f"delivery-{arm}-{failure}.db"
            db_execute(path, "CREATE TABLE receiver (id TEXT)")
            db_execute(path, "CREATE TABLE outbox (id TEXT PRIMARY KEY, delivered INTEGER)")
            if arm != "direct":
                db_execute(path, "INSERT INTO outbox VALUES ('event-1', 0)")
            if failure not in {"offline", "restart_before_send"}:
                db_execute(path, "INSERT INTO receiver VALUES ('event-1')")
                if arm != "direct" and failure != "lost_ack":
                    db_execute(path, "UPDATE outbox SET delivered=1")
            # Only committed pending events are retried after reconnect/restart.
            if arm != "direct" and db_execute(path, "SELECT id FROM outbox WHERE delivered=0"):
                if arm == "outbox_receiver_dedup":
                    db_execute(path, "CREATE UNIQUE INDEX dedup ON receiver(id)")
                    db_execute(path, "INSERT OR IGNORE INTO receiver VALUES ('event-1')")
                else:
                    db_execute(path, "INSERT INTO receiver VALUES ('event-1')")
                db_execute(path, "UPDATE outbox SET delivered=1")
            count = db_execute(path, "SELECT count(*) FROM receiver")[0][0]
            rows.append({"arm": arm, "failure": failure, "delivered": count})
    return {
        "kind": "experimental SQLite outbox + receiver, not current channel adapters",
        "issues": [49, 26, 70],
        "rows": rows,
        "summary": {
            arm: {
                "lost_cases": sum(r["delivered"] == 0 for r in rows if r["arm"] == arm),
                "duplicate_cases": sum(r["delivered"] > 1 for r in rows if r["arm"] == arm),
            }
            for arm in sorted({r["arm"] for r in rows})
        },
    }


def capability_evidence(root: Path) -> dict:
    from flows.models import Flow, FlowStep, flow_fingerprint
    from skills.models import Skill, SkillExecution

    bundle = root / "skill-bundle"
    bundle.mkdir()
    helper = bundle / "helper.json"
    notes = bundle / "notes.txt"

    def reset_bundle() -> None:
        helper.write_text('{"action": "sum"}')
        notes.write_text("Maintainer-only notes, ignored during execution.")

    reset_bundle()
    original_skill = Skill(
        "fixture",
        "Use to test",
        "Read helper.json to decide the action. Ignore notes.txt (maintainer-only notes).",
        bundle,
        execution=SkillExecution(agent="engineering", tools=("read_file",)),
    )
    original_flow = Flow("fixture-flow", "fixture", (FlowStep("one", "fixture"),), root)
    cases = [
        ("unchanged", original_flow, original_skill, False),
        ("skill_body", original_flow, replace(original_skill, body="different work"), True),
        (
            "skill_tool",
            original_flow,
            replace(original_skill, execution=SkillExecution(agent="engineering", tools=("bash",))),
            True,
        ),
        (
            "skill_approval",
            original_flow,
            replace(original_skill, execution=replace(original_skill.execution, approval="always")),
            True,
        ),
        ("skill_retired", original_flow, replace(original_skill, status="retired"), True),
        (
            "flow_instruction",
            replace(original_flow, steps=(FlowStep("one", "fixture", instructions="new"),)),
            original_skill,
            True,
        ),
        (
            "flow_input",
            replace(original_flow, steps=(FlowStep("one", "fixture", inputs={"account": "B"}),)),
            original_skill,
            True,
        ),
        (
            "evidence_metadata",
            replace(original_flow, provenance=("a",), activation_fingerprint="evidence"),
            original_skill,
            False,
        ),
        ("referenced_bundle_changed", original_flow, original_skill, True),
        ("unused_notes_changed", original_flow, original_skill, False),
        ("version_label_only", original_flow, replace(original_skill, version="2.0.0"), False),
    ]
    rows = []
    for arm in ("name_version", "current_fingerprint", "fingerprint_all_bundled_files"):

        def identity(flow: Flow, skill: Skill, selected_arm: str = arm) -> str:
            if selected_arm == "name_version":
                return flow.name + skill.name + skill.version
            fingerprint = flow_fingerprint(flow, lambda _: skill)
            if selected_arm == "fingerprint_all_bundled_files":
                fingerprint += digest(
                    json.dumps(
                        {name: (skill.directory / name).read_text() for name in skill.bundled_file_names()},
                        sort_keys=True,
                    )
                )
            return fingerprint

        reset_bundle()
        before = identity(original_flow, original_skill)
        for name, flow, skill, expected in cases:
            reset_bundle()
            if name == "referenced_bundle_changed":
                helper.write_text('{"action": "delete"}')
            elif name == "unused_notes_changed":
                notes.write_text("Cosmetic change to ignored maintainer notes.")
            changed = identity(flow, skill) != before
            rows.append({"case": name, "arm": arm, "invalidated": changed, "expected": expected})
    return {
        "kind": "actual existing flow/skill fingerprints, controlled mutations; no new registry",
        "issues": [47],
        "rows": rows,
        "summary": {
            arm: {
                "stale_evidence_accepted": sum(not r["invalidated"] and r["expected"] for r in rows if r["arm"] == arm),
                "needless_invalidations": sum(r["invalidated"] and not r["expected"] for r in rows if r["arm"] == arm),
            }
            for arm in sorted({r["arm"] for r in rows})
        },
    }


def loopback() -> dict:
    # Candidate input validation only. Never opens a public socket or starts North.
    cases = [
        ("127.0.0.1", True),
        ("127.0.0.2", True),
        ("::1", True),
        ("localhost", True),
        ("0.0.0.0", False),
        ("::", False),
        ("192.168.1.3", False),
        ("localhost.evil", False),
    ]
    rows = []
    for arm in ("literal_ip_only", "literal_ip_or_localhost"):
        for host, expected in cases:
            try:
                accepted = ipaddress.ip_address(host).is_loopback
            except ValueError:
                accepted = arm == "literal_ip_or_localhost" and host == "localhost"
            rows.append({"host": host, "arm": arm, "accepted": accepted, "expected": expected})
    return {
        "kind": "input-validation prototype only, not server enforcement or DNS rebinding protection",
        "issues": [38],
        "rows": rows,
        "summary": {
            arm: {
                "unsafe_accepted": sum(r["accepted"] and not r["expected"] for r in rows if r["arm"] == arm),
                "legitimate_rejected": sum(not r["accepted"] and r["expected"] for r in rows if r["arm"] == arm),
            }
            for arm in sorted({r["arm"] for r in rows})
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", nargs=3, metavar=("ROOT", "ARM", "FAULT"))
    parser.add_argument("--output", type=Path, default=HERE / "results.json")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="north-lean-trials-") as temporary:
        root = Path(temporary).resolve()
        os.environ["NORTH_HOME"] = str(root / "isolated-north")
        # Importing settings must not load the developer's ~/.north/.env.
        with patch.object(Path, "home", return_value=root / "config-home"):
            if args.child:
                fixture, arm, fault = args.child
                asyncio.run(effect_child(Path(fixture), arm, fault))
                return
            result = {
                "schema": 1,
                "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                "experiment_sha256": digest(Path(__file__).read_text()),
                "python": sys.version.split()[0],
                "platform": sys.platform,
                "permissions": asyncio.run(permissions()),
                "crash_recovery": crashes(root),
                "checklist": checklist(root),
                "sandbox": sandbox(root),
                "scheduling": scheduling(),
                "scheduling_holdout": scheduling(seed_start=200),
                "delivery": delivery(root),
                "capability_evidence": capability_evidence(root),
                "loopback": loopback(),
            }
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        for name, data in result.items():
            if isinstance(data, dict):
                print(name, json.dumps(data.get("summary", data.get("paired_comparisons", data.get("status")))))


if __name__ == "__main__":
    main()
