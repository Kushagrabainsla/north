# North experiments

This directory stores reproducible evidence behind architecture decisions.
Each experiment owns its inputs, runnable harness, recorded results, limitations,
and the production decision those results informed. Product code may reuse the
same primitives, but it must not import recorded results.

## Experiments

- [`browser_profiles_probe.py`](browser_profiles_probe.py) — disposable real Chrome
  sessions: separate synthetic logins, open-session reuse versus close/reopen,
  actual profile-directory checks, and North's connection/wrong-profile gate.
  No personal browser, credentials, or North settings are used. Temporary profiles
  are removed after the probe. Re-run with `.venv/bin/python -m experiments.browser_profiles_probe`.
  On 2026-10-10, all three open-session reuse rounds preserved both identities;
  close/reopen varied across runs: two of four checks in one run, all four in
  another (vendor and graceful close). It is not reliable across trials.
  Therefore North keeps sessions open between tasks, never purges ordinary closes,
  and requires fresh login evidence after reconnecting. These results are not a
  guarantee for every site, browser, or cookie policy.
- [`native_cdp_probe.mjs`](native_cdp_probe.mjs) — attach to the user's running
  Chrome through its built-in WebSocket-only debugging connection using public
  Puppeteer Core. Verify the actual profile, reject a wrong-profile expectation
  before any form interaction, and fill a synthetic form without submitting.
  This is an isolated SDK experiment, not North's production browser adapter.
  See the recorded comparison and limitations below.
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
- [`codex_prompt_cache/`](codex_prompt_cache/) — whether a run's first Codex
  call can reuse an earlier run's prompt cache (no, not enough to matter), and
  where calls inside a run lose it. See the section below.

## Browser and custom-flow completion regression (2026-10-10)

The installed chrome-agent rejected `goto about:blank`: its navigation helper
prefixes a URL without `://` with `https://`. North's managed-profile connection
test now reuses its existing `chrome://version` identity verifier rather than
patching the vendor or trusting navigation alone. The extended
[`browser_profiles_probe.py`](browser_profiles_probe.py) verified two fresh
managed profiles, WebSocket-URL attachment and wrong-profile rejection with
real Chrome exposing HTTP target discovery. This did not test Chrome's built-in
WebSocket-only debugging mode. Three reuse rounds kept both synthetic accounts separate; only two
of four close/reopen checks preserved login in this run. Reconnection still
requires site-specific login evidence, not a guarantee of restored cookies.

The [bounded browser/form check](../tests/live/test_browser_job_drafts_live.py)
used North's real BrowserTool and Safe-mode approval layer to verify a fresh
synthetic University profile, inspect two fake postings, and fill/assert name
and email on two fake application forms. Salary and authorization stayed blank;
the board recorded zero submissions and no one-time sign-in-token links.
Seventeen approval cards were recorded. It uses no model credentials or personal
profiles and purges only its unique test browser. This is scripted tool integration,
not an autonomous model/browser job-application test.

A prior real-model custom-flow run wrote the correct files but returned a typo in
one artifact path; FlowRunner nevertheless recorded success. Skills declaring an
`artifacts` array of string paths now use the existing nonempty-file helper before
completion. Missing, empty, directory and typo cases pause for manual, test and
scheduled runs; corrected paths resume normally. Relative paths use the granted
workspace, never an unspecified daemon working directory. This proves presence,
not freshness or content quality.

The [real-model custom-flow test](../tests/live/test_custom_flow_live.py) then
passed all four execution cases: two fitting-role drafts, a skipped senior-role
mismatch, and a hostile listing whose instructions were ignored. Every reported
file existed, salary/authorization remained questions, and no `PWNED` file was
created. This run reused previously model-authored candidates via
`NORTH_CUSTOM_FLOW_SEED`; it did not retest authoring, promote a candidate, or
schedule anything. Copied model credentials are removed by fixture cleanup.

The built-dashboard probe also passed creation/testing, setup resume, disconnect,
Settings panel ordering, and responsive checks at 1440, 820 and 390 pixels.
A broad regression run exposed a pre-existing equal-specificity conflict between
the onboarding steps and shared segmented control; the grid direction now has an
explicit compound selector while mobile column rules remain unchanged.
Final verification: 2,948 unit/integration tests passed, one skipped, with two
existing dictation AsyncMock warnings; all 64 frontend tests, the production
dashboard build, repository lint and changed-file formatting checks passed.
Personal settings/flows and the installed daemon were not changed or restarted.

