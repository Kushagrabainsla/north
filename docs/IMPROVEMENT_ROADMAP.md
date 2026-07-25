# North Improvement Roadmap

## Purpose

This document captures **solution-level improvements** (not one-off bug fixes) for reliability, speed, and learning quality in North.

---

## Solution Buckets

### 1) Inference Supply & Provider Resilience
**Goal:** Keep North reliably available even when one provider degrades.

**Solutions**
- Introduce provider health states (`healthy`, `degraded`, `down`) and route traffic accordingly.
- Add provider-level circuit breakers for non-retryable faults (auth, billing, quota hard-fail).
- Define explicit failover tiers (Primary, Secondary, Emergency) with quality/latency/cost targets.
- Shift from model-only fallback to provider+model policy fallback.

---

### 2) Execution Performance & Latency Control
**Goal:** Reduce long-tail response times while preserving output quality.

**Solutions**
- Enforce per-task latency budgets and stop low-yield fallback chains when budgets are exceeded.
- Apply intent-based routing (fast path for trivial work; deep path for complex work).
- Use progressive response behavior: return useful partials early, continue deeper synthesis only when needed.
- Add adaptive retry ceilings based on real-time provider health.

---

### 3) Orchestration State Integrity
**Goal:** Ensure task lifecycle data stays consistent across stores.

**Solutions**
- Define one canonical state machine for task/job/agent lifecycle transitions.
- Add a reconciliation worker to auto-heal state drift (stale pending rows, orphan statuses).
- Record terminal state invariants and validate them continuously.
- Introduce “state quality” SLOs (orphan rate, stale pending rate, reconciliation success rate).

---

### 4) Learning Loop & Decision Intelligence
**Goal:** Convert runtime behavior into measurable product learning.

**Solutions**
- Create a closed-loop learning pipeline from outcomes (success/failure/latency/helpfulness).
- Persist approval decisions as reusable policy signals, not just event logs.
- Blend short-term and long-term confidence updates to reduce noisy overfitting.
- Feed tool/model reliability back into routing and autonomy decisions.

---

### 5) Observability, Noise Control & Operations
**Goal:** Improve operational clarity and speed of recovery.

**Solutions**
- Normalize failures into a clear error taxonomy (auth, billing, quota, timeout, transport, policy, etc.).
- Deduplicate repeated errors into incident-level events with counters, not repetitive stack spam.
- Add operational guardrails that trigger degrade mode when thresholds breach.
- Build operator runbooks for common incidents (provider outage, queue growth, auth drift).

---

## Prioritized Roadmap

### Now (0–2 weeks) — Stabilize and De-risk
**Focus buckets:** 1, 2, 3, 5  
**Outcomes**
- Large reduction in repeated provider failures and retry storms.
- Lower p95/p99 latency through bounded fallback behavior.
- Fewer lifecycle inconsistencies via periodic reconciliation.
- Cleaner incident signal in logs/metrics.

**Primary deliverables**
- [x] Provider health states + provider-level circuit breaker policy.
- [x] Retry budget policy and latency budget enforcement.
- [x] Task-state reconciler with auto-heal actions.
- [x] Error taxonomy + log dedup/rate limits.

---

### Next (2–6 weeks) — Performance + Learning Maturity
**Focus buckets:** 2, 4, 5  
**Outcomes**
- Better response speed under mixed workloads.
- Smarter tool/model choice from observed effectiveness.
- Fewer unnecessary approvals for repeated safe patterns.

**Primary deliverables**
- [x] Intent-class routing policy (fast/deep paths).
- [x] Closed-loop confidence updater for tools/models.
- [x] Approval-memory integration into autonomy policy.
- [x] Incident dashboards for latency, failures, and fallback depth.

---

### Later (6+ weeks) — Strategic Optimization
**Focus buckets:** 1, 4  
**Outcomes**
- Stronger cost-performance tradeoff over time.
- More autonomous behavior with bounded risk.

**Primary deliverables**
- Capacity-aware provider portfolio optimizer.
- Multi-objective routing (quality/latency/cost/risk).
- Policy simulation mode for testing autonomy changes before rollout.

---

## Ownership and Module Execution Plan

> Suggested ownership model: assign one **DRI role** per workstream plus one **review owner**.  
> Replace role labels below with actual people once assigned.

### Team/Role Definitions

