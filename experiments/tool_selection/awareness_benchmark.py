"""Three-call A/B for how much catalog text a model needs to identify tools.

This complements ``retrieval_benchmark``: that benchmark measures the local
retriever, while this one measures model awareness without executing any tool.

Run:
    .venv/bin/python -m experiments.tool_selection.awareness_benchmark
"""

from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import dataclass
from typing import Any

from config.settings import settings
from experiments.tool_selection.retrieval_benchmark import (
    EvalCase,
    ToolDoc,
    discover_tool_docs,
    load_cases,
)
from inference.codex_auth import CodexCredentialProvider
from inference.models import CompletionRequest, PoolPriority
from inference.provider import Provider
from inference.providers.groq import GroqRouter
from inference.providers.openai_codex import OpenAICodexProvider
from utils.text import extract_json

DEFAULT_MODELS = {
    "groq": "openai/gpt-oss-120b",
    "openai_codex": "gpt-5.6-terra",
}


@dataclass(frozen=True)
class AwarenessResult:
    catalog: str
    tool_recall: float
    full_task_recall: float
    tokens_in: int
    tokens_out: int
    duration_seconds: float
    missed_cases: tuple[str, ...]


def _catalog_text(kind: str, docs: dict[str, ToolDoc]) -> str:
    if kind == "none":
        return "No catalog is provided. Infer likely North tool identifiers from the tasks."
    if kind == "names":
        return "Available tool names:\n" + ", ".join(sorted(docs))
    if kind == "descriptions":
        lines = [
            f"- {doc.name}: {doc.description.split('. ')[0].strip()}"
            for doc in sorted(docs.values(), key=lambda item: item.name)
        ]
        return "Available tools:\n" + "\n".join(lines)
    raise ValueError(f"Unknown catalog kind: {kind}")


def build_prompt(kind: str, docs: dict[str, ToolDoc], cases: list[EvalCase]) -> str:
    tasks = "\n".join(f"- {case.id}: {case.prompt}" for case in cases)
    return f"""You are measuring awareness of North's installed tools.

For each task below, identify the minimal set of tool identifiers needed. Do not
solve the tasks and do not explain. Return one JSON object mapping every task ID
to an array of tool-name strings. Use an empty array only when no tool is needed.

{_catalog_text(kind, docs)}

Tasks:
{tasks}
"""


def score_predictions(
    kind: str,
    predictions: dict[str, Any],
    cases: list[EvalCase],
    *,
    tokens_in: int = 0,
    tokens_out: int = 0,
    duration_seconds: float = 0,
) -> AwarenessResult:
    total_expected = 0
    total_recalled = 0
    fully_recalled = 0
    missed_cases: list[str] = []
    for case in cases:
        raw = predictions.get(case.id, [])
        predicted = {str(name) for name in raw} if isinstance(raw, list) else set()
        expected = set(case.expected)
        total_expected += len(expected)
        total_recalled += len(expected & predicted)
        complete = expected <= predicted
        fully_recalled += int(complete)
        if not complete:
            missing = ",".join(sorted(expected - predicted))
            missed_cases.append(f"{case.id}[{missing}]")
    return AwarenessResult(
        catalog=kind,
        tool_recall=total_recalled / total_expected,
        full_task_recall=fully_recalled / len(cases),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        duration_seconds=duration_seconds,
        missed_cases=tuple(missed_cases),
    )


def _build_provider(name: str) -> Provider:
    if name == "groq":
        if not settings.groq_api_key:
            raise RuntimeError("NORTH_GROQ_API_KEY is not configured in ~/.north/.env")
        return GroqRouter(settings.groq_api_key)
    if name == "openai_codex":
        credentials = CodexCredentialProvider()
        if not credentials.status().configured:
            raise RuntimeError("OpenAI Codex is not authenticated; run `north auth login openai-codex`")
        return OpenAICodexProvider(credentials)
    raise ValueError(f"Unknown provider: {name}")


async def run(provider_name: str, model: str, catalogs: tuple[str, ...]) -> list[AwarenessResult]:
    docs = discover_tool_docs()
    cases = load_cases()
    provider = _build_provider(provider_name)
    results: list[AwarenessResult] = []
    try:
        await provider.refresh()
        if model not in provider.get_models():
            raise RuntimeError(f"{provider_name} model is unavailable: {model}")
        for kind in catalogs:
            started = time.monotonic()
            response = await provider.complete(
                model,
                CompletionRequest(
                    prompt=build_prompt(kind, docs, cases),
                    priority=PoolPriority.LOW,
                    component="tool_awareness_eval",
                    max_tokens=4_000,
                    temperature=0,
                    json_mode=True,
                ),
            )
            parsed = extract_json(response.text)
            if not isinstance(parsed, dict):
                raise RuntimeError(f"{kind} arm did not return a JSON object")
            results.append(
                score_predictions(
                    kind,
                    parsed,
                    cases,
                    tokens_in=response.tokens_in,
                    tokens_out=response.tokens_out,
                    duration_seconds=time.monotonic() - started,
                )
            )
    finally:
        await provider.aclose()
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=tuple(DEFAULT_MODELS), default="groq")
    parser.add_argument("--model", help="Exact provider model ID; defaults by provider.")
    parser.add_argument(
        "--catalog",
        choices=("none", "names", "descriptions"),
        nargs="+",
        default=("none", "names", "descriptions"),
    )
    parser.add_argument("--details", action="store_true", help="List missed expected tools.")
    args = parser.parse_args()
    model = args.model or DEFAULT_MODELS[args.provider]
    print(f"Tool-awareness model: {args.provider}/{model}")
    print("catalog       recall  full-task  tokens in/out  seconds")
    results = asyncio.run(run(args.provider, model, tuple(args.catalog)))
    for result in results:
        print(
            f"{result.catalog:<12} {result.tool_recall:>6.1%}  "
            f"{result.full_task_recall:>9.1%}  "
            f"{result.tokens_in:>6}/{result.tokens_out:<6}  "
            f"{result.duration_seconds:>7.1f}"
        )
    if args.details:
        print("\nMisses (case[missing tools]):")
        for result in results:
            misses = ", ".join(result.missed_cases) or "none"
            print(f"- {result.catalog}: {misses}")


if __name__ == "__main__":
    main()