## Existing-Chrome native CDP comparison (2026-10-10)

The user enabled Chrome's built-in remote debugging. The selected existing
University profile was verified against the directory reported by
`chrome://version`, not a profile label or a successful socket handshake.

| Approach | Observed result | Decision |
| --- | --- | --- |
| Installed chrome-agent 0.12.0 | Profile preflight failed: HTTP `/json/list` returned 404 on the native endpoint. | Cannot use this adapter for this connection mode. |
| Google Chrome DevTools MCP 1.10.1 | Its `new_page` tool rejects `chrome://version`, required by North's identity check. Native attachment through an allowed page was inconclusive. | Do not remove the identity check to accommodate the wrapper. |
| Public Puppeteer Core 25.11.0 | Three fresh connections verified the selected profile; all nine form-fill rounds passed. Every connection also rejected a wrong-profile expectation before form interaction. | Candidate at this stage; integration follow-up below. |

The public-SDK probe created and closed only its own tab and disconnected without
closing Chrome. Each round verified name/email values, an untouched salary field,
and zero synthetic form submissions. No passwords or cookies were read, and no
existing user tab was intentionally navigated or closed. No real job application,
resume upload, or flow-definition change was attempted.
The public SDK was installed with scripts disabled in a disposable npm prefix;
no North production dependency was added. An earlier successful experiment used
the Puppeteer bundle shipped inside Google's MCP package; the reproducible probe
deliberately avoids that internal module.

Reproduce only after the user has enabled native debugging and selected a profile:

```sh
NORTH_CDP_PROBE_DIR=$(mktemp -d /tmp/north-native-cdp-probe.XXXXXX)
npm install --prefix "$NORTH_CDP_PROBE_DIR" --ignore-scripts --no-audit --no-fund --package-lock=false puppeteer-core@25.11.0
node experiments/native_cdp_probe.mjs --user-data-dir '/path/to/Chrome' --profile-directory 'Profile 3' --sdk-root "$NORTH_CDP_PROBE_DIR"
```

The probe reads `DevToolsActivePort` from the supplied Chrome directory, uses only
its loopback browser WebSocket, and fails before filling if the actual profile
does not match. It never launches a browser or retries mutations. Chrome may
request connection permission; it cannot be bypassed. Node >=22.12.0 is required.

Limitations: one machine/profile and a synthetic form, three connections with
three fill rounds each. This does not establish reliable site logins, LinkedIn
navigation, resume upload, arbitrary forms, approvals, pause/resume, crash safety,
or end-to-end job-flow readiness. Production integration must preserve the
existing BrowserTool and central approval layer, tool-call profile selection,
fresh identity checks, page-based action classification, task-owned tabs, and
uncertain-action recovery. The job flow remains untested against the real site.

Regression verification: `env -u NO_COLOR .venv/bin/pytest tests/unit tests/integration -q`
passed 2,955 tests, skipped one, and reported two existing dictation AsyncMock
warnings. Running with the shell's `NO_COLOR=1` produced one unrelated truecolor
wordmark-test failure; that test passed when the variable was removed. Repository
Ruff checks, the probe's Node syntax check, and `git diff --check` passed.

## Existing-Chrome task windows and recovery (2026-10-10)

The [opt-in live test](../tests/live/test_existing_chrome_windows_live.py) now runs
North's actual BrowserTool through its central Safe-mode approval layer against
the explicitly selected running University Chrome profile. Only synthetic local
forms are used. The harness approves only its own browser requests and does not
alter the user's mode, saved profiles, flows, schedules or credentials.

Puppeteer Core 25.13.0 provides public `newPage({type: 'window', background: true})`.
The thin adapter reuses North's MCP client with the official MCP SDK 1.32.1; no
custom CDP transport, Chrome launch, profile cloning or credential export is added.
The runtime manifest and integrity lock are included in the built wheel; Node
dependencies are installed with scripts disabled on first approved use. npm's
lockfile audit reported zero known vulnerabilities in this dependency set.

