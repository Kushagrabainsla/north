# North experiments

This directory stores reproducible evidence behind architecture decisions.
Each experiment owns its inputs, runnable harness, recorded results, limitations,
and the production decision those results informed. Product code may reuse the
same primitives, but it must not import recorded results.

## Experiments

- [`browser_profiles_probe.py`](browser_profiles_probe.py) — disposable real Chrome
  sessions: separate synthetic logins, open-session reuse versus close/reopen,
  actual profile-directory checks, and North's native connection/wrong-profile gate.
  No personal browser, credentials, or North settings are used. Temporary profiles
  are removed after the probe. Re-run with `.venv/bin/python -m experiments.browser_profiles_probe`.
  On 2026-10-10, all three open-session reuse rounds preserved both identities;
  close/reopen varied across runs: two of four checks in one run, all four in
  another (vendor and graceful close). It is not reliable across trials.
  Therefore North keeps sessions open between tasks, never purges ordinary closes,
  and requires fresh login evidence after reconnecting. These results are not a
  guarantee for every site, browser, or cookie policy.
- [`browser_setup_ui_probe.py`](browser_setup_ui_probe.py) — click through the
  built dashboard using synthetic APIs and a disposable browser: create a
  purpose-labelled profile, test it, reload/resume setup, and disconnect.
  It also checks Settings panel order, full-width profile placement, styled
  controls, and overflow at 1440, 820 and 390 pixels. Add
  `--screenshots /tmp/north-ui-review` to save synthetic UI captures; emulation
  stays attached during each check so the measured viewport is the requested size.
  Backend approval and persistence are checked separately by unit/integration
  tests; this probe does not claim a real provider or personal login works.

- [`lean_trials/`](lean_trials/) — pre-implementation comparisons for scoped
  approvals, effect recovery, checklist evidence, sandbox boundaries, quota
  scheduling, delivery, capability fingerprints, and loopback validation.
- [`approval_integrity/`](approval_integrity/) — approval replay, server-granted
  file-write scope, and the Telegram allowlist, before and after the fix.
- [`tool_selection/`](tool_selection/) — capability-catalog size and runtime
  tool-retrieval strategy.
- [`system_quality/`](system_quality/) — quick-path routing, task-specific
  evidence gates, and tool-output signal retention across prompt/repository
  shapes.
- [`coding_agents_integration/`](coding_agents_integration/) — whether a coding agent's
  approval requests, waits, runs and restarts connect to north's real cards, queue,
  dashboard and recovery, before the feature is built.

## Flow setup and timing intake (2026-10-10)

The [small flow-intake probe](../tests/live/test_flow_intake_live.py) exercises North's
real configured-model provider with the general prompt, selected authoring procedure,
and actual `ask_user`/`create_flow` schemas. Its capability catalog and read-only
discovery results are synthetic; proposed tools are inspected, never executed.
It uses an already-fresh North token read-only, without refreshing credentials,
copying them, starting a daemon, or changing personal flows/schedules.

Before the change, one missing-timing case reproduced the bug: the model proposed
`create_flow:create` without asking when the flow should run. The setup guidance now
requires missing outcome, inputs/preferences, outputs/delivery, scope/safety, and
timing choices before authoring. Timing must be explicitly manual-only, one-time,
or recurring. Deferring activation/installation does not answer the timing question.
This reuses `ask_user` and the existing scheduling tools; no new scheduler or flow
document schedule field was introduced.

After the change, all eight real-model probes passed using `gpt-5.6-terra`: missing timing, incomplete
recurrence, explicit manual-only, and complete weekday/time/zone recurrence,
each repeated twice. Missing choices produced questions before any proposed
mutation; explicit choices proceeded without redundant questions. Ninety focused
unit tests passed; the broader unit/integration suite passed 2,831 tests, with one
skip and two existing dictation AsyncMock warnings. These are prompt/tool-choice checks, not end-to-end scheduling
or proof that every model will comply. One-time and unrelated
missing setup fields were not live-probed. The running user daemon was not restarted.

