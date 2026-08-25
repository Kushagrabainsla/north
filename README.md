<!-- Logo is hosted off-repo so `uv tool install git+...` never downloads it.
     To set the URL: drag the image into any GitHub issue/PR comment box,
     copy the generated user-attachments URL, paste it below (don't submit the comment). -->
<p align="center">
  <img src="https://repository-images.githubusercontent.com/1221207908/a9516630-e5f6-475f-ab80-44b2dd6dc9c8" alt="north" width="480">
</p>

**A digital version of you that learns how you think and earns autonomy over time.** It starts by suggesting, moves to acting with your approval, and takes on more as it earns your trust.

---

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/Kushagrabainsla/north/main/scripts/install.sh | bash
```

The installer configures an inference provider. One provider is enough to start:

| Provider | Authentication | Setup |
|---|---|---|
| OpenRouter | `NORTH_OPENROUTER_API_KEY` | [openrouter.ai/keys](https://openrouter.ai/keys) |
| Groq | `NORTH_GROQ_API_KEY` | [console.groq.com/keys](https://console.groq.com/keys) |
| Gemini | `NORTH_GEMINI_API_KEY` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| OpenCode Zen | `NORTH_OPENCODE_ZEN_API_KEY` | [opencode.ai/auth](https://opencode.ai/auth) |
| OpenAI Codex (experimental) | Browser OAuth | `north auth login openai-codex` |

To add or change keys later, edit `~/.north/.env`:

```
NORTH_OPENROUTER_API_KEY=sk-or-...
NORTH_GROQ_API_KEY=gsk_...
NORTH_GEMINI_API_KEY=...
NORTH_OPENCODE_ZEN_API_KEY=...
```

North stores its own OpenAI Codex OAuth token under `~/.north/credentials/`
with private file permissions. Use `north auth status` to inspect provider
configuration and `north auth logout openai-codex` to remove the token.

---

## Commands

| Command | What it does |
|---|---|
| `north` | Open the TUI (starts server if needed) |
| `north start` | Start server + TUI |
| `north start --no-chat` | Start server only |
| `north start --docker` | Start server via Docker Compose |
| `north stop` | Stop the server |
| `north reset` | Wipe data, keep API key |
| `north auth login openai-codex` | Connect an OpenAI Codex subscription |
| `north auth status` | Show provider authentication status |
| `north auth logout openai-codex` | Remove North's Codex credentials |
| `north reset --all` | Wipe everything |
| `north update` | Update to latest version |
| | |
| `north task "..."` | Submit a task |
| `north task cancel <id>` | Cancel a task |
| `north tasks` | List active tasks |
| `north stream <id>` | Stream raw events for a task |
| `north dictate` | Push-to-talk voice input |
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
| `north limits` | Show per-provider/model rate-limit & cooldown status with precise reset times (reads on-disk state, works offline) |
| `north tools confidence` | Show tool confidence scores |
| `north config set <key> <value>` | Set a config value |
| `north metrics` | Show system performance metrics |

In Telegram: send `/limits` to get the same rate-limit status in chat.



---

<p align="center">
  <sub><strong>north</strong> · a digital version of you</sub><br>
  <sub>MIT · <a href="https://github.com/Kushagrabainsla/north">GitHub</a></sub>
</p>
