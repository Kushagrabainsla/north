# cli

This package is the `north` command (a Typer app) and the interactive terminal UI (a Textual app). Both are
clients of the local north server. They send HTTP requests to the Orchestrator API and keep no state of their own.
The CLI also starts, stops, resets and updates the server process.

## Main parts

| File | Key names | What it does |
|---|---|---|
| `main.py` | `app`, `_root`, `start`, `web`, `stop`, `reset`, `update`, `setup_interactive` | The Typer app. All commands are in this one file. With no subcommand, `_root` starts the server if necessary and opens the TUI. |
| `tui.py` | `NorthApp`, `run`, `ToolInspectorModal`, `PlanModal`, `ActionMenuModal`, `SteerModal` | The Textual chat UI: streamed answers, steps, approval and question cards, and slash commands. |
| `_client.py` | `_api`, `_headers`, `base_url`, `running_port`, `pairing_url`, `NotThisNorth`, `OtherNorth` | The HTTP client. It finds this home's server through its run file and sends the secret only after the server proves it holds it (`_check_genuine`, `GET /health/proof`). |
| `_server.py` | `_start_server_process`, `_port_in_use`, `_is_north_server`, `_find_compose_file`, `_resolve_workspace`, `CRASH_LOG_NAME` | Starts and finds the server process, finds the Docker Compose file, and picks the workspace (the git root of the current folder by default). |
| `_task_stream.py` | `TaskStream`, `TaskStepFeed`, `follow_task`, `iter_sse_events` | Follows `/orchestrator/stream/{task_id}` and turns the SSE events into steps and the streamed answer. |
| `formatting.py` | `wordmark`, `summarize_diff`, `_build_steps_table`, `_format_*` | Pure display helpers (Rich tables and text) for `main.py` and `tui.py`. |
| `constants.py` | `_SLASH_COMMANDS`, `_STEP_ICONS`, `_STEP_LABELS`, `YOLO_BADGE` | Data only: literals that the CLI and the TUI share. |
| `text.py` | `print_text`, `main` | `north text FILE`. It loads no north settings, so it can run inside the shell sandbox. |
| Small pure helpers | `provider_env.py`, `dictation.py`, `scheduling.py`, `startup_report.py`, `update_spec.py`, `web_build.py`, `tui_text.py` | First-run key setup in `.env`, push-to-talk audio, day lists for `cron`, the error from a failed start, the `uv` install spec, the check for a stale web build, and TUI text helpers. |
| `docker-compose.yml` | | The image-based Compose file for `north start --docker`. It publishes the port on `127.0.0.1` only. |

## Commands

| Group | Commands |
|---|---|
| Server | `start`, `web`, `stop`, `reset`, `update`, `setup`, `status` |
| Tasks | `task`, `tasks`, `stream`, `cancel`, `jobs`, `job cancel` |
| Schedules | `cron` (`list`, `add`, `set`, `rm`) |
| Memory and audit | `context` (`show`, `edit`, `add`), `ledger` (and `ledger search`) |
| Models | `auth` (`login`, `status`, `logout`), `inference` (`costs`, `models`, `speed`, `routing`), `models`, `speed`, `routing`, `limits`, `metrics` |
| Other | `tools confidence`, `config` (`set`, `get`, `list`), `text`, `dictate` |

The root option `--yolo` sets the approval mode of north to yolo through `POST /orchestrator/settings`. The mode
belongs to north, so it applies to every surface. Run `north --help` or `north <command> --help` for the options.

## How north uses it

- `pyproject.toml` sets the entry point: `north = "cli.main:app"`.
- `north web` calls `_ensure_web_build`. If `web/dist` is older than its sources in a source checkout, it runs
  `npm run build` in `web/`. Then it asks the server for a one-time sign-in link (`POST /web/api/pair`) and opens it.
- `web/updates.py` runs `python -m cli.main update --yes` as its own process for the Update button on the System page.
- `tools/specialized/bash.py` writes a small `north` launcher that runs only `cli.text.main`, so a shell command
  in the sandbox can read a PDF or a Word file.
- `__init__.py` has no `__all__`. Outside `cli`, only the callers above use the package.

## Rules

- Get all state from the server API. Do not read north's stores or keep a copy of a list (such as the approval
  modes) in the CLI or the TUI (CODING_STYLE §1, "Every Surface Shows the Same State").
- Listen on this computer only. `start` and `web` refuse any `--host` that is not `127.0.0.1`, `::1` or
  `localhost` (`_only_this_computer`, CODING_STYLE §12.6).
- Send the secret only to a server that proved it holds it. Use `_api` or `_headers`, which check first.
- Show times in the zone on the Settings page. `_root` reads it from `settings.json`, also when the server is off
  (CODING_STYLE §5.2.1).
- A message sent while a task runs goes into that task. In the TUI, `/steer` does this (CODING_STYLE §15.4).
- Keep `text.py` free of imports that load north's settings. The sandbox cannot read north's home.
- Put shared literals in `constants.py` and display code in `formatting.py`, not in `main.py` or `tui.py`
  (CODING_STYLE §4.1, §9.6).
- If you change a command or an option, check its row in the root `README.md` against `--help` and the code
  (CODING_STYLE §5.1, "The README covers north, install and commands only").
