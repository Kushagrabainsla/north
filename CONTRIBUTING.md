# Contributing to north

north is a personal AI operating system. Before making changes, read:

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) - the full system specification.
- [docs/CODING_STYLE.md](docs/CODING_STYLE.md) - coding conventions and the process rules in Section 23.

## Quick start

```bash
git clone https://github.com/your-username/north
cd north
uv sync
uv run pytest
```

Tests must pass before any change is merged.

## The three key flows

Per `docs/CODING_STYLE.md` Section 21.3:

### 1. Adding a new agent

Follows `docs/CODING_STYLE.md` Section 15 and `docs/ARCHITECTURE.md` Section 7. Briefly: drop a folder into `/agents/<name>/` with `agent.py`, `config.yaml`, `tools.yaml`, and `prompts/system.md`. The Orchestrator discovers it at startup. `north agent create` scaffolds all of this for you. See `agents/coder/` for a worked example.

### 2. Adding a new tool

Follows `docs/CODING_STYLE.md` Section 16 and `docs/ARCHITECTURE.md` Section 7.4. Briefly: implement the `Tool` ABC and drop the file in the right `tools/` subdir (`universal/` for all agents; `specialized/`, `semantic/`, or `analysis/` otherwise) - `ToolRegistry` discovers it automatically. For a specialized tool, list its name in the `tools.yaml` of each agent that should get it.

### 3. Running the test suite

```bash
uv run pytest                  # everything
uv run pytest tests/unit/      # unit tests only
uv run pytest -m integration   # integration tests only
```

Test framework, structure, and conventions live in `docs/CODING_STYLE.md` Section 18.

## Process rules

All contributors (human or AI) follow `docs/CODING_STYLE.md` Section 23:

- **If unsure about anything, ask.** Present two-to-four concrete options with trade-offs.
- **Confirm before substantive changes.** Once a pattern is approved, batch similar follow-ups without re-asking.
- **New tech goes through research → reason → propose → apply.** No new dependency lands without explicit sign-off.
- **Tests are written in the same change as functionality.** Adding code without adding or updating tests is an incomplete change.
- **Every change updates `CHANGELOG.md`.** Follow the [Keep a Changelog](https://keepachangelog.com) convention.

## Reporting issues

- Bugs, design questions, feature requests → open a GitHub issue.
- Security issues → see [SECURITY.md](SECURITY.md). Do **not** open a public issue.
