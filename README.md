# north
### Personal Life Operating System - Architecture & Design Document
> Version 0.3 · May 2026

---

## Table of Contents

1. [The Vision](#1-the-vision)
2. [How It's Different](#2-how-its-different)
3. [The Moat](#3-the-moat)
4. [Architecture Overview](#4-architecture-overview)
5. [Perception Layer](#5-perception-layer)
6. [The Ledger](#6-the-ledger)
7. [Context Layer](#7-context-layer)
8. [Onboarding - Solving the Cold Start](#8-onboarding--solving-the-cold-start)
9. [The Orchestrator](#9-the-orchestrator)
10. [Agent Layer](#10-agent-layer)
11. [Approval Layer](#11-approval-layer)
12. [Interface Model](#12-interface-model)
13. [Inference Router](#13-inference-router)
14. [Storage Model](#14-storage-model)
15. [End-to-End Data Flow](#15-end-to-end-data-flow)
16. [Open Questions & TODOs](#16-open-questions--todos)

---

## 1. The Vision

Most of what we call work in daily life is not real thinking. It is coordination overhead. Researching where to invest, writing a grocery list, planning a trip, drafting a task plan for the week - none of this requires you specifically. It just requires context about you.

north is a personal AI operating system that runs continuously in the background. You give it a north star - what you want to achieve, who you want to become - and it handles the operational work across every domain of your life: finance, health, career, travel, logistics. You review, approve, and enjoy the output. The cognitive load of coordination disappears.

> **Core principle:** You should spend your time thinking, deciding, and experiencing - not managing. north manages so you do not have to.

This is different from existing AI tools like ChatGPT or Perplexity, which are reactive - you ask, they answer. north is proactive. It knows your goals, watches your life passively, and surfaces work to you already done or ready to approve. The interaction model is closer to a chief of staff than a search engine.

---

## 2. How It's Different

Several tools today explore parts of the north vision, but none combine all of its components into a single coherent system.

| Tool | What it does | What it lacks |
|------|-------------|---------------|
| ChatGPT / Claude | Reactive conversation, some memory | No persistent context, no background activity, no action |
| Claude Code / Cursor | Excellent coding agent | Single domain, no life context |
| Rewind / Limitless | Passive mic + screen capture, searchable memory | Stops at recall - never acts on what it captures |
| Siri / Alexa | Voice commands, shallow integrations | No deep personal model, no multi-step coordination |
| LangChain / CrewAI | Agent orchestration frameworks | Blank slates - no memory, no perception, no approval layer |
| Notion / Obsidian | Knowledge organization | Does not act or make decisions |

north sits at the intersection of all these by combining continuous perception, structured personal memory, decision modeling, multi-LLM agent execution, and an approval loop into a single unified pipeline. The gap between knowing information and acting intelligently on it - that is where north lives.

---

## 3. The Moat

The technology itself is not the moat. Any engineer can replicate the architecture. The real moats are:

**The context layer compounds daily.** After six months, it contains something no competitor can replicate - a deep, accurate, continuously updated model of you. The switching cost grows every day you use it.

**The judgement rules are irreplaceable.** Your decision patterns, confidence-scored over hundreds of real decisions, cannot be transferred. A competitor would have to watch you make hundreds of decisions again from scratch to rebuild what north accumulated.

**Trust is a moat.** north sits in an extraordinarily sensitive position - it hears your conversations, sees your screen, knows your goals, plans your life. Once someone trusts a system with this level of access, they are extremely unlikely to give the same access to a competitor.

**The biggest threat** is not a startup - it is Apple or Google building this into the OS natively. They already have the mic, screen, calendar, and contacts. The only answer to that threat is going deeper on personalization faster than they can. A platform player building for billions will always build something more generic than a system built for one person.

---

## 4. Architecture Overview

north is built from seven distinct layers. Each has one clear job. Together they form a pipeline from raw perception of your world to real-world execution on your behalf.

```
┌─────────────────────────────────────────────────────┐
│                     YOU                              │
│         North stars · Goals · Voice commands         │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────┐
│                PERCEPTION LAYER                      │
│         Mic · Screen · Native integrations           │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────┐
│                   THE LEDGER                         │
│     Append-only · Timestamped · Configurable         │
└──────────────────────┬──────────────────────────────┘
                       │ extraction pipeline
┌──────────────────────▼──────────────────────────────┐
│                 CONTEXT LAYER                        │
│   public · private · privacy rules ·                 │
│   judgement rules · north stars                      │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────┐
│                 ORCHESTRATOR                         │
│    North star check · Decompose · Route              │
└──────┬───────────┬───────────┬───────────┬──────────┘
       │           │           │           │
┌──────▼─┐  ┌─────▼──┐ ┌──────▼─┐  ┌─────▼──┐
│Finance │  │ Health │ │  Work  │  │ Travel │  · · · (plug-and-play)
└──────┬─┘  └─────┬──┘ └──────┬─┘  └─────┬──┘
       └──────────┴─────┬─────┴──────────┘
                        │ Task Context Object
┌───────────────────────▼─────────────────────────────┐
│                 APPROVAL LAYER                       │
│    Information · Approval · Question cards           │
└───────────────────────┬─────────────────────────────┘
                        │
┌───────────────────────▼─────────────────────────────┐
│             REAL-WORLD EXECUTION                     │
└─────────────────────────────────────────────────────┘
         ▲ feedback loop back into context layer
```

Data flows in one direction. Perception feeds the Ledger. The Ledger feeds the Context Layer via the extraction pipeline. Context informs the Orchestrator. The Orchestrator directs Agents via the Task Context Object. Agents surface output through the Approval Layer. Your approvals and overrides flow back into the Context Layer. The loop is complete and self-improving.

---

## 5. Perception Layer

The Perception Layer is how north understands your world without you manually updating it. It runs continuously and passively.

### 5.1 Microphone Input

A microphone runs continuously and captures all audio - conversations, calls, podcasts, videos, meetings. Transcribed locally using Whisper and written immediately to the Ledger.

The microphone is the primary passive input channel. Most of what matters about your life passes through audio - decisions made in conversations, preferences expressed, commitments given, things learned while listening. Capturing this means the system learns about you without any deliberate effort on your part.

> **Why continuous?** Manually logging your life is friction. The moment you have to remember to update a system, it becomes a task in itself. Running continuously removes this friction entirely.

### 5.2 Screen Input

Screen capture is the universal integration fallback. Many applications do not expose APIs, or their APIs are paywalled or change frequently. Rather than building and maintaining dozens of integrations, the screen reader sees what you see and extracts relevant context from it.

Screen capture operates in three modes:

- **On-demand** - you explicitly ask the system to look at your screen
- **Triggered** - activates when you open an app that has no native integration
- **Periodic** - lightweight screenshot every few minutes when the system is active

> **Why screen over integrations?** Native integrations break when apps update APIs, move features behind paywalls, or deprecate endpoints. The screen is a stable universal interface - it works with any app, forever, because it sees exactly what you see.

### 5.3 Native Integrations

Where apps provide good APIs, native integrations are preferred - they give structured, reliable, low-cost data. Examples: Gmail, Google Calendar, GitHub, Notion, Spotify.

Priority rule: **prefer native integration → fall back to screen → fill gaps with mic.**

### 5.4 Wake Word and Intent Detection

Not everything the microphone hears is a command to north. A lightweight always-on wake word detector listens for a specific trigger phrase. When detected, the system shifts from passive capture to active command mode and routes the intent to the Orchestrator. Everything else stays passive and flows only to the Ledger.

### 5.5 Mobile as Perception Extension

The mobile app extends the perception layer beyond the desktop. When you leave your machine, your phone's microphone continues capturing. The system does not go blind when you are away from your desk. Audio captured on mobile feeds into the same Ledger as desktop capture.

---

## 6. The Ledger

The Ledger is a permanent, append-only record of everything the Perception Layer captures. It is never modified - only written to. Think of it as a personal life log.

Every entry is timestamped and tagged with its source:

```
[2026-03-31 09:14:22] [mic] [source: call]
  I think I want to lean more toward ML infrastructure, pure backend is getting repetitive

[2026-03-31 10:45:01] [screen] [source: browser - moneycontrol.com]
  User viewing HDFC Bank, price 1642, checking 52-week high

[2026-03-31 11:20:44] [mic] [source: ambient]
  remind me to look into Anyscale pricing before the LinkedIn internship starts
```

### 6.1 Three Purposes

1. **Raw material** for the extraction pipeline - the Context Layer is derived from it
2. **Searchable personal memory** - query it directly to recall decisions from weeks ago
3. **Safety net** - if the extraction layer misses something, the Ledger preserves it for reprocessing

### 6.2 Configurable Retention Windows

The Ledger uses a rolling archive model with fully configurable window sizes. Nothing is deleted by default.

```yaml
ledger_config:
  hot_window_days: 30        # actively queried, full fidelity, fast access
  archive_after_days: 30     # compress and move to cold storage after this
  delete_after_days: null    # null = never delete. set a number to auto-purge.
  compression: true          # compress archived entries to save space
```

- **Hot window** - recent entries in active SQLite, fully queryable, fast
- **Cold archive** - older entries compressed and stored, retrievable when needed but not actively queried
- **Delete** - disabled by default. Storage cost for compressed text is negligible even at years of scale.

Changing window sizes does not require touching any other part of the system. Tune them as you learn how you actually use the Ledger.

### 6.3 Privacy Routing in the Ledger

Not all Ledger entries are equal. Before writing an entry, the Perception Layer checks the privacy rules. Entries containing sensitive content - specific financial figures, health details, relationship dynamics - are flagged as private and written to the local Ledger only. Everything else goes to the cloud Ledger.

---

## 7. Context Layer

The Context Layer is the most important component of north. It is a structured, persistent model of you - your goals, constraints, preferences, decision patterns, and north stars. Every agent reads from it. Without it, agents are generic tools. With it, they are your personal specialists.

> The context layer is the primary moat. Every day it runs, it knows you better. After six months, it contains something no competitor can replicate. The switching cost grows every day.

### 7.1 Extraction Pipeline

A background job runs periodically - every few minutes - reading new Ledger entries, passing them through a fast cheap LLM, and writing only meaningful context deltas to the Context Layer.

The extraction layer asks one question about every Ledger entry: *does this tell me something new, meaningful, and durable about this person?* A preference expressed, a decision made, a goal updated, a commitment given - these are context-worthy. Background noise and filler conversation are discarded.

The Ledger stores everything. The Context Layer stores only meaning. Agents read from Context, not the Ledger, which keeps their inputs clean and compact.

### 7.2 The Five Documents

```
context/
  public.json            ← who you are. freely read by all agents.
  private.md             ← sensitive details. local only. gated, permission-required.
  privacy_rules.md       ← who can access what, and under what conditions.
  judgement_rules.md     ← how you decide. confidence-scored, self-updating.
  north_stars.md         ← what you are working toward, across all time horizons.
```

#### public.json
Freely available to all agents in every interaction. Contains your general goals, skill set, schedule preferences, dietary habits, interests, and risk appetite. Populated initially through onboarding, then updated continuously by the extraction pipeline. Stored in cloud.

#### private.md
Sensitive information that agents cannot read automatically. Includes specific account numbers and balances, medical details, relationship dynamics, and anything flagged as private. When an agent needs it, it raises a request. You approve. That session uses it and closes cleanly.

**Stored locally only. Never leaves your machine.**

> **The incognito model:** Think of private context access as an incognito session. The data is present in the system but compartmentalized - gated, session-scoped, and not mixed into regular context.

#### privacy_rules.md
A document you edit directly. Defines which agents can request private context, which have automatic access, and which topics always route to private context regardless of source. Stored in cloud.

#### judgement_rules.md
A living document that writes itself entirely from watching you make decisions. You never touch it directly. Every approval, override, and answered question writes a delta to it. Over time it becomes a distilled model of how you think - not just what you prefer, but your reasoning patterns.

```
Finance:
  - Willing to take higher risk on small cap tech          [confidence: 8/10]
  - Never approves anything over ₹50,000 without breakdown [confidence: 10/10]
  - Rejected HDFC Bank three times - skip without catalyst [confidence: 7/10]

Travel:
  - Layover acceptable if savings exceed ₹10,000           [confidence: 9/10]
  - Always prefers window seat                             [confidence: 10/10]
  - Never books non-refundable hotels > 2 months out       [confidence: 6/10]
```

Each rule carries a confidence score. A rule seen once is a hypothesis. A rule seen twenty times is a reliable preference. The Orchestrator weights rules by confidence when deciding whether to auto-approve or surface a question. This prevents one unusual decision from permanently changing behavior. Stored in cloud.

#### north_stars.md
Your goals across every time horizon simultaneously. Every action the Orchestrator takes first checks this document. If a suggestion conflicts with a north star at any horizon, the system surfaces the tension rather than resolving it silently.

```
Lifetime:   Financial independence, meaningful technical work at scale
5-year:     Principal / Staff engineer at a top infrastructure company
1-year:     Crush LinkedIn internship, publish ML research paper
3-month:    Ship north v1, complete CS271 with strong grade
This week:  Finish architecture doc, start Phase 2 of hallucination project
```

North stars are not limited to one window. Multiple simultaneous horizons. The system evaluates actions bottom-up - weekly first, then upward. When two north stars conflict, it surfaces that tension to you rather than picking a side. Stored in cloud.

> **Why multiple horizons?** A single fixed-window goal is too rigid for real life. Goals exist at every scale simultaneously. The north stars model lets the system serve your weekly focus without losing sight of your lifetime direction.

### 7.3 Feedback Loop

The Context Layer is not static. Every approval, override, and answered question flows back as a delta into public context and judgement rules. The longer it runs, the less it needs to ask, and the better its defaults become.

---

## 8. Onboarding - Solving the Cold Start

On day one, the context layer is empty. The judgement rules are blank. The north stars are undefined. A system that knows nothing about you cannot be useful, and a system that is not immediately useful will not be used long enough to learn. This is the cold start problem.

Onboarding solves it by seeding the context layer before passive perception has had time to build it naturally.

### 8.1 Structured Questionnaire

When you first set up north, it asks a short set of structured questions across each domain - current goals, financial situation in broad strokes, dietary preferences, work setup, travel patterns. A deliberate 20-minute investment that pays off immediately.

The questionnaire also captures your north stars. You define goals across time horizons - lifetime, five years, one year, this quarter, this week. This becomes the steering document for everything the Orchestrator does from day one.

### 8.2 Document Ingestion

Beyond the questionnaire, you can upload documents that describe your current life state:

- **Resume** - professional background, skills, career trajectory
- **SOPs and personal notes** - how you like to work
- **Financial statements** - current position snapshot
- **Health records** - baseline health context
- **Any other relevant document**

The extraction pipeline processes uploaded documents exactly like Ledger entries - pulling context deltas and writing them to the appropriate context documents. Someone who uploads ten documents on day one has a context layer that would otherwise take weeks of passive capture to build.

> **Why document ingestion matters:** Passive perception is powerful but slow. It takes weeks of continuous listening to learn things a resume tells you in seconds. Document ingestion gives the system a running start.

---

## 9. The Orchestrator

The Orchestrator is the brain of north. It sits between the Context Layer and the Agent Layer. Its job: take your goals and incoming intents, translate them into concrete coordinated work, and route that work to the right agents.

### 9.1 What It Does

When you state an intent, the Orchestrator does four things in order:

1. Reads the relevant slice of the Context Layer - constraints, preferences, prior decisions
2. Checks your north stars - does this task serve your goals across all time horizons?
3. Decomposes the intent into sub-tasks and identifies which agents handle each
4. Creates a Task Context Object and coordinates execution

### 9.2 North Star Check

Before routing any task, the Orchestrator checks it against north_stars.md. If it aligns, it proceeds. If it conflicts with a goal at any horizon, the Orchestrator flags the tension before any work is done. It never silently resolves a north star conflict.

Example: you ask the system to book a weekend trip to Goa. The Orchestrator checks north stars. Your three-month north star is shipping north v1. Your one-year north star includes a savings target. Before routing to the travel agent, it surfaces: *this trip costs ₹18,000 and falls during a week you marked as a deep work block. Do you want to proceed?* You decide.

### 9.3 Trigger Model

**Current version:** manually triggered via voice command with a wake word. You state your intent, it takes over. Intentional - manual triggers keep the system predictable while trust is being established.

**Future version:** proactive - waking itself on a schedule or in response to signals from the Perception Layer. A Monday morning briefing reviewing progress toward your north stars and surfacing the week's agenda is the first natural step.

### 9.4 Task Decomposition

The Orchestrator breaks complex intents into discrete, agent-sized sub-tasks. Identifies dependencies - which must complete before another begins - and which can run in parallel. Routes each sub-task to the appropriate agent with the relevant context slice attached.

---

## 10. Agent Layer

Agents are niche specialists. Each knows one domain deeply and operates only within it. They do not talk to each other directly - they communicate through the shared Task Context Object managed by the Orchestrator.

### 10.1 Plug-and-Play Agent Model

The agent layer is a plugin registry. The Orchestrator does not know what agents exist in advance - it reads the registry at runtime and routes tasks to whatever is registered. Adding a new agent means dropping it into the registry with a standard interface. No changes to anything else.

Every agent declares four things when registering:

```yaml
agent:
  name: finance_agent
  domain: finance                          # what category of tasks it handles
  context_requirements:                    # which context slice it needs
    - public.json
    - private.md (requestable)
  tools:                                   # external tools and APIs it has access to
    - market_data_api
    - portfolio_tracker
  task_interface:                          # what tasks it accepts and what it outputs
    accepts: [research, analysis, budget, forecast]
    output_format: structured_json
```

This means the system is infinitely extensible. Legal agent that reviews contracts? Register it. Learning agent that builds study plans from your north stars? Register it. Relationships agent that surfaces when you should reach out to someone important? Register it. The architecture supports it without modification.

The underlying model powering any agent is also swappable. Better model released tomorrow - point the agent at it. Done. Nothing else changes.

> **Why plug-and-play?** Your life will change. The agents you need in six months are not exactly the agents you need today. A plugin registry treats extensibility as a first-class design property - new capability is additive, never disruptive.

### 10.2 Initial Agent Set

| Agent | Responsibilities |
|-------|-----------------|
| Finance | Stock research, portfolio review, budgeting, expense analysis, savings planning, forex |
| Health | Meal planning, grocery lists, dietary tracking, supplement reminders, workout planning |
| Work | Task planning, coding assistance, calendar management, meeting prep, project tracking |
| Travel | Flight and hotel research, itinerary drafting, visa requirements, local recommendations |
| Research | General research, summarising articles and papers, competitive analysis |

### 10.3 The Task Context Object

The Task Context Object is the shared workspace created by the Orchestrator for every multi-agent task. It replaces direct agent-to-agent communication. Agents read from it, write to it, and raise questions through it. They never need to know which other agents exist or what they are doing.

```
TaskContext {
  task_id          → unique identifier for this task
  intent           → the original user request
  triggered_by     → source and timestamp
  status           → in_progress | awaiting_input | awaiting_approval | complete
  north_star_check → alignment with active north stars, conflicts flagged

  user_context     → relevant slice from the Context Layer

  agents_involved  → list of agents assigned to this task

  shared_state     → where agents read and write their outputs
  {
    finance   → { estimated_cost: X, budget_ok: true, forex_advice: Y }
    work      → { conflicts: [], calendar_blocked: true }
    travel    → { flights: [...], hotels: [...], itinerary: draft }
    health    → { dietary_notes: Z, vaccines: [] }
  }

  dependencies     → ordered constraints between agent tasks
  questions        → raised by agents when genuinely unsure
  approval_items   → decisions requiring your sign-off
  final_output     → assembled by Orchestrator from shared_state
}
```

### 10.4 The If-Unsure-Ask Rule

Agents follow a clear decision hierarchy when they encounter ambiguity:

1. **Check Context Layer and Judgement Rules first.** The answer is probably already there.
2. **Make a reasonable default, proceed, and flag it** transparently in the approval layer so you can override.
3. **If the decision is consequential and no clear default exists - stop and ask.**

When an agent asks, it writes a question into its shared_state slot and sets the task status to `awaiting_input`. The Orchestrator surfaces it through the interface. You answer via voice. The answer is written back to the Task Context Object, the agent resumes, and the answer is added to judgement rules so the agent never asks the same question twice.

> **Why this rule matters:** Agents making assumptions silently is worse than agents asking. A wrong assumption compounds - the travel agent books the wrong hotel class because it assumed your budget was higher. Every answer you give makes the system smarter for next time.

---

## 11. Approval Layer

The Approval Layer is your primary interface with north. You do not interact with agents directly. You interact with a clean surface that shows you what the system has done, what it is about to do, and what it needs from you.

### 11.1 Three Card Types

**Information cards** - things the system has done autonomously and is reporting back.
- "Your weekly meal plan is ready."
- "Portfolio summary for the week: up 2.3%."

**Approval cards** - consequential actions the system wants to take but needs your sign-off.
- "Book this flight for ₹32,000. Approve?"
- "Buy 5 shares of this stock. Approve?"

**Question cards** - genuine ambiguities an agent could not resolve from context or judgement rules alone.
- "Do you prefer to stay in Shinjuku or Shibuya?"
- "Direct flight for ₹45,000 or layover via Dubai for ₹28,000?"

### 11.2 How Judgement Rules Filter Cards

Before surfacing any card, the Orchestrator checks the judgement rules. If a rule clearly covers the situation it either auto-approves, auto-rejects, or pre-fills a recommendation. You only see a card if the situation is novel or the judgement rules are ambiguous. Over time the approval layer gets quieter - it asks you less as it learns your patterns.

### 11.3 Trust Calibration

Not all actions require the same oversight. You can configure trust thresholds per action category:

- **Low-stakes, repeatable** (grocery list, article summary, meal plan) → can be set to auto-approve
- **High-stakes, irreversible** (moving money, booking non-refundable travel, calendar commitments) → always pause for explicit approval regardless of judgement rules

Trust thresholds are adjustable at any time through privacy_rules.md.

### 11.4 Interface Design

The interface is intentionally minimal. It is not a chat app. It is a notification surface. Cards appear when the system has something to surface. You review, approve or override, and move on. The system does not demand your attention - it waits for yours.

---

## 12. Interface Model

north has three parallel interfaces serving three distinct purposes. None replaces the others - they are all first-class and used simultaneously.

### 12.1 CLI - Control Plane

The CLI is how you operate *on* the system. Configure settings, inspect the context layer, debug an extraction, manually edit a judgement rule, run a specific agent, force a ledger reprocess. Developer mode. Always available regardless of what the voice layer is doing.

Use it when you are building, tuning, or debugging. Think of it the same way as SSH into a server - you use it when you need control, not when you need speed.

```bash
north context view public
north context edit judgement_rules
north ledger reprocess --from 2026-03-01
north agent run finance --task "portfolio review"
north config set ledger.hot_window_days 14
```

### 12.2 Voice - Interaction Plane

Voice is how you interact *with* the system naturally. Trigger tasks, answer questions, give approvals, state new goals. Low friction, no context switching, always available. A wake word activates the Orchestrator. Everything else is passive ambient capture.

Use it during your day without breaking flow. You never open an app. You never type. The system comes to you.

### 12.3 Mobile App - Remote Control + Perception Extension

The mobile app does two things:

**As output** - the approval layer in your pocket. Cards surface as push notifications. You tap to review, approve, or override from anywhere. You never need to be at your desk to keep the system moving.

**As input** - an extension of the Perception Layer when you are away from your machine. Your phone's microphone continues capturing when you leave your desk. Conversations, calls, podcasts on the go - all feed into the same Ledger as desktop capture. The system does not go blind when you leave.

---

## 13. Inference Router

north does not use one model for everything. Different components have different requirements - some need speed and cheapness, some need deep reasoning, some need accuracy. The right model is used for the right job.

The inference router reads a model config file. Each component declares its preferred model and a fallback. You can tune cost vs quality per component independently.

```yaml
inference_config:
  extraction_layer:
    model: claude-haiku        # fast, cheap, high volume, simple classification
    fallback: gpt-4o-mini

  orchestrator:
    model: claude-sonnet       # needs strong reasoning and planning
    fallback: gpt-4o

  agents:
    finance_agent:
      model: claude-sonnet     # consequential, needs accuracy
      fallback: gpt-4o
    health_agent:
      model: claude-haiku      # straightforward domain, lower stakes
      fallback: gpt-4o-mini
    travel_agent:
      model: claude-haiku      # mostly retrieval and synthesis
      fallback: gpt-4o-mini
    work_agent:
      model: claude-sonnet     # coding and planning need strong reasoning
      fallback: gpt-4o
    research_agent:
      model: claude-sonnet     # deep synthesis, use strongest available
      fallback: gpt-4o

  judgement_rules_writer:
    model: claude-haiku        # simple pattern writing, high frequency
    fallback: gpt-4o-mini

  north_star_check:
    model: claude-sonnet       # consequential, needs nuance
    fallback: gpt-4o
```

Swapping a model means changing one line in this config. Nothing else in the system changes. As better models are released, you point components at them and move on.

---

## 14. Storage Model

Storage in north is split by sensitivity. Private data never leaves your machine. Everything else lives in the cloud where you get better semantic search, faster retrieval, and less infrastructure to maintain.

```
Storage Router:

  private.md           → local only. never leaves the machine. ever.

  public.json          → cloud
  judgement_rules.md   → cloud
  north_stars.md       → cloud
  privacy_rules.md     → cloud

  Ledger entries       → routed by content sensitivity
    private-flagged    → local SQLite only
    everything else    → cloud
```

The **privacy routing layer** is the gatekeeper. Before any piece of data is written anywhere, it checks privacy_rules.md to determine the destination. Private-flagged content routes local. Everything else routes cloud. This happens automatically - you never have to think about it.

**Starting stack:**
- Local: SQLite for the private ledger and private context
- Cloud: Supermemory (or equivalent) for semantic retrieval over public context, judgement rules, and north stars

> **Why cloud for non-private data?** Better semantic search, vector retrieval over your judgement rules and north stars, and less infrastructure to maintain yourself. Start with what makes the system fast and useful. Optimize later.

> **Why local for private data?** Your most sensitive information should never be on someone else's infrastructure regardless of how mature the system gets. This is a permanent design decision, not a temporary one.

**Inference** uses cloud APIs - your chosen LLM (Claude, OpenAI, etc.). This is the same tradeoff you already accept when using Claude or ChatGPT today. Prompts include slices of your context layer, so non-private context may appear in inference calls. Private context is only included in inference when you explicitly approve it for that session.

---

## 15. End-to-End Data Flow

Tracing a complete example - you say: *"Plan a trip to Japan in June."*

```
1. Wake word detected. Orchestrator activates.

2. Orchestrator reads Context Layer:
   budget, dietary restrictions, leave balance, passport validity.

3. Orchestrator checks North Stars:
   → 1-year goal includes savings target
   → 3-month goal: ship north v1
   → Flags potential tension. Will surface in final approval card.

4. Orchestrator creates Task Context Object for this trip.

5. Work agent runs (parallel):
   Checks calendar, identifies available June dates.
   Writes → shared_state.work: { conflicts: [], available_dates: [...] }

6. Finance agent runs (parallel):
   Estimates trip cost, checks savings target alignment.
   Writes → shared_state.finance: { estimated_cost: ₹28,000, budget_ok: true }

7. Travel agent runs (after work + finance complete):
   Searches flights and hotels within confirmed budget and dates.
   Checks judgement_rules: window seat preferred, layover ok if savings > ₹10,000.
   Finds qualifying flight. One ambiguity remains - hotel area not in judgement rules.
   Sets status: awaiting_input
   Writes → shared_state.travel.questions: ["Shinjuku or Shibuya?"]

8. Approval Layer surfaces Question card:
   "Do you want to stay in Shinjuku or Shibuya?"

9. You respond via voice: "Shinjuku."
   → Answer written to Task Context Object
   → Answer written to judgement_rules: "Prefers Shinjuku for Japan trips [confidence: 1/10]"
   → Travel agent resumes, finalises itinerary.

10. Health agent runs:
    Dietary notes for Japan, restaurant types to seek.
    No vaccine requirements.
    Writes → shared_state.health: { dietary_notes: [...], vaccines: [] }

11. Orchestrator assembles final output from all shared_state slots.
    Flags north star tension: trip costs ₹28,000, falls during deep work week.

12. Approval Layer surfaces Approval card:
    Complete trip plan - flights, hotels, itinerary, dietary notes, budget.
    North star conflict noted: "This falls during your north sprint week."
    You review, decide the trip is worth it, approve.

13. Finance agent executes payment.
    Travel agent confirms booking.
    Context Layer updated with trip details.
    Judgement rules updated.
```

**Total effort from you:** one voice sentence, one question answered, one approval tapped. The north star tension was surfaced and you made an informed decision. Everything else was done by the system.

---

## 16. Open Questions & TODOs

The following are deliberately deferred to the next phase of design.

**Unresolved decisions:**

- **Technical MVP scope** - the smallest version of this that provides daily value and can be built and tested in weeks, not months
- **Proactive orchestration triggers** - how the system decides on its own to do something without a voice command. The Monday morning briefing is the first candidate.
- **Multi-device sync** - how the context layer stays consistent across phone, laptop, and future devices. Especially the local private.md.
- **Cloud storage provider** - Supermemory vs alternatives for the public context layer. Defer until the system is running and scale is clearer.

**TODOs:**

> **TODO - Error recovery:** What happens when the system gets something wrong - the extraction layer misclassifies something, the judgement rules learn a bad pattern from one unusual decision, or an agent acts on a wrong assumption. Need a mechanism for correcting the system's model without breaking accumulated good patterns. Think through: manual override of specific judgement rules, confidence decay for stale rules, rollback of context deltas, and reprocessing Ledger entries with a corrected extraction prompt.

> **TODO - Persona / Mental Models layer:** A system where you can load the mental models of historical figures - Machiavelli, Robert Greene, Buffett, Einstein - and the system surfaces suggestions through those lenses. Three modes: advisory (personas comment on suggestions), lens (a persona's principles are baked into a specific agent's reasoning), and debate (two personas surface their disagreement for your consideration). Personas are advisory only - they never override your north stars or judgement rules.

> **TODO - Agent handoff for real-time back-and-forth:** The current Task Context Object model handles most coordination well. There may be complex tasks where agents need to iterate in real-time rather than a single shared state pass. Design needed.

---

*You set the destination. north handles the navigation. You live the journey.*
