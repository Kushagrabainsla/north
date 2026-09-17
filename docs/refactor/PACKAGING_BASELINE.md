# Packaging Baseline

This document records the Phase 1 baseline for separating North's runtime
resources from development-only repository content. It describes the artifact
produced by the current `pyproject.toml`; it does not change the packaging
behavior.

## Build

Baseline collected on 2026-09-16 from commit `ee93927` on `main`.

Commands:

```bash
UV_CACHE_DIR=/private/tmp/north-phase1-uv-cache \
  uv build --out-dir /private/tmp/north-phase1-artifacts.HVGNZz
```

Artifacts:

| Artifact | Size |
|---|---:|
| `north-1.20.0-py3-none-any.whl` | 1,245,726 bytes |
| `north-1.20.0.tar.gz` | 1,076,913 bytes |

The build succeeded. The repository working tree had one pre-existing
untracked directory, `.kiro/`; the build did not create tracked changes.

## Current packaging behavior

Package discovery is broad:

- setuptools searches from the repository root
- namespace discovery remains enabled
- package data uses the wildcard rule `"*" = ["*.yaml", "*.yml", "*.md", "*.html", "*.css", "*.js", "*.sh"]`
- `web/src` and `web/node_modules` are explicitly excluded from the `web`
  package data
- `tests*` and build/frontend dependency paths are excluded from package
  discovery

The wildcard package-data rule and namespace discovery are the main reasons
development-oriented packages and documentation enter the wheel.

## Wheel contents

The current wheel contains the following selected categories:

| Category | Files |
|---|---:|
| Runtime Python and package files | 278 |
| `evals/` | 50 |
| `agents/` | 49 |
| `skills/` | 46 |
| `prompts/` | 19 |
| `docs/` | 17 |
| `experiments/` | 9 |
| `web/` | 8 |
| `policies/` | 3 |
| `scripts/` | 3 |
| Metadata | 6 |

The wheel currently includes:

- all evaluation fixtures and grading code under `evals/`
- experiment benchmark code under `experiments/`
- repository documentation under `docs/`
- repository helper scripts under `scripts/`
- built-in agent implementation, configuration, and prompts under `agents/`
- built-in skills under `skills/builtin/`
- root prompt files under `prompts/`
- compiled frontend assets under `web/dist/`

The wheel does not include `web/src/` or `web/node_modules/` under the current
frontend exclusion rules.

## Source distribution contents

The current sdist includes substantially more repository material:

| Category | Files |
|---|---:|
| Evaluation material | 97 |
| Skills and skill runtime | 85 |
| Agents and agent resources | 66 |
| Tools | 65 |
| Orchestrator | 57 |
| Inference | 52 |
| Documentation | 21 |
| Prompts | 20 |
| Web files | 14 |
| Experiments | 12 |
| Scripts | 4 |
| Policies | 4 |

The sdist also includes repository metadata such as `MANIFEST.in`,
`pyproject.toml`, `README.md`, `LICENSE`, and generated package metadata.

This is expected for a source distribution to some extent, but the current
manifest does not deliberately distinguish runtime source from development
source. In particular, it explicitly prunes only `web/node_modules`.

## Runtime resource candidates

The following files are candidates for an explicit runtime-resource allowlist:

| Current location | Runtime purpose | Proposed treatment |
|---|---|---|
| `prompts/*.md` | Built-in system and pipeline prompts | Move under packaged runtime resources |
| `skills/builtin/**` | Built-in procedural skills | Move under packaged runtime resources |
| `agents/*/config.yaml` | Built-in agent declarations | Move with built-in agent resources or retain as explicitly packaged data |
| `agents/*/prompts/**` | Built-in agent prompts | Move under packaged runtime resources |
| `policies/*.md` | Binding runtime policy content | Explicitly package as runtime resources |
| `web/dist/**` | Compiled cockpit assets | Explicitly package as runtime assets |
| `cli/docker-compose.yml` | Bundled Docker startup template | Explicitly package as CLI data |
| selected `*.yaml` files | Runtime catalogs/configuration | Audit individually; do not retain a global wildcard |

Python files in `agents/`, `skills/`, `tools/`, and the other application
packages remain executable runtime code for this phase. Moving their Python
namespaces is out of scope.

## Development-only candidates

The following should remain in the repository but should not be included in a
production wheel:

- `tests/`
- `experiments/`
- `evals/`
- `docs/`
- frontend source under `web/src/`
- frontend dependency directories and build metadata
- repository caches and build directories

The sdist may retain source-development files needed for contributors, but its
contents should still be controlled explicitly. Production installation must
be validated against the wheel, not inferred from the sdist.

## Path-sensitive runtime code

The following current paths must be handled during the resource migration:

- `utils/prompts.py` resolves relative prompts from the repository root.
- `orchestrator/app.py` resolves agents, built-in skills, and `web/dist` from
  sibling repository directories.
- `agents/registry.py` discovers filesystem agent directories and constructs
  import paths dynamically.
- `skills/registry.py` discovers skills from filesystem directories.
- `agents/llm_agent.py` resolves policies relative to the repository tree.
- CLI agent creation and planner updates assume repository-level `agents/`
  and `prompts/` directories.

These are the primary seams for Phase 2. No broad import rewrite is required
to introduce a centralized resource resolver.

## Packaging decisions for the next phase

Phase 2 should:

1. replace wildcard package-data inclusion with explicit runtime-data rules;
2. introduce one resolver for packaged resources and checkout resources;
3. keep current Python import paths unchanged;
4. preserve development checkout behavior through a deliberate fallback;
5. add wheel-content tests that reject `evals`, `experiments`, and `docs`;
6. verify installed-wheel startup and resource loading in a clean environment.

Installing directly from Git will still download the repository before the
build. Removing development files from the download itself requires publishing
and installing a wheel or maintaining a separate runtime distribution.

## Final v2 packaging contract

The v2 packaging work keeps the repository layout compatible while separating
runtime content from development content at the distribution boundary:

- `resources/` contains immutable bundled prompts, policies, and built-in skills.
- Built-in agent implementations remain importable Python modules under
  `agents/`; their runtime YAML and system prompts are explicitly packaged.
- User-created agents live under `NORTH_HOME/agents/` and learned skills under
  `NORTH_HOME/skills/`. These directories are never included in a wheel or
  source distribution.
- `web/dist/` is the only frontend content shipped for runtime use.
- `docs/`, `evals/`, `experiments/`, `scripts/`, `tests/`, `architecture/`,
  `web/src/`, Vite build metadata, and agent README files remain repository/CI
  content only.

Packaging regression tests pin these rules. Release validation builds both
artifacts, inspects their member lists, installs the wheel without dependencies,
and imports the CLI, server, and runtime resource resolvers.
