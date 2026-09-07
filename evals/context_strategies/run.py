"""Run the strategy matrix and print the comparison.

    .venv/bin/python -m evals.context_strategies.run --dry-run
    .venv/bin/python -m evals.context_strategies.run --sizes 8000,64000 --trials 2

One cell = one strategy answering one task once. Everything else is held fixed,
so the difference between two rows is the strategy and nothing else.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from evals.context_strategies.corpus import Task, build_tasks
from evals.context_strategies.grading import score
from evals.context_strategies.llm import Ask, ScriptedRouter, pinned_router
from evals.context_strategies.strategies import STRATEGIES

_DEFAULT_SIZES = "8000,64000"
_DEFAULT_STRATEGIES = "raw,truncate,compact,repl,repl_recursive"
_ANSWER_KEEP_CHARS = 300


@dataclass
class CellResult:
    """One strategy's attempt at one task."""

    strategy: str
    task_id: str
    kind: str
    complexity: str
    context_chars: int
    trial: int
    score: float = 0.0
    model: str = ""
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0
    iterations: int = 0
    error: str = ""
    answer: str = ""
    notes: list[str] = field(default_factory=list)


async def run_cell(strategy_name: str, task: Task, router: object | None, trial: int, pool: str | None) -> CellResult:
    ask = Ask(router=router or ScriptedRouter(task.gold_reply), pool=pool, component=f"eval:{strategy_name}")
    result = CellResult(
        strategy=strategy_name,
        task_id=task.id,
        kind=task.kind,
        complexity=task.complexity,
        context_chars=task.context_chars,
        trial=trial,
    )
    started = time.monotonic()
    try:
        run = await STRATEGIES[strategy_name](task, ask)
        result.score = score(task.scorer, task.answer, run.answer)
        result.answer = run.answer[:_ANSWER_KEEP_CHARS]
        result.iterations = run.iterations
        result.error = run.error
        result.notes = run.notes
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    result.seconds = round(time.monotonic() - started, 1)
    result.model = ask.model
    result.llm_calls = ask.call_count
    result.tokens_in, result.tokens_out = ask.tokens_in, ask.tokens_out
    result.cost_usd = round(ask.cost_usd, 6)
    return result


async def run_matrix(
    strategies: list[str],
    tasks: list[Task],
    router: object | None,
    *,
    trials: int,
    pool: str | None,
    max_cost_usd: float,
) -> list[CellResult]:
    results: list[CellResult] = []
    spent = 0.0
    for trial in range(1, trials + 1):
        for task in tasks:
            for strategy_name in strategies:
                if spent >= max_cost_usd:
                    print(f"\n  stopping: spent ${spent:.2f}, over the --max-cost-usd ceiling")
                    return results
                result = await run_cell(strategy_name, task, router, trial, pool)
                spent += result.cost_usd
                results.append(result)
                # Flushed: a long run redirected to a file shows no progress otherwise.
                print(f"  {_cell_line(result)}  (${spent:.3f} so far)", flush=True)
    return results


def _cell_line(result: CellResult) -> str:
    status = f"{result.score:.2f}"
    if result.error:
        status += f"  [{result.error[:60]}]"
    return (
        f"{result.strategy:<16} {result.task_id:<18} score={status:<10} "
        f"calls={result.llm_calls:<3} {result.seconds:>5.1f}s"
    )


def _mean(values: list[float]) -> float:
    return statistics.mean(values) if values else 0.0


def _failed_to_run(result: CellResult) -> bool:
    """True when the cell never got an answer out of the model at all.

    A model that could not accept the input has not answered wrongly - it has not
    answered. Scoring that as 0.0 alongside a wrong answer would read as "this
    strategy is bad" when it means "this strategy could not run", which at 1M
    characters is the single most important thing the table has to say.
    """
    return result.llm_calls == 0 or bool(result.error and not result.answer)


def _cell_text(scores: list[float], failures: int) -> str:
    if not scores:
        return "-"
    if failures == len(scores):
        return "n/a*"
    return f"{_mean(scores):.2f}" + ("*" if failures else "")


def _score_table(results: list[CellResult]) -> str:
    """Strategies down the side, tasks across the top, mean score in the cells.

    A trailing * means at least one trial could not run; n/a* means none could.
    """
    by_cell: dict[tuple[str, str], list[float]] = defaultdict(list)
    failed: dict[tuple[str, str], int] = defaultdict(int)
    for result in results:
        by_cell[(result.strategy, result.task_id)].append(result.score)
        failed[(result.strategy, result.task_id)] += _failed_to_run(result)
    strategies = sorted({r.strategy for r in results}, key=list(STRATEGIES).index)
    task_ids = sorted({r.task_id for r in results}, key=lambda t: (t.split("_")[0], len(t), t))

    header = f"{'strategy':<16}" + "".join(f"{task:>20}" for task in task_ids) + f"{'mean':>10}"
    lines = [header, "-" * len(header)]
    for strategy in strategies:
        texts = [
            _cell_text(by_cell.get((strategy, task), []), failed[(strategy, task)]) for task in task_ids
        ]
        # The mean is over trials that actually ran, so a strategy is neither
        # rewarded nor punished for cells it could not attempt at all.
        answered = [r.score for r in results if r.strategy == strategy and not _failed_to_run(r)]
        mean_text = f"{_mean(answered):.2f}" if answered else "n/a*"
        row = f"{strategy:<16}" + "".join(f"{text:>20}" for text in texts)
        lines.append(row + f"{mean_text:>10}")
    return "\n".join(lines)


