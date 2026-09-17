"""Offline cross-prompt benchmark for North's execution guardrails.

Run:
    .venv/bin/python -m experiments.system_quality.benchmark --details
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any

from agents.tool_output_reduction import salient_excerpt
from orchestrator.models import ExecutionMode, ExecutionPlan, IntentClassification
from orchestrator.router import _execution_profile
from orchestrator.verification import evidence_sufficiency_violations

_CASES_PATH = Path(__file__).with_name("cases.json")
_FILLER = "ordinary diagnostic context without a notable signal\n"


def load_cases(path: Path = _CASES_PATH) -> dict[str, list[dict[str, Any]]]:
    return json.loads(path.read_text(encoding="utf-8"))


def _metric(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    failures = [outcome["id"] for outcome in outcomes if not outcome["passed"]]
    passed = len(outcomes) - len(failures)
    return {
        "passed": passed,
        "total": len(outcomes),
        "accuracy": passed / len(outcomes) if outcomes else 1.0,
        "failures": failures,
    }


def evaluate_routing(cases: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    outcomes = []
    for case in cases:
        classification = IntentClassification(
            is_consequential=False,
            domain="engineering",
            reasoning="benchmark label",
            confidence=float(case["confidence"]),
        )
        plan = ExecutionPlan(
            task_id=case["id"],
            agents=["researcher"],
            parallel_groups=[["researcher"]],
            dependencies={},
            mode=ExecutionMode.SINGLE_AGENT,
            engineering_kind=case["engineering_kind"],
        )
        actual = _execution_profile(case["prompt"], classification, plan)
        outcomes.append(
            {
                "id": case["id"],
                "repository_type": case["repository_type"],
                "expected": case["expected_profile"],
                "actual": actual,
                "passed": actual == case["expected_profile"],
            }
        )
    return _metric(outcomes), outcomes


def evaluate_evidence(cases: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    outcomes = []
    for case in cases:
        violations = evidence_sufficiency_violations(
            case["prompt"],
            case["successful_tools"],
            case["evidence_counts"],
            repository_context=bool(case.get("repository_context", False)),
        )
        actual_pass = not violations
        outcomes.append(
            {
                "id": case["id"],
                "repository_type": case["repository_type"],
                "expected": bool(case["expected_pass"]),
                "actual": actual_pass,
                "violations": violations,
                "passed": actual_pass == bool(case["expected_pass"]),
            }
        )
    return _metric(outcomes), outcomes


def _sized_text(characters: int, position: float, signal: str) -> str:
    available = max(0, characters - len(signal) - 2)
    before_chars = int(available * position)
    after_chars = available - before_chars

    def fill(length: int) -> str:
        repeats = (length + len(_FILLER) - 1) // len(_FILLER)
        return (_FILLER * repeats)[:length]

    return f"{fill(before_chars)}\n{signal}\n{fill(after_chars)}"


def evaluate_reduction(cases: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    outcomes = []
    for case in cases:
        signal = str(case["signal"])
        limit = int(case["limit"])
        reduced = salient_excerpt(
            _sized_text(int(case["characters"]), float(case["position"]), signal),
            limit,
        )
        retained = signal in reduced
        outcomes.append(
            {
                "id": case["id"],
                "repository_type": case["repository_type"],
                "input_characters": int(case["characters"]),
                "output_characters": len(reduced),
                "limit": limit,
                "signal_retained": retained,
                "passed": retained and len(reduced) <= limit,
            }
        )
    return _metric(outcomes), outcomes


def run_benchmark(path: Path = _CASES_PATH) -> dict[str, Any]:
    cases = load_cases(path)
    routing, routing_outcomes = evaluate_routing(cases["routing"])
    evidence, evidence_outcomes = evaluate_evidence(cases["evidence"])
    reduction, reduction_outcomes = evaluate_reduction(cases["reduction"])
    metrics = {
        "routing_profile_accuracy": routing,
        "evidence_gate_accuracy": evidence,
        "tool_signal_retention": reduction,
    }
    total = sum(metric["total"] for metric in metrics.values())
    passed = sum(metric["passed"] for metric in metrics.values())
    repository_types = sorted(
        {
            case["repository_type"]
            for section in cases.values()
            for case in section
            if case["repository_type"] != "none"
        }
    )
    return {
        "recorded_at": date.today().isoformat(),
        "kind": "offline_production_primitives",
        "repository_types": repository_types,
        "metrics": metrics,
        "overall": {"passed": passed, "total": total, "accuracy": passed / total if total else 1.0},
        "details": {
            "routing": routing_outcomes,
            "evidence": evidence_outcomes,
            "reduction": reduction_outcomes,
        },
    }


def render_report(result: dict[str, Any], *, details: bool = False) -> str:
    lines = [
        "North system-quality benchmark",
        f"Repository types: {', '.join(result['repository_types'])}",
        "",
    ]
    for name, metric in result["metrics"].items():
        lines.append(f"{name:<28} {metric['passed']:>2}/{metric['total']:<2}  {metric['accuracy']:.1%}")
    overall = result["overall"]
    lines.extend(("", f"Overall: {overall['passed']}/{overall['total']} ({overall['accuracy']:.1%})"))
    if details:
        lines.append("")
        for section, outcomes in result["details"].items():
            for outcome in outcomes:
                mark = "PASS" if outcome["passed"] else "FAIL"
                lines.append(f"[{mark}] {section:<9} {outcome['id']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=_CASES_PATH)
    parser.add_argument("--details", action="store_true")
    parser.add_argument("--json", action="store_true", help="print the complete machine-readable result")
    args = parser.parse_args(argv)

    result = run_benchmark(args.cases)
    print(json.dumps(result, indent=2) if args.json else render_report(result, details=args.details))
    return 0 if result["overall"]["passed"] == result["overall"]["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
