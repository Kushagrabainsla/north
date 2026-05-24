# north

**A personal AI operating system that runs in the background so you don't have to manage your own life.**

You give north your goals — your north stars. It handles the coordination work across health, finances, academics, and career. You review, approve, and get on with the things that actually need you.

---

## What it does

- **Orchestrates agents** across life domains (health, university, job, finance) in parallel
- **Learns your preferences** from every decision you make — asks less over time
- **Checks your goals** before taking any consequential action
- **Runs on a schedule** — daily meal plans, deadline checks, budget summaries, without you asking
- **Stays local** — all data lives on your machine in SQLite and markdown files

---

## Quick start

You need an [OpenRouter](https://openrouter.ai) API key (one key for all LLM inference and voice transcription).

### Docker (recommended)

```bash
git clone https://github.com/your-username/north
cd north
cp .env.example .env
# Set NORTH_OPENROUTER_API_KEY and NORTH_SECRET in .env
north start
```

### Local install

```bash
# Install uv (Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone and install
git clone https://github.com/your-username/north
cd north
uv tool install .

# Configure
cp .env.example .env
# Set NORTH_OPENROUTER_API_KEY in .env

# Start
north start --local
```

When ready:

```
★ north  Orchestrator → http://127.0.0.1:8000
         Web UI       → http://127.0.0.1:8000/ui/
         API docs     → http://127.0.0.1:8000/docs
```

Open the Web UI and submit a task to confirm everything works.

---

## Usage

```bash
# Submit a task
north task "Help me prep for my first week at LinkedIn"
north task "What assignments are due this week?"

# View activity
north tasks           # active tasks
north ledger          # full event log

# Context
north context view north_stars
north context add --text "I prefer mornings for deep work"
north context add --file resume.pdf

# Agents
north agent list
north agent create    # scaffold a new agent interactively

# Costs
north inference costs --period week
```

The Web UI at `localhost:8000/ui` gives a live activity feed, approval surface, and full context editor — intended to run on a second monitor.

---

## How it works

north is built in eight layers: Perception → Orchestrator → Agent Layer → Approval Layer, with a Ledger (append-only audit trail) and Context Layer (your goals, preferences, and decision patterns) shared across everything.

Full architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

---

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) before making changes. The short version:

```bash
git clone https://github.com/your-username/north
cd north
uv sync
uv run pytest
```

- **Adding an agent:** drop a folder into `/agents/<name>/` — the Orchestrator discovers it automatically
- **Adding a tool:** implement the `Tool` ABC and register it in `TOOL_GRAPH`
- **Bugs and feature requests:** open a GitHub issue
- **Security issues:** see [SECURITY.md](SECURITY.md) — do not open a public issue

---

## Tech stack

Python 3.12+ · FastAPI · SQLite · HTMX · OpenRouter · uv

---

*You set the destination. north handles the navigation.*
