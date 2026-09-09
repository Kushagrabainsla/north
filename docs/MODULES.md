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
| `intelligence.inference` | `inference/` | Intelligence | inference/providers/routing | Standard |
| `intelligence.memory` | `memory/` | Intelligence | user context and facts | Standard |
| `intelligence.workspace_context` | `context/` | Intelligence | repo instructions, code intelligence | Standard |
| `intelligence.skills` | `skills/` | Intelligence | skill discovery and format | Standard |
| `application.orchestration` | `orchestrator/` | Application | task lifecycle and plans | Standard |
| `application.agents` | `agents/` | Application | agents and delegation | Standard |
| `application.approval` | `approval/` | Application | consent and safe actions | Protected |
| `application.jobs` | `jobs/`, `bootstrap/` | Application | schedules and onboarding | Standard |
| `integrations.tools` | `tools/` | Integration | tool/mutation enforcement | Protected |
| `integrations.gateways` | `gateways/`, `mcp/` | Integration | Telegram and MCP | Standard |
| `interfaces.cli` | `cli/` | Interface | `north` CLI | Standard |
| `interfaces.web` | `web/` | Interface | cockpit API and browser UI | Standard |
| `composition.app` | `orchestrator/app.py`, `config/dependencies.py` | Composition | lifecycle and concrete wiring | Protected |
| `architecture` | `architecture/`, ADRs/refactor docs | Composition | boundaries and decisions | Protected |
| `safety_policy` | safety/clean-code policy and security docs | Composition | binding safety rules | Protected |

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