def _cost_table(results: list[CellResult]) -> str:
    by_strategy: dict[str, list[CellResult]] = defaultdict(list)
    for result in results:
        by_strategy[result.strategy].append(result)

    header = (
        f"{'strategy':<16}{'mean score':>12}{'calls':>8}{'tokens in':>12}"
        f"{'tokens out':>12}{'cost $':>10}{'sec':>8}{'errors':>8}"
    )
    lines = [header, "-" * len(header)]
    for strategy in sorted(by_strategy, key=list(STRATEGIES).index):
        rows = by_strategy[strategy]
        lines.append(
            f"{strategy:<16}"
            f"{_mean([r.score for r in rows]):>12.2f}"
            f"{_mean([float(r.llm_calls) for r in rows]):>8.1f}"
            f"{_mean([float(r.tokens_in) for r in rows]):>12.0f}"
            f"{_mean([float(r.tokens_out) for r in rows]):>12.0f}"
            f"{sum(r.cost_usd for r in rows):>10.4f}"
            f"{_mean([r.seconds for r in rows]):>8.1f}"
            f"{sum(1 for r in rows if r.error):>8}"
        )
    return "\n".join(lines)


def _complexity_table(results: list[CellResult]) -> str:
    """The paper's central claim, restated as a table: does the gap widen?"""
    by_key: dict[tuple[str, str], list[float]] = defaultdict(list)
    for result in results:
        by_key[(result.strategy, result.complexity)].append(result.score)
    complexities = sorted({r.complexity for r in results})
    strategies = sorted({r.strategy for r in results}, key=list(STRATEGIES).index)

    header = f"{'strategy':<16}" + "".join(f"{c:>14}" for c in complexities)
    lines = [header, "-" * len(header)]
    for strategy in strategies:
        cells = "".join(f"{_mean(by_key.get((strategy, c), [])):>14.2f}" for c in complexities)
        lines.append(f"{strategy:<16}{cells}")
    return "\n".join(lines)


def _build_router(dry_run: bool, model: str | None) -> object | None:
    """The model every cell shares: scripted, pinned, or north's own router."""
    if dry_run:
        return None
    if model:
        return pinned_router(model)
    from config.dependencies import build_production_dependencies

    return build_production_dependencies().cost_tracker


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare context strategies on identical inputs.")
    parser.add_argument("--strategies", default=_DEFAULT_STRATEGIES, help="comma-separated, in report order")
    parser.add_argument("--sizes", default=_DEFAULT_SIZES, help="context sizes in characters")
    parser.add_argument("--trials", type=int, default=1, help="repeats per cell (models are stochastic)")
    parser.add_argument("--pool", default=None, help="pin a model pool, e.g. reasoning")
    parser.add_argument(
        "--model",
        default=None,
        help="pin one model as provider:model_id (e.g. openrouter:qwen/qwen3-30b). "
        "Without it north routes freely and cells may not be comparable.",
    )
    parser.add_argument("--seed", type=int, default=0, help="corpus seed")
    parser.add_argument("--max-cost-usd", type=float, default=2.0, help="stop once this much has been spent")
    parser.add_argument("--dry-run", action="store_true", help="use a scripted model; spends nothing")
    parser.add_argument("--out", default="evals/context_strategies/results.json")
    return parser.parse_args()


async def _main() -> None:
    args = _parse_args()
    sizes = [int(size) for size in args.sizes.split(",") if size.strip()]
    strategies = [name.strip() for name in args.strategies.split(",") if name.strip()]
    unknown = set(strategies) - set(STRATEGIES)
    if unknown:
        raise SystemExit(f"unknown strategies: {sorted(unknown)}. Known: {sorted(STRATEGIES)}")

    tasks = build_tasks(sizes, seed=args.seed)
    print(f"\n  {len(strategies)} strategies x {len(tasks)} tasks x {args.trials} trial(s) "
          f"= {len(strategies) * len(tasks) * args.trials} cells")
    print(f"  tasks: {', '.join(f'{t.id}({t.complexity})' for t in tasks)}")
    print(f"  ceiling: ${args.max_cost_usd:.2f}" + ("   [dry run - no spend]" if args.dry_run else "") + "\n")

    router = _build_router(args.dry_run, args.model)
    results = await run_matrix(
        strategies, tasks, router, trials=args.trials, pool=args.pool, max_cost_usd=args.max_cost_usd
    )

    print("\n\n=== Score by task (mean over trials) ===\n")
    print(_score_table(results))
    print("\n\n=== Score by task complexity ===\n")
    print(_complexity_table(results))
    print("\n\n=== Cost of each strategy ===\n")
    print(_cost_table(results))

    models = sorted({r.model for r in results if r.model})
    print(f"\n  models used: {', '.join(models) or '(none)'}")
    if len(models) > 1:
        print(
            "  WARNING: more than one model answered, so these cells are NOT comparable -\n"
            "           the numbers compare models, not strategies. Re-run with --model."
        )
    print(f"  total spend: ${sum(r.cost_usd for r in results):.4f}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8")
    print(f"  full results: {out_path}\n")


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
