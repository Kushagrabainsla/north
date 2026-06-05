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

```bash
curl -fsSL https://raw.githubusercontent.com/Kushagrabainsla/north/main/scripts/install.sh | bash
north start
```

The install script installs the `north` CLI via `uv` and prompts for your [OpenRouter](https://openrouter.ai/keys) API key. That's it.

### Manual install (alternative)

Requires **Python 3.12+** and [uv](https://docs.astral.sh/uv/).

```bash
uv tool install git+https://github.com/Kushagrabainsla/north
echo "NORTH_OPENROUTER_API_KEY=sk-or-your-key" >> ~/.north/.env
north start
```

### Server / headless deployments

Running north on a remote server or home server? Use the Docker Compose mode:

```bash
north start --docker   # start
north stop --docker    # stop
```

This requires Docker and a `docker-compose.yml` in the current directory or `~/.north/`.
Note: Docker mode isolates north from your local network, so LAN device control (e.g. smart home) won't work in this mode.

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
north context show north_stars
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
Engineering deep-dives: [docs/TECHNICAL_FEATURES.md](docs/TECHNICAL_FEATURES.md)

---

## Uninstall / Reset

**Start fresh** — wipe all data but keep your API key:
```bash
north reset
```

**Full reset** — wipe everything including your API key and config:
```bash
north reset --all
```

Both commands stop the server automatically before wiping.

**Uninstall completely:**
```bash
north reset --all          # wipe all data
uv tool uninstall north    # remove the CLI
```

---

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) before making changes. The short version:

```bash
git clone https://github.com/Kushagrabainsla/north
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
