"""Offline benchmark for North's global tool-selection strategies.

The benchmark imports tool metadata but never executes a tool or calls a
completion model. Dense retrieval uses North's local embedding provider, so the
comparison is repeatable, private, and free.

Run:
    .venv/bin/python -m experiments.tool_selection.retrieval_benchmark
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from inference.models import EmbedRequest
from inference.providers.local_embeddings import DEFAULT_MODEL_ID, LocalEmbeddingProvider
from tools.base import Tool
from tools.retrieval import bm25_rank, reciprocal_rank_fusion, tool_retrieval_profile
from utils.math import cosine_similarity

_ROOT = Path(__file__).resolve().parents[2]
_CASES_PATH = Path(__file__).with_name("cases.json")
_TOOL_PACKAGES = ("universal", "analysis", "semantic", "specialized")


@dataclass(frozen=True)
class ToolDoc:
    name: str
    description: str
    parameters_schema: dict[str, Any]

    @property
    def description_profile(self) -> str:
        return self.description

    @property
    def enriched_profile(self) -> str:
        """Retrieval profile: identity, purpose, and argument semantics."""
        return tool_retrieval_profile(self)

    @property
    def schema_chars(self) -> int:
        schema = {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }
        return len(json.dumps(schema, sort_keys=True))


@dataclass(frozen=True)
class EvalCase:
    id: str
    prompt: str
    expected: tuple[str, ...]
    kind: str


@dataclass(frozen=True)
class StrategyResult:
    strategy: str
    top_k: str
    tool_recall: float
    full_task_recall: float
    single_tool_top1: float | None
    average_tools_loaded: float
    average_schema_chars: float
    schema_reduction: float
    missed_cases: tuple[str, ...]


def discover_tool_docs(root: Path = _ROOT) -> dict[str, ToolDoc]:
    """Read every concrete Tool class, including dependency-injected tools."""
    docs: dict[str, ToolDoc] = {}
    tools_root = root / "tools"
    for package_name in _TOOL_PACKAGES:
        package_dir = tools_root / package_name
        for path in sorted(package_dir.glob("*.py")):
            if path.name.startswith("_"):
                continue
            module_name = f"tools.{package_name}.{path.stem}"
            module = importlib.import_module(module_name)
            for value in vars(module).values():
                if not (
                    inspect.isclass(value)
                    and issubclass(value, Tool)
                    and value is not Tool
                    and not inspect.isabstract(value)
                    and value.__module__ == module_name
                ):
                    continue
                name = getattr(value, "name", "")
                description = getattr(value, "description", "")
                parameters = getattr(value, "parameters_schema", {})
                if isinstance(name, str) and name and isinstance(description, str):
                    docs[name] = ToolDoc(
                        name=name,
                        description=description,
                        parameters_schema=parameters if isinstance(parameters, dict) else {},
                    )
    return docs


def load_cases(path: Path = _CASES_PATH) -> list[EvalCase]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    return [
        EvalCase(
            id=str(row["id"]),
            prompt=str(row["prompt"]),
            expected=tuple(map(str, row["expected"])),
            kind=str(row["kind"]),
        )
        for row in rows
    ]


async def dense_ranks(queries: list[str], documents: dict[str, str]) -> list[list[str]]:
    """Rank all tools for each query with North's production local encoder."""
    provider = LocalEmbeddingProvider()
    names = list(documents)
    texts = [documents[name] for name in names] + queries
    response = await provider.embed(
        DEFAULT_MODEL_ID,
        EmbedRequest(texts=texts, component="tool_selection_eval"),
    )
    document_vectors = response.embeddings[: len(names)]
    query_vectors = response.embeddings[len(names) :]
    rankings: list[list[str]] = []
    for query_vector in query_vectors:
        scored = [
            (cosine_similarity(query_vector, vector), name)
            for name, vector in zip(names, document_vectors, strict=True)
        ]
        scored.sort(key=lambda item: (-item[0], item[1]))
        rankings.append([name for _, name in scored])
    return rankings


def _adaptive_k(prompt: str) -> int:
    """Choose a wider set from observable multi-step language only."""
    multi_signal = bool(
        re.search(r"\b(and|then|before|after|also)\b", prompt.lower())
    )
    return 8 if multi_signal else 5


