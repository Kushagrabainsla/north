# agents

The `agents` package is the agent layer of north. North has one agent, `NorthAgent` in `agents/north/`, and every task, flow step and schedule runs on it (CODING_STYLE §15). The package holds the `Agent` base class and the ReAct loop, which calls tools through native function calling. It also loads the agent folder and the built-in policies. A specialism is a skill on north, not a new agent. North hands coding to the installed coding agents through `coding_agents/`.

## Main parts

| File | Key names | What it does |
|---|---|---|
| `base.py` | `Agent` (ABC) | Template method `run()`. It continues a stopped run, selects the skills once, loads context and tools, calls `_execute()` and records the run in `agent_run_store`. A subclass implements `_execute()` only. |
| `llm_agent.py` | `LLMAgent` | Reads `prompts/system.md` from the agent folder at construction. Adds the built-in policies and the runtime context to the system prompt. Parses a JSON answer. |
| `agentic_llm_agent.py` | `AgenticLLMAgent` | The ReAct loop. Each step sends a heartbeat, reads new user messages, compacts the history, calls the model with the tool schemas, runs the tool calls and saves a checkpoint. Serves the built-in tools `ask_user`, `request_approval`, `request_secret` and `find_tools` through the approval layer. |
| `north/` | `NorthAgent`, `config.yaml`, `prompts/system.md` | The one agent. `NorthAgent` is an empty subclass of `AgenticLLMAgent`. `config.yaml` sets `domain: general`, the tag of the skills north gets. |
| `registry.py` | `AgentRegistry` | Loads each folder in `agents/` that has `config.yaml` and `agent.py`. Gives `north`, `get()`, `all()` and `names()`. |
| `models.py` | `AgentPayload`, `AgentResult`, `AgentConfig`, `AgentDependencies`, `StreamEmitter` | The input of a run, its output, the folder config, and the bundle of shared services each agent gets. |
| `policy.py` | `Policy`, `load_policies`, `render_policies`, `PolicyError` | Loads the policy files in `resources/policies/` (today only `safety.md`). Fails closed on a missing, empty or malformed file. |
| `schemas.py` | `FIND_TOOLS_SCHEMA`, `ASK_USER_SCHEMA`, `REQUEST_SECRET_SCHEMA`, `REQUEST_APPROVAL_SCHEMA` | JSON schemas of the built-in loop tools. |
| `context_compaction.py` | `compact_if_needed`, `compact_history`, `context_window_for`, `estimate_messages_tokens` | Keeps the conversation inside the context window. Summarises old tool results with a model, or reduces them without a model. |
| `tool_output_reduction.py` | `salient_excerpt` | Cuts long tool output and keeps errors, `file:line` locations, declarations and diff anchors. |
| `reasoning.py` | `strip_reasoning`, `ReasoningStreamSplitter` | Removes `<thought>`, `<thinking>` and `<reasoning>` blocks. The stream and the stored answer use the same rules. |
| `inbox.py` | `TaskInbox` | Holds the messages the user sends a running task (CODING_STYLE §15.4). |
| `capabilities.py` | `build_platform_capabilities_summary` | A short list of the tool names and flows north has, for the prompt. |
| `tool_results.py` | `extract_success`, `failure_kind`, `failed_json` | Reads serialized tool results. Bad JSON counts as a failure, not an exception. |
| `constants.py`, `packaging.py`, `exceptions.py` | `NORTH_AGENT`, `MAX_TOOL_RESULT_CHARS`, `agent_module_root`, `AgentError` and subclasses | Small helpers. |

## How north uses it

- `orchestrator/app.py` builds `AgentDependencies` in `_build_agent_deps()` and the registry in `_build_agent_registry()`.
- `orchestrator/orchestrator.py` calls `agent.run(payload)` in `_run_agent_with_retry()`.
- `Orchestrator.send_message()` posts a mid-task message to the `TaskInbox`. The loop reads it before the next step and before it answers.
- `config/dependencies.py` makes the one `TaskInbox` (field `task_inbox`).
- `tools/universal/flow_runner.py` and `orchestrator/coding_run_recorder.py` build an `AgentPayload`.
- `orchestrator/result_audit.py` uses `Agent`, `AgentPayload` and `AgentResult`.
- `tools/universal/flow_schedules.py`, `tools/universal/create_skill.py`, `web/extensions.py` and `orchestrator/api/cron.py` use `NORTH_AGENT`.
- `cli/tui.py` calls `context_window_for()`.
- The public interface (`__all__`) is `Agent`, `LLMAgent`, `AgentRegistry`, `AgentConfig`, `AgentDependencies`, `AgentPayload`, `AgentResult` and the `AgentError` family.

## Rules

- Do not add an agent for a specialism. Write a skill on north instead (CODING_STYLE §15, §6.4).
- Do not override `Agent.run()`. Implement `_execute()` (CODING_STYLE §15.1).
- An agent returns an `AgentResult` (CODING_STYLE §15.3).
- Read context only through the memory gateway (`Agent._memory()`). It applies the gate on what the agent may read.
- Keep the start tool set (`_ESSENTIAL_TOOL_NAMES` in `base.py`) the same for every task. A different set on each task breaks the prompt cache. A flow step's `allowed_tools` replaces the set.
- Do not decide approvals in the agent. A tool call goes to `Tool.execute()`, and the approval layer decides (CODING_STYLE §7.3).
- Save a checkpoint after each step, so a paused or interrupted task continues where it stopped (CODING_STYLE §15.5).
- A run with no model available ends as `paused`, not `failed`, so the task continues later (CODING_STYLE §13.5).
- Put new prompt text in a markdown file and load it with `utils.prompts.load_prompt()` (CODING_STYLE §5.3). The agent prompt is `north/prompts/system.md`.
- Test an agent without the orchestrator, with a scripted model (CODING_STYLE §18.4).
