# Tool catalog and retrieval experiment

Date: 2026-09-13

## Decision

North keeps a sorted name-only tool manifest in the system prompt and retrieves
task-specific schemas from a universal catalog. Retrieval uses reciprocal-rank
fusion over local dense similarity and BM25 lexical matching, with eight results
by default. Explicitly named tools, loop-essential tools, skill dependencies,
and the always-visible `find_tools` recovery tool remain outside that limit.

This replaces a one-line-description manifest plus dense-only top-15 retrieval.

## Why

The isolated results are recorded in [`results.json`](results.json):

- On GPT-5.6 Terra, names-only and description catalogs both reached 95.4%
  tool recall. Names-only used 792 fewer input tokens and lost one full
  multi-tool task (93.9% vs 95.9%).
- On GPT-OSS 120B, names-only reached 92.3% tool recall versus 95.4% with
  descriptions, again using 792 fewer input tokens.
- Hybrid top-8 retrieval reached 93.8% tool recall and 93.9% full-task recall,
  versus 86.2% and 85.7% for dense-only top-15. It loaded 46.7% fewer schemas.
- A simple adaptive 5/8 cutoff performed worse than fixed top-8, so it was not
  shipped.

The mechanism also follows published tool-search findings: defer large tool
definitions, enrich the private retrieval representation, combine retrieval
signals, and keep a recovery path.

- [Anthropic: Tool search](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool)
- [ToolRerank](https://arxiv.org/abs/2403.06551)
- [Tool-DE](https://arxiv.org/abs/2510.22670)
- [ToolRet](https://arxiv.org/abs/2503.01763)

## Reproduce

The dataset has 49 hand-labeled prompts: 39 single-tool and 10 multi-tool tasks.
Neither harness executes tools.

```bash
# Offline, deterministic apart from the pinned local embedding runtime.
.venv/bin/python -m experiments.tool_selection.retrieval_benchmark --details

# Live awareness A/B through GPT-5.6 Terra.
.venv/bin/python -m experiments.tool_selection.awareness_benchmark \
  --provider openai_codex --model gpt-5.6-terra --details

# Live awareness A/B through GPT-OSS 120B.
.venv/bin/python -m experiments.tool_selection.awareness_benchmark \
  --provider groq --model openai/gpt-oss-120b --details
```

The offline harness imports the production profile, BM25, and fusion functions
from `tools/retrieval.py`; benchmark logic therefore cannot drift into a second
implementation of the algorithm it is meant to justify.

## Limitations

- The labels are curated, not sampled from production traffic.
- Awareness results are one temperature-zero batch per model, not confidence
  intervals over repeated calls.
- Awareness and retrieval are intentionally atomic tests. Their percentages
  must not be multiplied into a synthetic end-to-end success rate.
- Latency from one live call is too noisy to support a speed claim.

Production telemetry should track `find_tools` recovery calls and tasks whose
required tool was initially absent. That evidence can decide whether eight
should later become ten.