def evaluate_strategy(
    name: str,
    cases: list[EvalCase],
    rankings: list[list[str]],
    docs: dict[str, ToolDoc],
    top_k: int | None,
    *,
    ranked: bool = True,
) -> StrategyResult:
    """Measure retrieval correctness and schema footprint for one isolated arm."""
    total_expected = 0
    total_recalled = 0
    full_recall = 0
    single_top1 = 0
    single_count = 0
    loaded_counts: list[int] = []
    loaded_chars: list[int] = []
    missed_cases: list[str] = []
    full_schema_chars = sum(doc.schema_chars for doc in docs.values())
    for case, ranking in zip(cases, rankings, strict=True):
        limit = _adaptive_k(case.prompt) if top_k is None else top_k
        selected = ranking[:limit]
        selected_set = set(selected)
        expected_set = set(case.expected)
        total_expected += len(expected_set)
        total_recalled += len(expected_set & selected_set)
        fully_recalled = expected_set <= selected_set
        full_recall += int(fully_recalled)
        if not fully_recalled:
            missing = ",".join(sorted(expected_set - selected_set))
            missed_cases.append(f"{case.id}[{missing}]")
        if case.kind == "single":
            single_count += 1
            single_top1 += int(bool(selected) and selected[0] in expected_set)
        loaded_counts.append(len(selected))
        loaded_chars.append(sum(docs[tool].schema_chars for tool in selected))
    average_chars = sum(loaded_chars) / len(loaded_chars)
    return StrategyResult(
        strategy=name,
        top_k="adaptive 5/8" if top_k is None else str(top_k),
        tool_recall=total_recalled / total_expected,
        full_task_recall=full_recall / len(cases),
        single_tool_top1=single_top1 / single_count if ranked else None,
        average_tools_loaded=sum(loaded_counts) / len(loaded_counts),
        average_schema_chars=average_chars,
        schema_reduction=1 - average_chars / full_schema_chars,
        missed_cases=tuple(missed_cases),
    )


def render_report(results: list[StrategyResult], docs: dict[str, ToolDoc], cases: list[EvalCase]) -> str:
    full_chars = sum(doc.schema_chars for doc in docs.values())
    one_line_catalog = "\n".join(
        f"- {doc.name}: {doc.description.split('. ')[0].strip()}"
        for doc in docs.values()
    )
    name_catalog = ", ".join(sorted(docs))
    lines = [
        "North tool-selection benchmark",
        f"Catalog: {len(docs)} tools, {full_chars:,} full-schema characters",
        f"Catalog hints: {len(one_line_catalog):,} one-line-description characters, "
        f"{len(name_catalog):,} name-only characters",
        f"Cases: {len(cases)} ({sum(c.kind == 'single' for c in cases)} single, "
        f"{sum(c.kind == 'multi' for c in cases)} multi)",
        "",
        "strategy                 k             recall  full-task  top1(single)  avg tools  schema saved",
    ]
    for result in results:
        top1 = "n/a" if result.single_tool_top1 is None else f"{result.single_tool_top1:.1%}"
        lines.append(
            f"{result.strategy:<24} {result.top_k:<13} "
            f"{result.tool_recall:>6.1%}  {result.full_task_recall:>9.1%}  "
            f"{top1:>12}  {result.average_tools_loaded:>9.1f}  "
            f"{result.schema_reduction:>11.1%}"
        )
    return "\n".join(lines)


async def run_benchmark(
    cases_path: Path = _CASES_PATH,
) -> tuple[list[StrategyResult], dict[str, ToolDoc], list[EvalCase]]:
    docs = discover_tool_docs()
    cases = load_cases(cases_path)
    missing = sorted({name for case in cases for name in case.expected} - set(docs))
    if missing:
        raise ValueError(f"Benchmark labels reference unknown tools: {missing}")

    descriptions = {name: doc.description_profile for name, doc in docs.items()}
    profiles = {name: doc.enriched_profile for name, doc in docs.items()}
    queries = [case.prompt for case in cases]
    dense_description, dense_profile = await asyncio.gather(
        dense_ranks(queries, descriptions),
        dense_ranks(queries, profiles),
    )
    bm25_profile = [bm25_rank(query, profiles) for query in queries]
    hybrid_profile = [
        reciprocal_rank_fusion(dense, lexical, exact_query=case.prompt)
        for case, dense, lexical in zip(cases, dense_profile, bm25_profile, strict=True)
    ]
    all_rankings = [sorted(docs) for _ in cases]

    results = [
        evaluate_strategy("all schemas", cases, all_rankings, docs, len(docs), ranked=False),
        evaluate_strategy("hybrid enriched", cases, hybrid_profile, docs, 3),
        evaluate_strategy("dense descriptions", cases, dense_description, docs, 5),
        evaluate_strategy("dense enriched", cases, dense_profile, docs, 5),
        evaluate_strategy("BM25 enriched", cases, bm25_profile, docs, 5),
        evaluate_strategy("hybrid enriched", cases, hybrid_profile, docs, 5),
        evaluate_strategy("hybrid enriched", cases, hybrid_profile, docs, 8),
        evaluate_strategy("hybrid enriched", cases, hybrid_profile, docs, 10),
        evaluate_strategy("hybrid enriched", cases, hybrid_profile, docs, None),
        evaluate_strategy("dense descriptions", cases, dense_description, docs, 15),
    ]
    return results, docs, cases


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=_CASES_PATH)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable results.")
    parser.add_argument("--details", action="store_true", help="List cases missed by each arm.")
    args = parser.parse_args()
    results, docs, cases = asyncio.run(run_benchmark(args.cases))
    if args.json:
        print(json.dumps([asdict(result) for result in results], indent=2))
    else:
        print(render_report(results, docs, cases))
        if args.details:
            print("\nMisses (case[missing tools]):")
            for result in results:
                misses = ", ".join(result.missed_cases) or "none"
                print(f"- {result.strategy} @ {result.top_k}: {misses}")


if __name__ == "__main__":
    main()