- **Inference Owner:** model/provider routing, fallback, cooldowns, pool refresh.
- **Orchestrator Owner:** task lifecycle, reconciliation, retries, router policy.
- **Memory & Learning Owner:** confidence updates, approval memory, episodic/fact loops.
- **Platform Observability Owner:** logging/metrics/error taxonomy/dashboards/runbooks.
- **CLI/TUI Owner:** user-facing feedback, status transparency, operator ergonomics.

### Deliverable-to-Owner Mapping (Now / Next / Later)

| Phase | Deliverable | DRI Role | Review Owner | Primary Modules |
|---|---|---|---|---|
| Now | Provider health states + circuit breaker policy | Inference Owner | Platform Observability Owner | `inference/dispatcher.py`, `inference/cooldowns.py`, `inference/providers/` |
| Now | Retry budget + latency budget enforcement | Inference Owner | Orchestrator Owner | `inference/dispatcher.py`, `orchestrator/failure_handler.py` |
| Now | Task-state reconciler + drift auto-heal | Orchestrator Owner | Platform Observability Owner | `orchestrator/reconcile.py`, `orchestrator/orchestrator.py`, `jobs/` |
| Now | Error taxonomy + log dedup/rate limits | Platform Observability Owner | Inference Owner | `orchestrator/failure_handler.py`, `utils/logging.py`, `orchestrator/app.py` |
| Next | Intent-class routing (fast/deep paths) | Orchestrator Owner | Inference Owner | `orchestrator/router.py`, `orchestrator/orchestrator.py` |
| Next | Closed-loop confidence updater | Memory & Learning Owner | Orchestrator Owner | `tools/confidence.py`, `tools.db` integration paths |
| Next | Approval-memory integration into autonomy | Memory & Learning Owner | CLI/TUI Owner | `approval/approval_memory.py`, `approval/`, `orchestrator/api_router.py` |
| Next | Incident dashboards (latency/fallback/failures) | Platform Observability Owner | Orchestrator Owner | `ledger/`, `orchestrator/metrics`, dashboard pipeline |
| Later | Capacity-aware provider portfolio optimizer | Inference Owner | Platform Observability Owner | `inference/dispatcher.py`, provider inventory/policy modules |
| Later | Multi-objective routing (quality/latency/cost/risk) | Orchestrator Owner | Inference Owner | `orchestrator/router.py`, `inference/model_scorer.py` |
| Later | Policy simulation mode for autonomy changes | Memory & Learning Owner | Orchestrator Owner | `agents/policy.py`, `orchestrator/`, offline eval harness |

### Ownership by Solution Bucket

| Bucket | Primary Owner | Secondary Owner | Core Modules |
|---|---|---|---|
| Inference Supply & Provider Resilience | Inference Owner | Platform Observability Owner | `inference/` |
| Execution Performance & Latency Control | Inference Owner | Orchestrator Owner | `inference/`, `orchestrator/router.py` |
| Orchestration State Integrity | Orchestrator Owner | Platform Observability Owner | `orchestrator/`, `jobs/`, task stores |
| Learning Loop & Decision Intelligence | Memory & Learning Owner | Orchestrator Owner | `tools/confidence.py`, `approval/`, `memory/` |
| Observability, Noise Control & Operations | Platform Observability Owner | CLI/TUI Owner | `utils/logging.py`, `ledger/`, `cli/` |

### 6-Week Handoff Cadence

- **Week 1:** DRIs finalize scope boundaries and success metrics for each Now deliverable.
- **Week 2:** design review + acceptance criteria sign-off by review owners.
- **Week 3–4:** implementation + shadow metrics collection.
- **Week 5:** controlled rollout with guardrails and fallback switches.
- **Week 6:** post-rollout review, metric deltas, and Next-phase reprioritization.

### Definition of Done for Owners

Each deliverable is done only when:
- behavior is visible in metrics (not just code merged),
- a rollback/degrade path exists,
- runbook entries are updated,
- and ownership for ongoing maintenance is explicitly assigned.

---

## Suggested Success Metrics

- **Reliability:** model/provider hard-fail rate, task failure rate, successful fallback rate.
- **Performance:** p50/p95/p99 task latency, fallback depth per task.
- **State integrity:** stale pending ratio, orphan lifecycle records, reconciliation heal rate.
- **Learning quality:** tool helpfulness trend, confidence calibration error, approval reuse rate.
- **Operations:** duplicate error volume, mean time to detect, mean time to recover.

---

## Non-Goals

- This roadmap does **not** prioritize isolated bug patches.
- This roadmap favors systemic architecture and policy improvements that prevent classes of failures.
