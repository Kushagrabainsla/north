# Lean architecture trials

First experiment batch, 2026-10-09. Production behavior is unchanged. These
results support choosing the next experiment; they do not approve implementation
or establish that any issue is solved.

The [focused safety validation](#focused-safety-validation-second-batch) below
extends these historical results with real offline builds, the installed Codex
sandbox executor, concurrent callers, and additional recovery faults.

## Run and inspect

```sh
.venv/bin/python -m experiments.lean_trials.trials
.venv/bin/pytest tests/unit/experiments/test_lean_trials.py -q
# Optional: consumes vendor quota; do not run until Claude quota is available.
.venv/bin/python -m experiments.lean_trials.reviewers
```

- [trials.py](trials.py): mechanical comparisons and queue simulation.
- [results.json](results.json): all arms, cases, seeded workloads, and outcomes.
- [reviewers.py](reviewers.py): six executable review fixtures, three repetitions,
  two prompts given identical information. Labels are checked by executing each
  small function against its explicit contract before any model call.
- [reviewer_results.json](reviewer_results.json): the live attempt and quota error.

The mechanical harness imports actual North policy, tool execution, effect log,
TODO store, confinement rules and capability fingerprints where labeled. Other
arms are experimental implementations confined to this directory. They are not
complete production replacements.

Fixtures use temporary SQLite databases, a synthetic home, and two local HTTP
servers. Kernel probes use macOS sandbox-exec, with unconfined positive controls
and explicit permission-denial checks. Child processes deliberately exit after
committing a simulated submission to test real process death and recovery.
Nothing submits a real application, sends a real notification, launches North,
installs a service, or reads a real secret. NORTH_HOME and Path.home are isolated
before product imports to avoid loading personal settings.

## Observed results

| Question | Comparison | Result and qualification |
| --- | --- | --- |
| Can different runs follow different owner choices? | Global ASK / global YOLO / per-run mode / per-run mode plus grants; 12 cases per arm | Global ASK creates friction in 3 intended automatic actions; global YOLO exceeds 7 fixture-specific constraints. Per-run mode alone exceeds 5; the grant prototype matches all 12. These are hypothetical per-run owner choices, **not violations of today's intentional global YOLO contract**. |
| What happens after a crash? | Current Tool.execute + EffectLog / durable intent with pause / receiver idempotency; 5 cut points per arm | Current path duplicates once after a committed effect but before logging success. Intent-and-pause prevents duplicates but leaves one action unfinished after an intent-only crash. Receiver idempotency has neither failure in these cases, **only because the fixture receiver implements atomic deduplication**. |
| Are persisted TODO ticks enough? | Current PlanStore / persisted ticks / persisted hash-and-attempt-bound evidence; 8 cases per arm | Both tick-based representations accept 6 invalid completion claims; the in-memory plan also loses completion after restart. Evidence-bound requirements accept both valid cases and reject all 6 invalid cases. This compares possible checklist designs; **it is not evidence of a bypass in North's current landing gate**. |
| Can sandbox bounds be narrower? | Current Bash Seatbelt profile / explicit loopback port / explicit port plus synthetic-home read boundary; 7 cases per arm | Current profile permits 2 undesired accesses: an unapproved local server and a new unlisted credential file. Port restriction removes the first; the home boundary removes both. All 4 legitimate fixture actions still work in every arm. **Real toolchain builds, vendor agents and OS variations remain untested.** |
| Does priority help most workloads? | FIFO / foreground priority / priority with one provider quota admission reserved; 200 discovery plus 200 held-out seeds | Priority improves foreground mean latency in **185/400**, ties in **215**, loses in **0**; background mean latency worsens in **179**. Reserve improves foreground mean latency over priority in **96**, ties in **301**, loses in **3**; background worsens in **152**, and total draining takes longer in **35**. Both comparisons have zero median foreground improvement across all scenarios. There is **no strict majority improvement** across this synthetic distribution. |
| Is an outbox sufficient? | Direct send / SQLite outbox / outbox plus receiver deduplication; 4 faults per arm | Direct send loses 2 events. Outbox loses none but duplicates 1 after a lost acknowledgment. Receiver deduplication removes that duplicate in the fixture. A channel without receiver deduplication still needs tolerance for duplicate notification deliveries. |
| Must capability evidence be redesigned? | Name+version / existing flow+skill fingerprints / fingerprints plus all bundled files; 11 cases per arm | Name+version misses 7 behavior changes and invalidates 1 harmless version-label change. Existing fingerprints catch 6 definition changes but miss a referenced file's changed bytes. Adding bundled-file hashes catches all 7 but invalidates 1 ignored notes-file edit. This points to a small dependency-evidence extension, not proof that a new registry is needed. |
| How should loopback addresses be accepted? | Literal IP check / literal IP plus exact localhost; 8 addresses per arm | Both reject all 4 unsafe fixtures. Literal-only needlessly rejects localhost; adding exact localhost accepts all 4 legitimate fixtures. This is **input validation only**, not server integration, HTTP authentication, TLS, or protection against a changed localhost resolver. |

Queue policies were frozen before running the second 200 seeds. Discovery and
held-out win/tie/loss counts are recorded separately. Each arm receives exactly
the same arrivals, provider, duration and quota within a seed. The simulation has
two worker slots per provider, fixed ten-tick quota windows, and returns an unused
reserve to background jobs late in a window. It drains all jobs; no dropped work
is hidden in the latency metric. It does not run North's scheduler or model API,
model variable token costs, account-wide daily quotas, preemption, or retries.
Its p95 is an empirical lower order statistic; small workloads have coarse tails.
The selected workload distribution is not a sample of actual North usage.

## Live reviewer result

The first Claude call failed with its weekly quota exhausted (reported reset:
October 11, 03:00 America/Los_Angeles). Stopped immediately: **one attempted call,
zero completed reviews, zero paired comparisons, $0 reported inference cost**.
No prompt, model, reviewer-count, or provider-fallback quality winner can be
inferred. The optional harness caps a complete run at 36 requests, $0.05 per
request, two concurrent requests and a 45-second request timeout. It disables
tools, MCP, customizations, Chrome and session persistence, uses an isolated
working directory, and sends only the synthetic contracts and code.

Even a completed run would have only six unique, short, hand-authored tasks.
Repetitions measure variability on those tasks, not general coding competence.
The two strategies are plain review and explicit invariant checking; this is
**not** a test of one versus two review rounds or end-to-end agent changes.

## Decision and next experiment for every issue

| Issue | Evidence now; next experiment before adoption |
| --- | --- |
| #74 per-run modes | Scope/mode cases support separation; next use two overlapping actual runs, child inheritance, pause/resume and changing defaults. |
| #70 smoke defects | Crash and delivery fixtures cover possible failure shapes only. Reproduce each reported card/conflict/branch/general-run defect against North; none is declared fixed. |
| #67 default agent/fallback | Claude exhaustion was observed; fallback was not tested. Inject quota/auth errors into both backends and prove the old writer has stopped before handing off its actual diff/tests. |
| #66 removed coder cleanup | No benefit measured here. Audit remaining references and run import, routing and TUI checks; do not invent an architecture experiment for simple dead-code removal. |
| #65 harder evals/live probes | Kernel and crash probes ran; live review unavailable. Run held-out larger coding tasks, real approval cards and both vendors in disposable repositories when quota is available. |
| #64 Codex confinement | The actual generated rules deny existing synthetic credentials, but do not explicitly deny a new home entry made after rule generation. **Not a live Codex escape test.** Next prove enforcement and compatible builds/refusal reporting with Codex itself. |
| #63 coding checklist | Evidence-bound prototype beats persisted ticks on declared requirements; next attach real test/review evidence to a disposable coding run and try edits, old attempts and restart. |
| #51 recall MCP/plugin | Not tested. Compare explicit scoped recall versus bounded cached workspace briefs for relevance, permission leakage, tokens and hook latency. |
| #49 channels | Outbox faults support durable retry with qualified deduplication; next test actual adapters, reconnect cursors and concurrent decisions on one real approval card. |
| #47 capabilities | Extend existing fingerprints rather than assuming a registry replacement; next validate bundled dependencies, file/path safety and differing validators for tools, flows and skills. |
| #46 entity memory | Not tested. Use ambiguous names, aliases, unassigned facts and held-out queries; compare current recall, entity-only and hybrid recall with permission filtering. |
| #45 common execution | Real tool crash gap reproduced, but whole-run lifecycle consolidation not tested. Next interrupt actual flow/coding/chat runs at durable boundaries and verify cancellation and stale completion rejection. |
| #40 scoped tokens | Grant matching is only a precursor. Token minting, hashing, authentication, resource scopes and current revocation are untested; next use isolated API clients and scheduled jobs. |
| #39 confirmed grants | Prototype supports bounded account/action/spend constraints without blocking authorized cases. Next test confirmed server-owned grants against hostile tool descriptions and confused account/resource targets. No universal danger floor was added. |
| #38 loopback bind guard | Input validation passed; next exercise every actual startup entry point without opening a public listener and test IPv4/IPv6 resolution. |
| #37 OS sandbox | Real kernel probes show two specific Bash bounds worth narrowing; next measure compatibility across actual builds, Git/worktrees, caches and local test servers. Linux and full vendor integration remain untested. |
| #36 always-on LaunchAgent | Crash safety matters before automatic restarts. Launchd installation/startup was not tested; next use a disposable service label to check launch paths, single instance and restart throttling. |
| #26 dashboard | Outbox fixtures touch notification reliability, not UI behavior. Next test existing API controls, card state, task steering and history search in browser fixtures. |
| #8 quota starvation | Priority is the leaner candidate; reserve has added tradeoffs. Next replay real sanitized demand traces through North's admission path, checking foreground latency, background progress and provider cooldowns. |

## Interpretation limits and validation

Safety counterexamples are deterministic. Repeating the same seven kernel cases
or five crash cuts is a reproducibility check, not seven/five independent samples
of real-world attack or failure probability. Prototype guards enforce the same
declared requirements used to label their fixtures; passing them is necessary,
not evidence of generalized quality or resistance to hostile models.

The scoped-policy experiment assumes trusted, correctly classified action facts
and grants. It does not mint client credentials, parse natural-language grants,
exercise prompt injection, or establish that every real tool supplies reliable
resource/account/spend metadata. ASK counts as not automatically allowed, not
permanent refusal. Receiver idempotency assumes a stable logical-operation key
and an atomic receiver-side uniqueness check; it is not available on arbitrary
web forms. The fixtures do not test key collisions, retention or changed payloads.

These receiver qualifications match the mechanisms described in the
[AWS Builders' Library](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/).
The scheduling tradeoff is consistent with
[Google SRE's overload guidance](https://sre.google/sre-book/handling-overload/),
but neither source establishes North-specific performance; that comes only from
the labeled local tests and simulation above.

Validation: 98 focused tests passed (8 harness checks plus existing approval
policy/effects, PlanStore, Codex confinement and flow registry tests); Ruff passed.
The final mechanical suite was repeated with fresh databases, home paths,
processes and ephemeral ports, and its result data matched. No production source,
GitHub issue, commit, push or live daemon was changed by this experiment batch.

## Focused safety validation (second batch)

2026-10-09. Experiments only: no production fixes, commits, pushes, daemon changes
or issue closures. No model calls or real external submissions were made.

```sh
.venv/bin/python -m experiments.lean_trials.sandbox_validation
.venv/bin/python -m experiments.lean_trials.recovery_validation
.venv/bin/pytest tests/unit/experiments/test_safety_validation.py -q
```

- [sandbox_validation.py](sandbox_validation.py) and
  [sandbox_validation_results.json](sandbox_validation_results.json).
- [recovery_validation.py](recovery_validation.py) and
  [recovery_validation_results.json](recovery_validation_results.json).
- [Prototype assertions](../../tests/unit/experiments/test_safety_validation.py).

### Bash confinement: safety AND compatibility

Each arm runs 21 cases in three fresh fixtures: 13 legitimate operations and
8 boundary probes. All cases also pass their unconfined positive controls.
This exercises the real macOS kernel and North's current Seatbelt profile;
candidate profiles are local prototypes, not changes to North's Bash tool.

| Approach | Legitimate failures / 39 | Boundary breaches / 24 |
| --- | ---: | ---: |
| Current profile | 0 | 24 |
| Narrow ports + close home, retain broad caches/toolchain directory | 15 | 15 |
| Narrow ports + isolated paths + scrubbed environment | 6 | 0 |
| Same + isolated Git object store | 0 | 0 |

Legitimate cases: Python stdlib, pytest, Git status/add/diff/staged-file reading,
workspace writing, two permitted local listeners, C compilation/execution, Node,
an offline npm local-dependency install, and an offline uv wheel install into a
new virtual environment. Install correctness is checked by importing the result.
npm lifecycle scripts are disabled; this is not a full application build.

Boundary probes: an unapproved localhost control-API stand-in, a home credential
created after profile generation, a symlink read escape, credentials neighboring
shared caches and toolchains, an out-of-workspace write, a symlink write escape,
and an inherited synthetic environment credential. All secrets and servers are
fixtures; no actual North control endpoint or real credential is probed.

The synthetic home lives under system temporary storage. The two write breaches
demonstrate overly broad **shared-temp** access, not arbitrary writes to a real
home outside allowed temp/cache roots. Environment inheritance is explicitly
modeled with a synthetic token; this harness does not exercise North's actual
subprocess-environment construction.

The simple home restriction breaks four Git commands and uv interpreter access.
Opening the worktree's own index still does not let Git write new objects.
Setting GIT_OBJECT_DIRECTORY to a per-run directory and
GIT_ALTERNATE_OBJECT_DIRECTORIES to read the owner's history passes these cases
without granting writes to the owner's shared object database. The strict profile
denies home file data but preserves ancestor metadata traversal, permits vetted
runtime paths, and restricts writable paths to the workspace, its own Git admin,
and its own cache/temp directories. Broad cache/bin read exceptions are removed.

**Decision:** this is the best Bash candidate tested, not yet a deployable patch.
North's parent process must correctly consume/export the isolated Git objects and
retain their environment throughout validation and landing. That was not tested.
Runtime read roots also need review for unexpected credentials. Network ports
need trusted allocation and scoped endpoint authorization; allowing a port does
not make every endpoint behind that listener safe.

### Codex: actual executor, not just rule inspection

Installed `codex-cli 0.162.0`, using its `codex sandbox` command with an explicit
permission profile; no inference, login request or provider quota needed. Each
arm runs three legitimate and three boundary cases, repeated in three fresh
repositories. Invalid configurations are not counted as successful protection.
Successful reads are checked against the exact expected fixture contents.

| Approach | Legitimate failures / 9 | Boundary breaches / 9 |
| --- | ---: | ---: |
| Current generated snapshot | 3 | 3 |
| Deny home parent, reopen workspace | 6 | 0 |
| Also reopen Git roots + isolate objects | 6 | 0 |
| Private Git clone, workspace write permission | 3 | 0 |
| Private clone + explicit private .git write permission | 0 | 0 |

The current executor permits the **late-created home file** in all three runs.
It blocks the existing credential and its symlink. It also fails staging newly
modified content in the linked worktree because shared objects are read-only.
Blanket home denial fixes the late-file gap but breaks Git's ancestor traversal,
even when Git roots are reopened. A separate local clone avoids linked-worktree
metadata dependence; explicitly opening its private .git allows index writes.
The clone is made with --no-hardlinks so its objects are not shared writable
inodes with the owner's repository.

This supersedes the first batch's **rule-inspection-only** evidence for the
late-file gap, but is still **not** a full agent/app-server run. Model-selected
tools, vendor file-edit tools, approval hooks, network restrictions, refusal
reporting, config loading, and North's review/landing path remain untested here.
Large-repository clone cost was not measured. The private-clone candidate does
not establish equivalent confinement for these other paths.

**Decision:** do not ship a blanket home deny or rescan-based repair. The tested
vendor profile cannot preserve metadata traversal for linked worktrees. A
private Git copy is a working fixture alternative, but it needs end-to-end North
landing and resource-cost validation before choosing that layout change.

### Recovery: durable ownership, qualified retries

The baseline uses real Tool.execute/Approvals/EffectLog with an isolated local
SQLite receiver. Three candidates use a separate experimental atomic intent
ledger: pause on uncertainty, receiver-idempotent replay, and receipt lookup.
Each arm is tested at seven cut/failure points and 20 forced overlaps.

| Approach | Duplicate fault cases / 7 | Unfinished fault cases / 7 | Duplicate overlaps / 20 |
| --- | ---: | ---: | ---: |
| Current completion log | 2 | 0 | 20 |
| Atomic intent + pause | 0 | 1 | 0 |
| Atomic intent + receiver deduplication | 0 | 0 | 0 |
| Atomic intent + receipt lookup | 0 | 1 | 0 |

Faults include death before dispatch, after durable intent, after the receiver
commits, after local success, a lost acknowledgment, a proven pre-send failure,
and a fault-free control. Death uses separate subprocesses with abrupt exits and
fresh-process recovery. Since the current path has no intent claim, its two
pre-dispatch cuts both occur before Tool.execute.

The baseline duplicates after a committed effect without a completion record,
and after a lost acknowledgment. Forced overlap exposes a missing atomic claim
in the execution primitive; it is **not** evidence that North's normally
serialized mutation scheduler currently overlaps these calls. Candidates race
two threads with separate SQLite connections; multi-daemon coordination was not
tested.

Fifteen extra checks (five per candidate) cover an unavailable read-only ledger,
changed payload under the same ID, intentionally distinct IDs with identical
payloads, an older sender still running, and a failed local completion write.
The failed claim sends nothing; changed payloads are rejected; separate intents
both execute; overlap and completion-write failure do not duplicate effects.
The completion-write failure is injected after an actual receiver commit, not
a simulated pre-send error.

Pause and receipt lookup intentionally leave one intent-only crash unfinished.
**A missing receipt is not permission to resend:** an older sender might still
complete. A positive, strongly consistent receipt can confirm success; otherwise
the operation stays unknown. Receiver deduplication succeeds automatically only
because its transaction atomically checks a stable operation ID and applies the
effect, rejecting changed payloads under the same ID. Arbitrary websites do not
offer that guarantee. SQL failure before dispatch blocks sending in all three
candidates; failure to record completion afterwards leaves uncertainty rather
than being reclassified as a safe pre-send failure.

**Decision:** extend the existing effect log with an atomic, committed intent
after authorization but before sending. Preserve a stable logical operation ID
across retries, and distinguish a new intentional action from a retry. Pending
operations recover through receiver deduplication, trusted positive receipts,
or an explicit unknown-state pause—not a timer or blind resend. Production
integration still needs operation-ID plumbing, adapter capability declarations,
database migration, approval placement, and a clear owner-facing recovery state.

### Reproducibility and limits

112 focused tests passed: 14 new prototype assertions, 8 first-batch assertions,
and 90 existing policy/effects/plan/confinement/flow-registry checks. Ruff passed.
Both final suites were rerun with fresh databases, repositories, processes and
ports. Recovery result data matched exactly; every sandbox case outcome and
summary matched, excluding timing and incidental error-text paths. Result files
include the corresponding harness source SHA-256.

Three repeated sandbox fixtures and twenty forced races are reproducibility
checks, not independent samples of real-world workload or failure probability.
No majority-of-real-builds improvement is established. Tests are macOS-only,
use tiny offline packages, and do not cover real credentialed downloads,
arbitrary filesystem escapes, port-reuse races, database corruption, power loss,
deduplication retention, or hostile receiver behavior. The security wins apply
to the enumerated boundaries, not a claim that all sandbox gaps are closed.
