<p align="center">
  <img src="docs/assets/north-logo.svg" alt="north." width="100%">
</p>

<p align="center">A digital version of you that learns how you think and earns autonomy over time.</p>

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/Kushagrabainsla/north/main/scripts/install.sh | bash
```

The installer sets up `uv`, installs north from GitHub, and asks for an OpenRouter key (you can skip it). Then open the dashboard:

```bash
north web
```

Onboarding walks you through each connection: AI providers, coding agents, browser profiles and Telegram. They all stay in **Settings → Connections**. For the terminal UI, run `north`.

## Commands

**Run north**

| Command | What it does |
|---|---|
| `north` | Open the terminal UI (starts the server if needed) |
| `north --yolo` | Switch north to yolo: every approval is yes, until you set another mode |
| `north start` | Start the server and the terminal UI (`--no-chat` for the server only, `-w <dir>`, `--host`, `--port`, `--docker`) |
| `north web` | Start the server if needed and open the dashboard (`--no-open`, `-w <dir>`) |
| `north stop` | Stop the server (`--all` stops every north process, `--docker`) |
| `north status` | Server health, approval mode, models, providers, browser and agents |
| `north setup` | Enter OpenRouter, Groq and Gemini keys and Telegram in the terminal |
| `north update` | Install the latest north from GitHub main and restart it (`--source local --path <dir>` for a checkout, `--no-restart`) |
| `north reset` | Wipe north's data, keeping keys and logins (`--all` removes those too) |

**Work**

| Command | What it does |
|---|---|
| `north task "..."` | Submit a task and stream it live (`-w <dir>`) |
| `north tasks` | List active tasks |
| `north stream <id>` | Raw events for a task |
| `north cancel <id>` | Stop a task or job (`--all` stops everything) |
| `north dictate` | Push-to-talk: hold Right Option+Space, speak, release (`--hotkey`) |
| `north agents` / `north agent list` | List agents |
| `north agent run <name> "<task>"` | Run one agent directly, without the planner |
| `north agent create` | Scaffold a new agent |

**Schedules and history**

| Command | What it does |
|---|---|
| `north cron` / `north cron list` | List schedules |
| `north cron add <flow> --hour 7` | Run a flow on a schedule, in local time (`--minute`, `--days weekdays`, `--name`, `--label`) |
| `north cron set <name>` | Change a schedule (`--hour`, `--minute`, `--days`, `--flow`, `--pause`, `--resume`) |
| `north cron rm <name>` | Remove a schedule |
| `north jobs` | List jobs (`--status`, `-n`) |
| `north job cancel <id>` | Cancel a job |
| `north ledger` | Recent audit log entries (`--task <id>`, `--agent`, `-n`) |
| `north ledger search "<text>"` | Search the audit log |
| `north metrics` | Performance metrics (`-p <days>`) |

**Memory**

| Command | What it does |
|---|---|
| `north context show <doc>` | Show a context document (`user`, `north_stars`, `judgement_rules`, `soul`) |
| `north context edit <doc>` | Edit it in `$EDITOR` |
| `north context add --text "..."` | Add to your context (`--file <path>`, `--url <url>`) |

**Models**

| Command | What it does |
|---|---|
| `north auth login` | Sign in to OpenAI Codex in the browser (`north auth status`, `north auth logout`) |
| `north models` | Models north can reach |
| `north routing` | Why each part of a task ran on its model (`--task <id>`) |
| `north speed` | Measured speed of each model |
| `north limits` | Rate limits and cooldowns, with reset times |
| `north inference costs` | Cost summary (`--period day\|week\|month`) |

**Settings**

| Command | What it does |
|---|---|
| `north config list` | All settings and their values |
| `north config get <key>` | Read one |
| `north config set <key> <value>` | Change one, saved to `~/.north/.env` |
| `north tools confidence` | Tool confidence per agent |

In the terminal UI, type `/help` for its slash commands.
