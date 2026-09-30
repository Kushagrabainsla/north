<p align="center">
  <img src="docs/assets/north-logo.svg" alt="north." width="360">
</p>

<p align="center">A digital version of you that learns how you think and earns autonomy over time.</p>

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/Kushagrabainsla/north/main/scripts/install.sh | bash
```

The installer sets up `uv`, installs north, and asks for an OpenRouter key (you can skip it). Groq and Gemini keys go in with `north setup`, OpenCode Zen on the dashboard, and OpenAI Codex signs in with `north auth login openai-codex`. Then:

```bash
north web       # the dashboard
north           # the terminal UI
```

Every provider can be added or changed later on the dashboard under **System → Providers**.

## Telegram

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Get your numeric Telegram user id, for example from [@userinfobot](https://t.me/userinfobot).
3. Run `north setup` and enter both, or put them in `~/.north/.env`:

   ```
   NORTH_TELEGRAM_BOT_TOKEN=123456:ABC...
   NORTH_TELEGRAM_ALLOWED_CHAT_IDS=123456789
   ```

4. Restart north: `north stop`, then `north start`.

Only the ids on that list can use the bot. With no list, the bot does not start.

In the chat, send any message or voice note to run a task. Commands: `/status`, `/cancel`, `/autonomy [mode]`, `/decisions`, `/limits`, `/help`.

## Commands

**Run north**

| Command | What it does |
|---|---|
| `north` | Open the terminal UI (starts the server if needed) |
| `north --yolo` | Switch north to yolo: every approval is yes, until you set another mode |
| `north start` | Start the server and the terminal UI (`--no-chat` for the server only, `--docker`, `--host`, `--port`, `-w <dir>`) |
| `north web` | Start the server and open the dashboard (`--no-open`) |
| `north stop` | Stop the server (`--all` stops every north process) |
| `north status` | Server health, dials, providers and agents |
| `north setup` | Set up provider keys, Telegram and settings |
| `north update` | Install the latest north (`--source local --path <dir>` for a checkout) |
| `north reset` | Wipe north's data (`--all` also removes keys and logins) |

**Work**

| Command | What it does |
|---|---|
| `north task "..."` | Submit a task and stream it live |
| `north tasks` | List running tasks |
| `north stream <id>` | Raw events for a task |
| `north cancel <id>` | Stop a task or job (`--all` stops everything) |
| `north dictate` | Push-to-talk voice input |
| `north agents` / `north agent list` | List agents |
| `north agent run <name> "<task>"` | Run one agent directly |
| `north agent create` | Scaffold a new agent |

**Schedules and history**

| Command | What it does |
|---|---|
| `north cron list` | List schedules |
| `north cron add <flow> --hour 7` | Run a flow on a schedule (`--minute`, `--days weekdays`, `--label`) |
| `north cron set <name>` | Change a schedule |
| `north cron rm <name>` | Remove a schedule |
| `north jobs` | List jobs |
| `north job cancel <id>` | Cancel a job |
| `north ledger search "<text>"` | Search the audit log |
| `north metrics` | Performance metrics |

**Memory**

| Command | What it does |
|---|---|
| `north context show <doc>` | Show a context document (`north_stars`, `judgement_rules`, `user`, `soul`) |
| `north context edit <doc>` | Edit it in `$EDITOR` |
| `north context add --text "..."` | Add to your context (`--file <path>`, `--url <url>`) |

**Models**

| Command | What it does |
|---|---|
| `north auth login openai-codex` | Sign in to OpenAI Codex (`status`, `logout`) |
| `north models` | Models north can reach |
| `north routing` | Why each part of a task ran on its model |
| `north speed` | Measured speed of each model |
| `north limits` | Rate limits and cooldowns, with reset times |
| `north inference costs` | Cost summary |

**Settings**

| Command | What it does |
|---|---|
| `north config list` | All settings and their values |
| `north config get <key>` | Read one |
| `north config set <key> <value>` | Change one |
| `north tools confidence` | Tool confidence per agent |

In the terminal UI, type `/help` for its slash commands.
