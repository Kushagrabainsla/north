# utils

The `utils` package holds the small helpers that more than one north package uses. It is in the
`platform` layer (`platform.common` in `architecture/modules.yaml`), at the bottom of the import
order. It holds no business logic of its own. It also holds the `Protocol` ports that let higher
layers type against tools, plans and sessions without an import of those packages.

## Main parts

| File | Key names | What it does |
|---|---|---|
| `db.py` | `open_db_connection`, `close_all_pools` | The one SQLite helper. Keeps one connection per thread and file, with WAL, `synchronous=NORMAL`, `busy_timeout`, foreign keys and `sqlite3.Row`. Loads `sqlite_vec` if it is installed. A nested call uses a `SAVEPOINT`. |
| `time.py`, `weekdays.py` | `utcnow`, `now_epoch`, `to_epoch`, `epoch_to_local`, `format_local`, `parse_local`, `configure_timezone`, `awake_seconds`; `parse_weekdays`, `normalise_weekdays` | Epoch storage and display in north's configured zone. Reads a weekday selection (number, name, group word or list) into one form. |
| `ids.py` | `generate_id`, `generate_task_id` | Random hex ids, and task ids with a `task_` prefix. |
| `tasks.py` | `spawn`, `drain` | Starts a background coroutine, keeps a strong reference to it and logs its exception. |
| `logging.py` | `configure_structured_logging`, `bind_task_id` | JSON log lines that carry the active `task_id`. Takes secrets out of each line. |
| `execution_context.py`, `spans.py` | `ExecutionIdentity`, `bind_execution`, `current_execution`; `span`, `collecting`, `totals` | The run identity of the current agent, kept across async calls. Named, timed steps inside one tool call, for the `tool_timing` event. |
| `text.py`, `math.py`, `documents.py` | `extract_json`, `strip_code_fences`, `normalize_dashes`, `strip_html`, `extract_markdown_section`; `cosine_similarity`; `file_text`, `pdf_text`, `docx_text` | Text from model output, HTML and documents (PDF, Word). Vector similarity. |
| `net.py` | `fetch_url_text`, `validate_public_url`, `is_public_ip`, `UnsafeUrlError`, `HostNotFoundError` | URL fetch with SSRF protection. Checks the resolved IPs before the request and after each redirect. Limits size and time. |
| `secrets.py` | `contains_secret`, `redact`, `redact_for_logging`, `is_secret_field_name`, `blank_query_values` | Finds credentials and card numbers in free text and removes them. |
| `prompts.py`, `runtime_resources.py`, `version.py` | `load_prompt`; `resource_path`, `prompts_dir`, `builtin_skills_dir`, `policies_dir`, `web_dist_dir`; `NORTH_VERSION`, `installed_commit` | Finds bundled files under `resources`. Reads the north version from `pyproject.toml` or the package metadata. |
| `tools.py`, `plan.py`, `sessions.py`, `edit_scope.py` | `ToolRegistryPort`, `ToolRunnerPort`, `ToolDispatchRegistryPort`, `ToolInput`, `ToolOutcome`, `ToolNotFoundError`, `named_tools`, `tool_mutates`; `PlanStorePort`; `ActiveSessionStorePort`; `EditAuthorizer`, `narrow_workspace` | `Protocol` ports. The orchestrator and agents use them instead of the concrete classes in `tools`, `architecture` and the stores. |
| `retired_tools.py`, `step_approval.py` | `current_tool`; `parse_step_approval`, `approval_fingerprint_value` | Shared words for skills and flows, which cannot import each other. A retired tool name maps to the tool that does its work now. |
| `handoff.py`, `filesystem.py`, `workspace_lock.py`, `repository.py`, `processes.py` | `handoff_dir_for`, `ensure_handoff_dir`, `prune_handoff_dirs`; `PRUNED_DIRS`; `workspace_lock`; `repository_identity`; `stop_process_group` | Files and processes: per-task handoff folders under `<NORTH_HOME>/tasks`, folders not to walk, one lock per workspace, the Git commit and dirty state, and stopping a process group. |
| `clone.py`, `power.py` (+ `_mac_power.py`), `reveal.py`, `desktop.py` | `clone_tree`; `fully_awake`; `reveal`; `frontmost_app`, `activate` | Code that differs by OS, behind one call each: APFS folder clone, real wake against a short sleep wake, show a file in the file manager, and give the front back to the user's app. |
| `vector_space.py` | `ensure_vector_space`, `ensure_vector_space_reembed` | Stamps each vector store with its embedding model. A model change clears the store or its vectors. |
| `update_record.py` | `record_update`, `last_update` | Writes and reads `update.json`, the state of the last `north update`. |

## How north uses it

- Almost every package imports `utils`. The most used files are `time.py` (about 39 files),
  `db.py` (26), `ids.py` and `text.py` (18 each), `prompts.py` (14) and `tasks.py` (10).
- Every SQLite store (`ledger/`, `jobs/`, `memory/`, `approval/`, `inference/`) opens its file
  with `open_db_connection`.
- `orchestrator/app.py` calls `configure_structured_logging` at startup.
- `config/dependencies.py` calls `configure_timezone` with the zone from the Settings page.
- `memory/` and `tools/tool_index.py` call `ensure_vector_space` and `cosine_similarity`.
- `tools/universal/fetch_url.py`, `tools/universal/browser.py`, `memory/injection.py` and
  `skills/checks.py` fetch URLs through `utils/net.py`.
- `tools/base.py` and `orchestrator/coding_landing.py` take `workspace_lock`.
- Public interface: `__all__` in `utils/__init__.py` names `open_db_connection`,
  `generate_id`, `generate_task_id`, `load_prompt`, `utcnow` and `format_timestamp`. No caller
  uses it. Callers import each file directly, for example `from utils.time import now_epoch`.

## Rules

- If logic is used by more than one package, put it here once. Do not copy it into a package
  (CODING_STYLE §5.1, §5.2).
- Do not import other north packages from `utils`. It is in the `platform` layer, and today it
  imports none. For this reason, `handoff.py` reads `NORTH_HOME` from the environment.
- Open every SQLite file with `open_db_connection` (CODING_STYLE §11.1).
- Store each instant as a Unix epoch. Show it in the zone from the Settings page with the
  helpers in `time.py`, never with a bare `.astimezone()` (CODING_STYLE §5.2.1).
- Load each prompt from a file with `load_prompt` (CODING_STYLE §5.3).
- Start a background coroutine that nobody awaits with `spawn`, not with
  `asyncio.create_task` (CODING_STYLE §14.1).
- Fetch a URL that a model gave with `fetch_url_text`. It refuses loopback, private and
  link-local addresses.
- Put OS-specific code behind one interface that selects its implementation. Callers do not
  check `sys.platform` (CODING_STYLE §16.4). To add Linux to `clone.py` or `power.py`, write a
  class and add it to `_IMPLEMENTATIONS`.
- Show a file with `reveal`. North never opens a file (CODING_STYLE §5.1).
- Keep a removed tool name in `retired_tools.py`, so that a saved skill still runs
  (CODING_STYLE §16.3).
- Use the standard `logging` module only. Do not add a logging framework (CODING_STYLE §22).
- Read the version from `NORTH_VERSION`. Do not write the version anywhere else.
- Add tests in `tests/unit/utils/` with the change (CODING_STYLE §18.8).