Adversarial repetition rejected a tempting profile-routing approach: native
target metadata lists several normal profile context IDs, but Chrome does not
allow arbitrary normal-profile selection with those IDs. Some fresh-connection
trials passed and others failed before form interaction. Chromium's
[context manager](https://github.com/chromium/chromium/blob/main/chrome/browser/devtools/devtools_browser_context_manager.cc)
only resolves the last-used regular profile (plus its DevTools-created incognito
contexts). The implementation therefore uses the public window API, verifies the
actual profile path before use, closes mismatches and requests profile-selection
help through the existing ask_user layer. It does not probe other accounts.

The final three independent live trials all passed (62.25 seconds): six distinct
task windows and 18 form-fill rounds, with salary and work authorization left
blank, and zero submissions. Each trial verified that restarting the private
SDK worker restored the same window and its draft without navigation; a mutation
that threw after changing a field was not replayed after a further worker restart.
The original change remained exactly once. Submission controls were correctly
classified through the existing page-based approval code, without clicking them.
Task-owned tabs were closed at the end; Chrome itself was only disconnected.

Fourteen deterministic Node tests cover reuse, ownership, moved/closed tabs,
changed profile/endpoint bindings, context-identity changes, interrupted creation,
unknown outcomes, forged purpose flags, and invalid connection/action inputs.
The focused Python regression selection passed 44 tests. These tests do not prove
real LinkedIn login, MFA, resume upload, popup handling or job-flow completion.
Windows share the existing profile's cookies and extensions; this is isolation
of tab ownership, not isolation of accounts. New tasks may need the intended
profile brought forward first; existing verified tasks do not depend on whichever
profile is currently selected. No real application was attempted or submitted.

Final unit/integration regression: 2,965 passed, one skipped, with the same two
pre-existing dictation AsyncMock warnings. Repository Ruff checks, changed-file
formatting, Node syntax checks, the built wheel's resource inventory and diff
whitespace checks passed.

After confirming no active tasks or running flow runs, North was updated from
the local checkout and restarted. All health checks passed; the selected enabled
existing profile remained configured. The installed interpreter resolves the
new adapter and all six packaged SDK resource files. Chrome was not restarted,
and no settings, approval mode, personal flow or schedule was edited. The real
job-flow test is still pending; these results establish generic browser behavior.

```sh
env -u NO_COLOR NORTH_LIVE_EXISTING_CHROME=1 \
  NORTH_EXISTING_CHROME_DIR='/path/to/Chrome' \
  NORTH_EXISTING_CHROME_PROFILE='Profile 3' \
  .venv/bin/pytest tests/live/test_existing_chrome_windows_live.py -q -s
node --test tests/unit/tools/browser_windows.test.mjs
```

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

## Existing-browser bridge choice (2026-10-12)

Goal: use the user's own signed-in Chrome profiles with no repeated prompts, the
right profile per call, and no focus change while the user works. The probe
scripts were deleted after the decision; this table is the record.

| Approach | Outcome |
| --- | --- |
| Puppeteer over Chrome's remote debugging (CDP) | Forms and task windows worked, but Chrome asks to allow debugging on each new connection, and new windows open in the last-used profile. |
| chrome-agent / Chrome DevTools MCP attached to native Chrome | Failed on Chrome's WebSocket-only endpoint, or refused the identity page. |
| Playwright MCP Bridge extension (Microsoft) | Profile-aware with a key, but brought its window forward in all 18 routine trials. |
| Chrome Faithful extension | Background windows worked only with North's own patches; we would maintain a fork. |
| chrome-mcp, agent-browser-mcp, mcp-chrome | No separate-window support, weak or no local key, or unclear license. |
| Agent360 Browser MCP | Brings tabs and windows to the front, cannot tell profiles apart, unauthenticated local bridge since 1.30.1. |
| Browser MCP (browsermcp.io) | Drives the one tab you click Connect on; extension source not public; last update May 2025. |
| **TaskWindow 0.2.13 (chosen)** | Six disposable two-profile runs: 72/72 identity, draft and workspace checks, 24/24 reconnects without re-pairing, focus kept in every attributable trial. Real sjsu.edu and personal profiles at once: signed in, right Gmail account each, frontmost app never changed. |

Audit of TaskWindow at commit 6981df5 (6.6k lines; store build, release zip and
source identical): no outbound calls besides an npm update check, no cookie,
history or password access, agents limited to their own tab groups. One flaw:
the daemon gave its key to any request claiming the Web Store origin. North
patches that, pins both halves, and loads the extension unpacked so it never
auto-updates. Chrome lets only one extension ID debug at a time: per-profile
copies with different IDs locked each other out (reproduced in Chrome for
Testing), so every copy shares one manifest key and ID. Live check: `tests/live/test_taskwindow_bridge_live.py`.

## Codex prompt cache across runs (2026-10-22)

Question: Codex caches a prompt start only within one `session_id`, and north
sends the run id, so every run's first call is 0% cached. Would a session
shared across runs (per task, or per agent) fix that?

Answer: no. Codex caches the tool list **ahead of** the instructions, and
stops at the first tool that differs. Each run picks its own tools, so most
runs share little or nothing. `session_id` stays the run id.

Live probe ([`probe.py`](codex_prompt_cache/probe.py), `gpt-5.6-terra`, real
general-agent instructions and tool schemas):

| Call | Session | Prompt | Input | Cached |
| --- | --- | --- | --- | --- |
| 1 | S | tools A + task 1 | 10,869 | 0 |
| 2 | S | tools B (only the first tool differs) + task 2 | 10,373 | **0** |
| 3 | S | tools A + task 2 | 11,353 | 6,656 |
| 4 | new | copy of call 1 | 10,869 | 0 |

Calls 2 and 3 ran at once in one session; both worked. Call 4 confirms the
cache is per session.

Offline replay ([`replay.py`](codex_prompt_cache/replay.py)) of the 46
general-agent runs from Oct 17 to Oct 22. It re-selects each run's tools
with the production selection code and counts what the run before it, under
the same key, had already cached. Only 11 of 45 runs had the same tools as
the run before. First calls are 716k of 2.28M uncached input tokens (31%).

| Session key | Cache life | Cached on first calls | Share of all uncached | Runs helped |
| --- | --- | --- | --- | --- |
| per run (today) | any | 0 | 0% | 0 |
| per task | 5-60 min | 9,369 | 0.4% | 2 |
| per agent | 5 min | 43,877 | 1.9% | 8 |
| per agent | 60 min | 85,604 | 3.7% | 13 |

A per-agent key also puts every run on one key. OpenAI says a key spills to
other machines above about 15 requests a minute, which would lose part of the
74% cache reuse inside runs (4.46M tokens); losing 2% of that cancels the gain.

Inside a run ([`misses.py`](codex_prompt_cache/misses.py)): a later call has to
send fresh only what was added since the call before. Anything else it sends
uncached is a miss. From Oct 17 to Oct 22, 45 of 178 later calls missed. 33
ran before north sent `session_id` (5e6e089), and 9 before north stopped
rewriting older tool results as summaries (d55a91d). Since d55a91d, 98 later
calls were 86.5% cached. Of the 529k uncached tokens, 444k were new and could
not be cached yet. Only 3 calls missed, all when `find_tools` added a tool
(34.7k tokens). The rest is about 500 tokens a call. So the context is already
append-only, except when `find_tools` adds a tool, and a soft-budget note sent as
a system message would also break it (it last fired on 2026-09-18).

The only lever left is a tool list that stays the same across runs, such as
the full catalog (41 tools, about 12.7k tokens, against 3-5k selected). That
reverses lean tool selection and was not measured.

Limitations: the replay rebuilds tool sets without each task's plan text or
skill descriptions, so a few sets may differ from what ran. Token sizes are
north's 4-characters-a-token estimate. Cache life on the Codex backend is not
published; 5, 10 and 60 minutes bracket it.

```sh
.venv/bin/python -m experiments.codex_prompt_cache.probe    # about 45k tokens of Codex plan quota
.venv/bin/python -m experiments.codex_prompt_cache.replay --since 2026-10-17 --until 2026-10-22T19:10
.venv/bin/python -m experiments.codex_prompt_cache.misses --since 2026-10-18T07:18:25 --until 2026-10-22T19:10
```

Recorded numbers: [`results.json`](codex_prompt_cache/results.json).
