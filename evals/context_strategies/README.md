# Context strategies: an A/B bench

Does north's context handling need changing? This answers that with numbers
instead of a judgement call.

It compares what north does today against the Recursive Language Model idea
(Zhang, Kraska & Khattab, [arXiv:2512.24601](https://arxiv.org/abs/2512.24601)),
which puts the prompt in a REPL as a variable and has the model write code to
examine it rather than reading it.

**Nothing here is wired into north.** It is a bench, so the decision to adopt
comes after the measurement, not before.

## What is held fixed

One cell = one strategy answering one task once. The corpus, the question, the
model and the grader are identical across a column, so the only thing that
differs between two rows is the strategy.

## The strategies

| name | what it is | who does this |
|---|---|---|
| `raw` | whole context in one prompt | the control - what a model does unaided |
| `truncate` | cut the context to a head and a tail | **north today** (`agents/context_compaction.py`) |
| `compact` | summarise each chunk, then answer from the summaries | **north today** (the compaction agent) |
| `repl` | context bound to a REPL variable; the model writes code to inspect it | RLM at depth 0 |
| `repl_recursive` | the same, plus `llm_query()` callable from inside loops | RLM at depth 1 |

Adding a sixth is one function in `strategies.py` and one entry in `STRATEGIES`.

## The tasks

Zhang et al.'s central claim is that a model's usable context depends on how much
of the input the answer needs, not merely on input length. So tasks vary by
complexity, and each has ground truth known by construction:

| task | needs | answer | scored by |
|---|---|---|---|
| `needle` | O(1) - one line | an access code | exact match |
| `aggregate` | O(n) - every line | a count | exact number |
| `pairs` | O(n²) - every pair | 4 planted record pairs | F1 over the set |
| `control_short` | a short prompt | a name | exact match |

`control_short` is not decoration. The paper reports a scaffold that lifted
long-context scores while *halving* short reasoning scores (MATH 26.0 → 5.6).
If a strategy does that here, this row is where it shows.

Two things keep the tasks honest:

- **The pairs are planted**, so the answer is 4 pairs whether the corpus is 8K or
  1M. Found pairs would explode to 5,792 at 256K and every strategy would score
  zero for running out of output tokens - measuring the token limit, not the
  strategy.
- **The planted project codes look like every other code.** A code containing
  "SHARED" would let one grep answer a task meant to need the whole corpus.

## Running it

```bash
# Prove the harness without spending anything. A scripted model answers every
# task correctly, so anything below 1.00 means the harness is broken.
.venv/bin/python -m evals.context_strategies.run --dry-run

# A real run, against north's own router and your configured models.
.venv/bin/python -m evals.context_strategies.run --sizes 8000,64000 --trials 3

# Options
#   --strategies repl,compact   compare a subset
#   --sizes 8000,64000,256000   context lengths in characters
#   --trials 3                  repeats per cell; models are stochastic
#   --pool reasoning            pin a model pool
#   --max-cost-usd 2.0          stop once this much has been spent
```

Every run writes `results.json` with the per-cell answer, model, tokens, cost and
trajectory notes, so a surprising number can be traced to what actually happened.

## The run that actually answers the question

The first real run used 8K and 64K corpora and `raw` won. That is expected and it
settles nothing: 64K characters is only ~18,600 tokens, which every model here
swallows whole. Nothing was under pressure, so nothing needed fixing. Notably the
REPL strategies sent **879 tokens** where `raw` sent 18,575 for the same task -
the mechanism works, it just was not needed yet.

The paper's effect lives past that point. To reach it:

```bash
.venv/bin/python -u -m evals.context_strategies.run \
  --sizes 8000,250000,1000000 --trials 5 --model codex:gpt-5.5
```

1M characters is ~250K tokens, near the top of a frontier window, so `raw` starts
to fail there rather than merely score badly. Cells that could not run at all show
`n/a*` instead of `0.00`, because "did not fit" and "got it wrong" are different
findings and the second is not the strategy's fault.

Keep `8000` in the list. It is the control: a strategy that wins at 1M while
losing at 8K has moved the problem rather than solved it.

## Reading the output

Three tables: score by task, score by task complexity, and what each strategy
cost. Read them together. A strategy that wins the first table by making ten
times as many calls has not won; the paper's own claim is parity on cost, and
that is checkable here rather than assumed.

Judge on **trials ≥ 3**. A single trial per cell is a smoke test, not evidence: at two
trials, 9 of 35 cells disagreed with themselves, several flipping 1.0 to 0.0. Treat any
gap under about 0.2 as noise until more trials say otherwise.

## Caveats

- The REPL runs model-written Python in a restricted namespace - no file access,
  no network, and imports limited to pure-computation modules (`re`, `json`,
  `collections`, `itertools`, `math`, `string`, `statistics`). That is enough for
  synthetic data on your own machine.
  Adopting any of this in north would need the real sandbox
  (`tools/specialized/_sandbox.py`), because production context is your data.
- The corpus is synthetic. It is shaped like north's fact store, but a result
  here is evidence about the mechanism, not a promise about your files.
- The ceiling on every score is the model. A weak or rate-limited model scores
  low whatever the strategy, and `errors` in the cost table is how you tell that
  apart from a strategy failing.
