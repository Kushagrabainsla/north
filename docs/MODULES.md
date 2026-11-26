# North Module Catalog

The machine-readable source of truth is [`architecture/modules.yaml`](../architecture/modules.yaml). This guide explains its current-state module contracts while paths remain at the repository root.

## How to make a change

1. Find the module owning the path.
2. Confirm the task scope allows that module and path.
3. Read its public contracts and required validation.
4. For a cross-module change, declare every affected module and run the union of validation gates.
5. Do not edit **protected** modules without explicit user approval.

Architecture, safety-policy, approval, tool-mutation, and composition-root changes are protected because they can widen an agent's authority or bypass safety controls.

## Current catalog

| Module | Current paths | Layer | Key contracts | Protection |
|---|---|---|---|---|
| `platform.config` | `config/` | Platform | `NORTH_*`, settings | Standard |
| `platform.ledger` | `ledger/` | Platform | audit/event persistence | Standard |
| `platform.common` | `utils/`, `exceptions.py` | Platform | dependency-light utilities | Standard |
| `platform.runtime_resources` | `resources/` | Platform | bundled prompts and built-in skills | Standard |
| `intelligence.inference` | `inference/` | Intelligence | inference/providers/routing | Standard |
| `intelligence.memory` | `memory/` | Intelligence | user context and facts | Standard |
| `intelligence.workspace_context` | `context/` | Intelligence | repo instructions, code intelligence | Standard |
| `intelligence.skills` | `skills/` | Intelligence | skill discovery and format | Standard |
| `intelligence.coding_agents` | `coding_agents/` | Intelligence | delegated coding runs on installed agents | Standard |
| `application.orchestration` | `orchestrator/` | Application | task lifecycle and plans | Standard |
| `application.agents` | `agents/` | Application | agents and delegation | Standard |
| `application.approval` | `approval/` | Application | consent and safe actions | Protected |
| `application.jobs` | `jobs/`, `bootstrap/` | Application | schedules and onboarding | Standard |
| `integrations.tools` | `tools/` | Integration | tool/mutation enforcement | Protected |
| `integrations.gateways` | `gateways/`, `mcp_client/` | Integration | Telegram and MCP | Standard |
| `interfaces.cli` | `cli/` | Interface | `north` CLI | Standard |
| `interfaces.web` | `web/` | Interface | cockpit API and browser UI | Standard |
| `composition.app` | `orchestrator/app.py`, `config/dependencies.py` | Composition | lifecycle and concrete wiring | Protected |
| `architecture` | `architecture/`, ADRs/refactor docs | Composition | boundaries and decisions | Protected |
| `safety_policy` | `resources/policies/`, `SECURITY.md` | Composition | binding safety rules | Protected |

## Package READMEs

Each top-level package has a README. It tells what the package is for, its main files, how north uses it, and its rules.

| Package | What it holds |
|---|---|
| [`agents/`](../agents/README.md) | The agent base classes, the agent loop and north's one agent |
| [`approval/`](../approval/README.md) | Approval cards, approval modes and the one place that decides |
| [`architecture/`](../architecture/README.md) | The module manifest and the import checks |
| [`bootstrap/`](../bootstrap/README.md) | Onboarding and first setup |
| [`cli/`](../cli/README.md) | The `north` command and the terminal interface |
| [`coding_agents/`](../coding_agents/README.md) | Coding runs on the installed coding agents |
| [`config/`](../config/README.md) | Settings, dependency wiring and secrets |
| [`context/`](../context/README.md) | Workspace context, such as repository instructions |
| [`evals/`](../evals/README.md) | The coding scoreboard |
| [`flows/`](../flows/README.md) | Flows: multi-step work that can run on a schedule |
| [`gateways/`](../gateways/README.md) | Telegram and other channels |
| [`inference/`](../inference/README.md) | Model providers, routing and cost tracking |
| [`jobs/`](../jobs/README.md) | The job queue and schedules |
| [`ledger/`](../ledger/README.md) | The append-only record of what happened |
| [`mcp_client/`](../mcp_client/README.md) | north's MCP client |
| [`memory/`](../memory/README.md) | Facts, episodes and the memory gateway |
| [`orchestrator/`](../orchestrator/README.md) | The server, the task lifecycle and the event stream |
| [`policies/`](../policies/README.md) | The self-edit rule and the prompt policies |
| [`skills/`](../skills/README.md) | Skill discovery, selection and learned skills |
| [`tools/`](../tools/README.md) | Tools, the tool registry and the tools north writes |
| [`utils/`](../utils/README.md) | Shared helpers |
| [`views/`](../views/README.md) | Custom panels that north makes |
| [`web/`](../web/README.md) | The dashboard API and the browser UI |

## Dependency direction

Current imports are transitional. The intended direction is:

```text
interfaces ─────┐
integrations ───┼──> application ──> intelligence / platform
app ────────────┘          │
                            └──> integrations through injected ports only
```

Temporary exceptions are explicit in `architecture/modules.yaml`, each with a removal stage. New exceptions require an ADR and tests; no permanent exception is created for convenience.

## Agent permissions

`editable_by` identifies roles eligible to implement an approved change, not blanket authority. In Stage 2, the task's declared module/path scope will be enforced by source-mutating tools. An empty `editable_by` list means user approval is required before any agent can modify that module.

Architect, researcher, and reviewer agents continue to be read-only by tool grant. Coder tasks must still remain inside their assigned scope.