The user's correction makes timezone a settings-owned fact, not an intake choice.
Updated guidance no longer asks for timezone or declares it as a flow input.
Schedule tool schemas and both schedule API request schemas remove overrides,
and stale `tz`/`timezone` arguments are refused before writes. Recurring flows
resolve display, next firing, and previous firing through the existing settings
timezone; stored legacy timezone metadata does not override it. Legacy non-flow
entries retain their old zone. One-shot local times use settings at creation and
remain absolute instants thereafter. No migration or personal-data rewrite is needed.

The updated real-model probe supplied timezone only in trusted runtime context,
not the user's request: all eight cases passed again, without asking for timezone
or adding it to step inputs. The 251 focused tests also passed, including one-shot
and recurring timing in Los Angeles and Kolkata, both schedule APIs rejecting
overrides, actual settings-endpoint updates changing recurring flow firing and
display, and daylight-saving behavior. The provider requests were read-only probes;
no tool mutations, personal schedule changes, or daemon restart occurred.
The broader scheduling/configuration/CLI/integration regression selection passed
301 tests; lint, formatting, and diff checks passed.

## Prompt-authored custom flows (2026-10-09)

The [live custom-flow probe](../tests/live/test_custom_flow_live.py) uses North's configured
model in a disposable home, not a scripted model or a built-in job flow. A prompt created a
learned executable skill and updated a user-authored candidate flow through the normal
authoring tools, including fresh approval and a recoverable before-image. The candidate
declared four required string inputs and only `read_file`/`write_file`; nothing was activated
or scheduled. Test-mode execution does not require promoting the candidate skill.

The probe exposed silently ignored contract aliases (`input_schema`, `output_schema`,
`minimum_approval`). The authoring schema now advertises exact fields and fixed intent
identifiers, and the loader rejects unknown fields. Separate regression probes confirm
missing/wrongly typed resolved inputs pause before approval or agent work, and approved
user-document edits grant no future North ownership or source-edit exemption.

A cached-embedding comparison on the model-authored skill rejected both positive prompts
with missing intent metadata (selecting the coding delegation procedure instead). With
`review` metadata, both positives selected the custom procedure and both submission/browser
negatives excluded it. This informed the generic authoring guidance; it is not evidence of
successful activation or a reason to bypass selection validation.

Live execution produced and manually inspected `fit.md`, `application.md`, and
`questions.json` for a suitable synthetic role. Salary and authorization remained questions;
the draft used the supplied resume facts. Two independent suitable-role attempts completed
with schema-valid durable results and only file/review tools. An earlier attempt paused on
provider overload/cooldown; a repeat in the final batch paused when the provider stream stalled.
This is **partial evidence, not a passed four-case live probe**. Same-process repeat-fit,
mismatch, and hostile-listing checks remain unverified. Several earlier attempts stopped on overly strict
stand-in approval handling or a dashboard-history assertion, not production execution bugs;
the harness now checks actual tool identities, write targets, and durable results.

Regression check: **2,827 passed, 1 skipped**, with two pre-existing dictation mock warnings.
No live daemon restart, real user-flow modification, external application action, commit,
or push was performed.

```sh
env -u NO_COLOR NORTH_LIVE_CUSTOM_FLOWS=1 .venv/bin/pytest tests/live/test_custom_flow_live.py -q -s
env -u NO_COLOR .venv/bin/pytest tests/unit --ignore=tests/unit/experiments tests/integration -q
```

The optional `NORTH_CUSTOM_FLOW_SEED` points to a previous disposable experiment home to
reuse its model-authored candidates when iterating on execution checks. Seeded runs do not
re-test prompt authoring. Test artifacts are temporary; model availability and routing
selection remain explicit limitations, not silently promoted readiness.
