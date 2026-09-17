# ADR 0004: Keep top-level application packages instead of adopting `src/north/`

- Status: Accepted
- Date: 2026-09-09
- Amended: 2026-09-16 (personal agents moved to `NORTH_HOME/agents/`)
- Supersedes the layout migration proposed as Stage 6 of
  [the clean-architecture plan](../refactor/CLEAN_ARCHITECTURE_PLAN.md)

## Context

Stage 6 of the approved refactor proposed relocating every application package
into `src/north/`, in dependency order, "only after prior stages are stable".
Stages 0-5 are now complete: module ownership, import direction, edit scopes,
composition roots, and cohesive module splits are all enforced by tests.

Measured migration surface at the time of this decision:

- 17 top-level Python packages.
- ~420 files import those packages directly.
- `orchestrator/app.py` resolves agents as `Path(__file__).parent.parent / "agents"`,
  a sibling directory *inside the installed package tree*.
- `tools/universal/create_agent.py` scaffolds new agents into
  `Path(__file__).parent.parent.parent / "agents"` - the same tree.
- `AgentRegistry` and `ToolRegistry` import discovered extensions by constructed
  dotted module names.
- The console script is `north = "cli.main:app"`; the container runs
  `uvicorn orchestrator.app:app`.

## Decision

Keep the application packages at the repository root. Do not introduce
`src/north/`.

Enforce boundaries logically - through `architecture/modules.yaml`, the
ownership contract test, and the AST import-direction gate - rather than
physically through directory nesting.

## Rationale

north is an application, not a library. Its public contract is the `north`
command, the HTTP API, the on-disk data in `~/.north`, and the agent/tool
extension directories. None of those is an importable Python path, so the usual
`src/` layout benefit - protecting a distributed library's import surface - does
not apply here.

At the time of this decision, relocating the packages also moved the directory
that `north agent create` wrote into and that agent discovery scanned. That path
was user-visible in a source checkout or editable install. The later packaging
refactor removed this concern by storing personal agents under
`NORTH_HOME/agents/`, outside the replaceable package tree.

The migration would also rewrite ~420 files, the module manifest's path
patterns, the import baseline's module names, the console-script entry point, the
container command, and CI - a large mechanical diff whose only gain is directory
nesting. The cost/benefit does not justify it, and Stage 6's own exit criteria
require installation and persistent data to stay compatible.

## Consequences

- The layered structure stays declared in `architecture/modules.yaml` and proven
  by `tests/unit/architecture/`, which is where it is actually enforceable.
- Packaging behavior that a migration would have had to prove is instead pinned
  by a permanent gate: `tests/unit/architecture/test_packaging.py` asserts the
  console-script name and target, that every owned top-level package is
  discoverable, and that vendored frontend dependencies stay out of the sdist.
- Agent module resolution no longer hardcodes a top-level prefix
  (`agents/packaging.py`), so this decision is reversible without touching
  discovery logic.
- Personal agents are now stored under `NORTH_HOME/agents/` and discovered after
  packaged agents, so a git-install update no longer deletes them and they cannot
  shadow a built-in agent with the same name.
