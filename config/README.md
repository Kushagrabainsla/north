# config

The `config` package holds north's configuration. It reads the system settings from the environment, keeps the user settings in `settings.json`, and keeps the user's secrets. It also holds the request authentication and the dependency wiring in `dependencies.py`. In `architecture/modules.yaml` most of the package is the module `platform.config`, in the lowest layer. `dependencies.py` is part of the protected module `composition.app`.

## Main parts

| File | Key names | What it does |
|---|---|---|
| `settings.py` | `Settings`, `settings`, `reload_settings`, `north_home_in`, `read_secret_file` | System settings from `NORTH_*` environment variables and the `.env` in north's home. `settings` is the one shared instance. |
| `strategy.py` | `NorthSettings`, `StrategyMode`, `WorkKind`, `ModelChoice` | User settings in `settings.json`: the Models panel rows, the power (eco, cruise, sport), the approval mode, the time zone, browser profiles and setup progress. |
| `approval_mode.py` | `ApprovalMode`, `parse_approval_mode`, `require_approval_mode`, `resolve_approval_mode`, `mode_options` | Names the four approval modes (ask, safe, autonomous, yolo) and their text. It decides nothing. |
| `secret_store.py` | `SecretStore`, `store`, `secret`, `released_to`, `SecretUnavailable` | The user's keys and passwords in `secrets.json`. Gives a value only to a tool that declared it, and hides stored values in text. |
| `security.py` | `verify_api_access`, `verify_secret`, `load_secret`, `effective_secret`, `server_proof`, `issue_web_session`, `issue_pairing`, `keep_home_private` | The shared secret in `secret.key`, the CLI secret check, browser sessions and pairing tokens, and the FastAPI auth dependency. |
| `env_file.py` | `write_env_key`, `remove_env_key` | Writes and removes keys in north's `.env` with owner-only permissions. |
| `run_file.py` | `write_run_file`, `read_run_file`, `remove_run_file` | Records the address of the running server in `run/server.json`, so that the CLI can find it. |
| `server_url.py` | `server_url`, `listening_url`, `is_this_computer` | The URL of this server, from the setting or from the port that the process listens on. |
| `browser_profiles.py` | `BrowserProfile`, `SetupProgress`, `discover_browser_profiles` | Browser profile models. Finds the local Chrome, Chromium and Brave profiles by name and folder only. |
| `browser_connection.py` | `cdp_http_base`, `active_cdp_endpoint` | Checks that a Chrome DevTools endpoint is on this computer only. |
| `runtime.py` | `set_runtime`, `get_runtime` | Holds the live `Dependencies` object after startup. |
| `dependencies.py` | `Dependencies`, `build_production_dependencies` | Builds the stores, the ledger, the inference router, the cost tracker and the memory gateway once. |

`__init__.py` is empty. Callers import from each file.

## How north uses it

- Almost every package imports `settings` from `config.settings`.
- `orchestrator/app.py` calls `build_production_dependencies`, `set_runtime`, `load_secret`, `keep_home_private` and `write_run_file` at startup. The objects that need async startup are built in `orchestrator/app.py`, not in `dependencies.py`.
- `orchestrator/api/deps.py` applies `verify_api_access` to the API routes. `orchestrator/api/health.py` answers with `server_proof`.
- `cli/_client.py` reads the run file and checks `server_proof` before it sends the secret.
- `tools/base.py` calls `released_to` and `hide_in` around every tool call. `web/secrets_panel.py` saves and removes secrets.
- `web/api.py` writes provider keys with `write_env_key` and calls `reload_settings`.
- `tools/specialized/north_config.py` and `web/api.py` read the live container with `get_runtime`.
- `NorthSettings` is built once and given to the inference router and the orchestrator.

## Data on disk

All files are in north's home (`~/.north` by default, or `NORTH_HOME`).

- `.env`: system settings and provider keys. `settings.json`: user settings.
- `secret.key`: the shared secret, mode 0600. `secrets.json`: the user's tool secrets, mode 0600.
- `run/server.json`: the address and process id of the running server.

## Rules

- Read configuration through `settings`. Do not read `os.environ` directly (CODING_STYLE §17.5).
- North reads only the `.env` in north's home. A `.env` in the current folder can come from a cloned repository, so `Settings` does not load it.
- System settings come from the environment. User settings that change at runtime live in `NorthSettings` (CODING_STYLE §17.2).
- Wire concrete classes only in `dependencies.py` (CODING_STYLE §6.3). It is a protected path: do not change it without the user's approval (`docs/MODULES.md`).
- `approval_mode.py` only names the modes. `approval/policy.py` decides (CODING_STYLE §7.3). Outside `approval/` and `config/`, only the modules in `MODE_READERS` in `tests/unit/architecture/test_approval_layer.py` may name the mode.
- The model knows a secret by its name only. A tool declares `uses_secrets` and reads the value with `secret(name)` inside `run()` (CODING_STYLE §7.6).
- Every API route uses `verify_api_access` (CODING_STYLE §12.3). North serves this computer only (CODING_STYLE §12.6).
- `platform.config` may import no other layer (`architecture/modules.yaml`). Only `dependencies.py`, in the `composition` layer, imports the other packages.
- The tests are in `tests/unit/config/` and `tests/unit/utils/test_security_*.py`.
