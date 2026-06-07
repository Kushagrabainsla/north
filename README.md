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

The install script installs the `north` CLI via `uv` and prompts for your [OpenRouter](https://openrouter.ai/keys) API key. That's the only required key — OpenRouter covers all model tiers.

`north start` boots the server and drops into the interactive TUI. The Web UI is also available at `http://127.0.0.1:8000/ui/`.

### Optional: additional inference providers

Add either key to `~/.north/.env` to activate that provider. Direct providers are preferred over OpenRouter for their own models, giving you lower latency and separate rate-limit buckets.

```bash
# ~/.north/.env
NORTH_OPENROUTER_API_KEY=sk-or-...   # required
NORTH_GROQ_API_KEY=gsk_...           # optional — fast free-tier completions + Whisper
NORTH_GEMINI_API_KEY=AIza...         # optional — Gemini free-tier completions + embeddings
```

No code changes needed — adding a key activates the provider automatically on next start.

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
north start --docker     # start via Docker Compose
north stop               # stop
```

Requires Docker with the Compose plugin and a `docker-compose.yml` in the current directory or `~/.north/`.
Note: Docker mode isolates north from your local network, so LAN device control (e.g. smart home) won't work in this mode.

When ready:

```
★ north  Orchestrator → http://127.0.0.1:8000
         Web UI       → http://127.0.0.1:8000/ui/
         API docs     → http://127.0.0.1:8000/docs
```

Submit a task from the TUI or Web UI to confirm everything works.

---

## Usage

```bash
north                 # open interactive TUI (auto-starts server if needed)
north start           # start server + TUI
north start --no-chat # start server only (headless)
north stop            # stop the server

# Submit tasks
north task "Help me prep for my first week at LinkedIn"
north task "What assignments are due this week?"

# View activity
north tasks           # active tasks
north ledger          # full event log
north stream <id>     # raw SSE stream for a task (debug)

# Context
north context show north_stars
north context edit judgement_rules   # opens in $EDITOR
north context add --text "I prefer mornings for deep work"
north context add --file resume.pdf
north context add --url "https://example.com/article"

# Agents
north agents
north agent run health --task "meal plan for today"
north agent create    # scaffold a new agent interactively

# Costs and inference
north inference costs --period week
north inference models
north metrics         # per-agent task counts, success rates, p50/p95 durations

# Tools
north tools confidence --agent health
```

Running `north` with no subcommand opens the full TUI — chat, live tool activity, and inline approvals in one terminal. The Web UI at `localhost:8000/ui` gives the same view on a second monitor with a richer layout.

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

Python 3.12+ · FastAPI · SQLite · HTMX · OpenRouter / Groq / Gemini · uv

---

*You set the destination. north handles the navigation.*
