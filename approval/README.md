# approval

The `approval` package decides whether north does an action without asking you, and it shows you the cards that ask. A tool describes an action as facts. `ApprovalPolicy.rule()` rules on it. Only a ruling of "ask" becomes a card (CODING_STYLE §7.3). The package also keeps your past decisions, the safe-action list, and the notifiers that reach you.

This package is **Protected** in `docs/MODULES.md`. Do not change it without explicit approval from the user. A change here can let north do more without asking.

## Main parts

| File | Key names | What it does |
|---|---|---|
| `policy.py` | `ApprovalPolicy`, `Action`, `ActionKind`, `Ruling`, `Verdict`, `Answer`, `Decider` (Protocol), `needs_your_confirmation`, `may_replay_yes` | The one decision point. `rule()` goes through tiers from hard facts to learned memory, and the first match wins. `answer()` decides who answers a question card. It is the only code that reads the approval mode. |
| `approvals.py` | `Approvals`, `Request`, `Decision` | The entry point. `Tool.execute()` calls `decide()` for each call that changes something. Before the policy, it refuses a repeat send or spend and a path you said no to. It also lets an action through that an earlier yes in the same task covers. `decide_ahead()` asks once for several exact calls. |
| `decider.py` | `MemoryDecider` | Autonomous mode. A model reads your facts, profile, rules, past decisions and chat messages, and rules with a reason and the memory it used. It returns `None` when it cannot decide, and then the card waits for you. |
| `interaction.py` | `UserInteraction`, `CardEvent` | The one path from a `Card` to you. It puts decision cards in the store, waits for the answer, and sends information cards to the notifiers. |
| `store.py` | `ApprovalStore` | The card registry, in SQLite. `resolve()`, `overrule()`, `wait_for_decision()`, `pending()`. At startup, a blocking card that is still pending becomes `TASK_ENDED`. |
| `models.py` | `Card`, `CardField`, `CardType`, `ApprovalDecision`, `DecidedBy`, `MemoryRef`, `action_fingerprint` | Card data and enums. `DecidedBy` names who decided each card. |
| `approval_memory.py`, `decisions.py` | `ApprovalMemory`, `ensure_decisions_table` | Your approve, reject and answer decisions, one row each in the `decisions` table. `recall()` replays only your own decisions for the exact action. |
| `standing_rules.py` | `offer`, `digest`, `offer_morning_digest` | Makes your answer to one card a rule for one kind of action, in all of north. |
| `unattended.py` | `UnattendedPolicy`, `forbidden_reason` | The safe-action subset (test, lint and build commands, device toggles, messages to you, edits in the workspace, and browser field fills, which ship turned off). The two hard rules are in code here: a send to another person and a spend are never auto-approved. |
| `unattended_rules.py` | `UnattendedRuleStore`, `UnattendedRule` | The safe-action list as rows you edit on the web UI. Shipped rules are seeded as rows. |
| `effects.py` | `EffectLog` | Records each send or spend that succeeded, per task and per flow, so a resumed step does not do it again. |
| `rejections.py` | `RejectionLog` | Records the paths you said no to in a task, so the "no" covers later reads too. |
| `session.py` | `Session`, `NO_SESSION`, `SessionReader` | Whether a run started from chat, and your messages in that chat. |
| `continuation.py` | `CardContinuations`, `CardOutcome` | Runs the next step after a non-blocking card is decided, by the card's `source`. |
| `notices.py` | `NoticeStore`, `Notice` | What north delivered to you, kept on the dashboard until you dismiss it. Notices ask nothing. |
| `base.py`, `terminal.py`, `telegram.py`, `batching.py` | `Notifier` (ABC), `TerminalNotifier`, `TelegramNotifier`, `BatchingNotifier` | Alert channels. `BatchingNotifier` sends one alert for a burst of non-blocking cards. |
| `callback_server.py` | `app` | A small FastAPI app on `settings.callback_port` (default 8001). It forwards a button decision to `/orchestrator/approval/respond`. |
| `exceptions.py` | `ApprovalError` | Base error of the package. |

## The four modes

The mode is `ApprovalMode` in `config/approval_mode.py`. `ApprovalPolicy` reads it at each decision, so a change applies at once.

- **ask**: read-only actions run. Every other action asks.
- **safe**: read-only actions, the safe-action list and exact replays of your answers run. Every other action asks.
- **autonomous**: the fixed tiers decide first, then `MemoryDecider`. If the decider cannot decide, a card waits.
- **yolo**: every approval is yes, and every question gets the yes answer.

Below yolo, a change to north's own settings always asks. In every mode, north's own tools may not edit files in a git repository. No mode answers a card that asks for a secret.

## How north uses it

- `orchestrator/app.py` builds `ApprovalPolicy`, `MemoryDecider`, `UserInteraction` and `Approvals`, and gives the one `Approvals` to the tool registry. It also wraps the notifier chain (`BatchingNotifier` around `TelegramNotifier`) and starts the callback server.
- `config/dependencies.py` makes `ApprovalStore`, `NoticeStore`, `CardContinuations` and the default `TerminalNotifier`.
- `tools/base.py`: `Tool.execute()` calls `Approvals.decide()` before `run()`. A tool overrides only `describe()`.
- `agents/agentic_llm_agent.py` uses `UserInteraction` for `ask_user`, `request_approval` and `request_secret`.
- `orchestrator/orchestrator.py` resolves and overrules cards through `ApprovalStore`.
- `orchestrator/coding_gate.py`, `coding_fetch.py` and `coding_landing.py` ask `Approvals` about coding-agent actions. `orchestrator/coding_ask.py` uses `UserInteraction`.
- `web/views.py` calls `needs_your_confirmation()`. `web/conversations.py` makes `Session` values.
- The public interface (`__all__`) is `ApprovalDecision`, `ApprovalError`, `Card`, `CardEvent`, `CardType`, `MemoryDecider`, `Notifier`, `TerminalNotifier` and `UserInteraction`.

## Data on disk (in `~/.north`)

- `approvals.db`: the cards (`approval_cards`).
- `notices.db`: the notices.
- `approval_memory.db`: your decisions (`decisions`), the safe-action list (`unattended_rules`), completed sends and spends (`completed_effects`) and refused paths (`rejected_paths`).

## Rules

- Decide whether north acts without asking only in `ApprovalPolicy.rule()`. No other code may branch on the mode for this (CODING_STYLE §7.3).
- A tool states facts on an `Action`. It does not read the mode, keep an allowlist or give a verdict (CODING_STYLE §7.3, §7.5).
- Rules use facts, never an agent's name (CODING_STYLE §7.3).
- Every ruling names its rule, and every decided card names who decided it (`DecidedBy`).
- A card never expires. Park the waiting task instead (CODING_STYLE §13.5).
- Keep the two hard rules in `unattended.py`, not in the rule table. A row can be turned off; code cannot.
- A yes never replays for a send, a spend or a change to north (`may_replay_yes`).
- A tool with no approval layer gets `Approvals.unbound()`. It refuses every action that would ask, so a missing gate is never an open gate.
- Text from an agent, a repository or a tool is untrusted. It never authorizes an action in autonomous mode.
