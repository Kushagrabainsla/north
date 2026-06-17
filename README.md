# north

**Personal AI operating system.** You give north your goals (your north stars). It handles coordination across health, finances, academics, and career in the background. You review and approve.

---

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/Kushagrabainsla/north/main/scripts/install.sh | bash
```

Add your API key to `~/.north/.env`:

```
NORTH_OPENROUTER_API_KEY=sk-or-...
```

---

## Commands

| Command | What it does |
|---|---|
| `north` | Open the TUI (starts server if needed) |
| `north start` | Start server + TUI |
| `north start --no-chat` | Start server only |
| `north stop` | Stop the server |
| `north reset` | Wipe data, keep API key |
| `north reset --all` | Wipe everything |
| `north update` | Update to latest version |
| | |
| `north task "..."` | Submit a task |
| `north task cancel <id>` | Cancel a task |
| `north tasks` | List active tasks |
| `north stream <id>` | Stream raw events for a task |
| | |
| `north ledger` | View the audit log |
| `north jobs` | List scheduled jobs |
| `north job cancel <id>` | Cancel a job |
| | |
| `north agents` | List registered agents |
| `north agent run <name> <task>` | Run an agent manually |
| `north agent create` | Scaffold a new agent |
| | |
| `north context show north_stars` | View your current goals |
| `north context edit judgement_rules` | Edit approval rules |
| `north context add --text "..."` | Add text to your context |
| `north context add --file resume.pdf` | Add a document |
| `north context add --url <url>` | Add a web page |
| | |
| `north inference costs` | Show inference cost summary |
| `north inference models` | Show model pool state |
| `north tools confidence` | Show tool confidence scores |
| `north config set <key> <value>` | Set a config value |
| `north metrics` | Show system performance metrics |

---

## Development

```bash
uv sync                  # install deps
uv run pytest            # run tests
uv run ruff check        # lint
uv run ruff check --fix  # auto-fix lint
```

- **Architecture:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- **Contributing:** [CONTRIBUTING.md](CONTRIBUTING.md)
