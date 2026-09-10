# Compatibility seams

Stage 8 of [the refactor plan](CLEAN_ARCHITECTURE_PLAN.md) requires every
compatibility shim to be tracked with an owner and a removal condition. This is
that register.

## Status

**No temporary shims are outstanding.** `architecture/modules.yaml` declares no
`temporary_exemptions`, and every forbidden-import pair removed during the
refactor was deleted from `architecture/import-baseline.txt` rather than
exempted.

## Intentional re-exports (not scheduled for removal)

Stage 5 moved cohesive logic out of oversized modules while leaving the original
name importable from its old home. These are one-line re-exports with no second
implementation, so they cannot drift; they exist so unrelated call sites and
tests did not have to change in a mechanical-move commit.

| Old name | Now owned by | Why it stays |
|---|---|---|
| `orchestrator.orchestrator._read_artifact` (re-export) and `Orchestrator._primary_artifact_path` (delegating method) | `orchestrator.handoff_artifacts` | Internal call sites inside a 2,200-line module |
| `orchestrator.orchestrator.AgentFailure`, `_is_model_scarcity`, `_MODEL_SCARCITY_MESSAGE` | `orchestrator.model_scarcity` | Referenced across the pipeline |
| `agents.agentic_llm_agent._extract_success`, `_failure_kind`, `_is_unanswered_approval`, `_is_delegation_failure`, `_failed_json` | `agents.tool_results` | Used throughout the react loop |
| `memory.facts._normalize_for_dedup` | `memory.dedup` | Used by the fact store's duplicate check |
| `cli.main._parse_hotkey`, `_wav_bytes` | `cli.dictation` | Used by the push-to-talk command |
| `cli.main._load_env_keys`, `_update_env_file`, `_save_provider_key`, `_parse_provider_selection`, `_provider_is_configured`, `_any_provider_configured` | `cli.provider_env` | Used by first-run provider setup |
| `cli.main._day_selection` | `cli.scheduling` | Wraps the pure normalizer with Typer error output |
| `cli.main._web_build_is_stale` | `cli.web_build` | Used by `north web`; also imported by its existing test |
| `cli.main._last_error_lines` | `cli.startup_report` | Used by startup failure reporting |
| `cli.main._pinned_git_spec` | `cli.update_spec` | Used by `north update` |
| `cli.tui._describe_turn`, `_estimated_tokens`, `_slash_argument`, `_requested_context_document` | `cli.tui_text` | Used throughout the app class |
| `tools._path` re-exports of handoff and pruning helpers | `utils.handoff`, `utils.filesystem` | Plugin-facing: tools import from `tools._path` |
| `tools.universal._schedules.parse_weekdays` | `utils.weekdays` | Keeps the CLI and API validating against one parser |

Removal condition for any row: when the last caller of the old name is updated,
delete the re-export in that same change. None is load-bearing for behavior, so
none blocks a release.

## Deliberate divergence, not duplication

`memory.facts` recognizes secrets with `SECRET_RE | CC_RE`, while
`utils.secrets.contains_secret` also checks `TOKEN_RE`. These look like
duplication but are not interchangeable: unifying them would make the fact store
start rejecting content it currently stores. Any change here is a behavior
change and needs its own decision and tests.
