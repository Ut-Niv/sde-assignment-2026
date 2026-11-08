# — Submission
---

## 0. TL;DR
Controller continuously ingests call events, maintains per-variant / hourly / caller-ID stats, explores then promotes welcome+voice with Wilson lower bounds, auto-rotates degraded caller IDs when policy allows, and routes every other change through the approval API with summary/diff/dry-run/impact. On public_01: **+2.9% conclusion rate**, 4/5 applied changes beneficial. On public_02: mixed (3 beneficial / 3 harmful); DID-flag response incomplete. Cut deep cohort modelling, multi-arm bandits, full C11 UI, and credit-fair allocation.

## 1. Assumptions
1. The HTTP API is the only source of truth; scenario YAML ground truth is never read at runtime.
2. Primary KPI is the workflow's declared `objective.primary`; guardrail is respected only passively (we do not yet reject promotions that hurt pickup when optimising engagement).
3. Approver latency is random in the declared window; we poll every tick and apply immediately on approval.
4. History chunks are complete and idempotent by chunk number; restart resumes from `last_chunk` in SQLite.
5. A 5–6 pp absolute pickup gap between caller IDs is enough evidence of degradation to rotate.
6. Soft promotion (70–75% winner, residual exploration) is safer than winner-takes-all under non-stationarity.
7. `mid_flight: next_campaign_only` means allocation changes only affect new campaigns; schedule/prompt still apply immediately — we still propose full configs.
8. Credits are scarce on incidents; we only call LLM/validation on hallucination spikes.

## 2. Scope decisions
| # | Capability | Built / Partial / Cut | Why | What it would take to finish |
|---|---|---|---|---|
| C1 | Event ingestion at load scale | Built (batched SQLite) | Floor + needed for all else | Partition by workflow for 10× |
| C2 | Shared cohort object | Partial (default + observed cohort_id only) | Time; default traffic is majority | Feature-rule cohorts + size floors |
| C3 | Bootstrapping from history | Built | Needed for day-0 decisions | — |
| C4 | Call-time optimisation | Partial (hour_weights from history) | High ROI, auto-apply eligible | Per-person preferred hour |
| C5 | Caller-ID health | Partial | Critical for public_02 | Sliding-window rates, faster rotate |
| C6 | Variant experimentation | Built (welcome + voice) | Core learning loop | Multi-dim interactions, sequential testing |
| C7 | Winner promotion / decay / rollback | Partial (soft promote, no explicit rollback) | Time | EWMA decay detector + rollback proposal |
| C8 | Hallucination incident path | Built (detect → LLM → validate → propose) | Required by contract for prompt changes | Tighter credit budget + post-apply verify |
| C9 | Governance client | Built | Non-negotiable | Smarter batching / TTL awareness |
| C10 | Impact / affected / dry-run | Partial (honest-ish stats dump) | Approver requirements | Proper counterfactual dry-run |
| C11 | Supervisor view | Cut | Floor is audit store | Thin FastAPI read API |
| C12 | Decision/audit store | Built (SQLite decisions table) | Floor | — |
| C13 | Crash recovery | Built + demo script | Floor | — |
| C14 | Observability of the loop | Partial (logs only) | Time | Prometheus metrics |
| C15 | Fair credit/approver allocation | Cut | Single-tenant public scenarios | Global budget scheduler |

**Principle:** ship a complete, constraint-respecting loop that improves the primary KPI on the happy path, keeps an auditable trail, and survives restart. Defer polish and multi-tenant fairness.

## 3. Stakeholders

### 01 Sales
- **Accepted:** measurable lift on conclusion rate is the story; public_01 shows +2.9% overall.
- **Pushed back:** promising “fully autonomous in week one” — governance latency and supervisor edits make that false.
- **Wrong claim:** that every workflow can be optimised the same way; objectives and mid_flight policies differ.
- **Reply:** We ship a controller that already moves conclusion rate on the public dev scenario and records every change for the client. Autonomy is earned per workflow under their governance policy, not granted day one. Happy to join a pilot with one tenant and a shared success metric.

### 02 Client Compliance
- **Accepted:** absolute governance boundary; every change goes through `/approvals` or policy-scoped auto-apply; audit log answers “who/when/why”.
- **Pushed back:** real-time human approval of every allocation tweak — would freeze learning; we use auto-apply only where the client already enabled it (schedule, caller_id).
- **Wrong claim:** that auto-apply is unconstrained; policy `allowed_dims` and `max_shift` bind us.
- **Reply:** No live config change bypasses the boundary you configured. Prompt changes require a validation run above your pass-rate threshold. Full decision log is queryable from our store independently of the simulator.

### 03 Platform / SRE
- **Accepted:** restart safety, idempotent chunk cursor, bounded memory via batched aggregates.
- **Pushed back:** “no new stateful services” — a local SQLite file is the minimum durable cursor/audit store; we can swap to Postgres later.
- **Wrong claim:** that the controller can be pure stateless; pending approvals and last_chunk must survive process death.
- **Reply:** Controller is a single process + SQLite. Kill -9 and restart completes the scenario without double-apply. At load we will partition by workflow and export metrics (ingest lag, decisions/hour, credit burn).

### 04 Client Ops Supervisor
- **Accepted:** one change summary a busy human can read; we never silently overwrite a supervisor edit (stale base → clear pending).
- **Pushed back:** “alert me on every experiment” — that is alert fatigue; we only page on incidents (hallucination spike, caller-ID collapse).
- **Wrong claim:** that the controller can pause dialling; cadence/window/capacity are business-owned.
- **Reply:** You will see a single approval card with summary, diff, affected-lead estimate and dry-run evidence. Supervisor edits always win; we re-propose on top of your version.

### 05 Product
- **Accepted:** separate pickup vs engagement KPIs; primary/guardrail from workflow config.
- **Pushed back:** rich cohort UI and multi-armed bandit complexity in v1.
- **Wrong claim:** that engagement and pickup can be blended into one score; contract forbids it.
- **Reply:** Loop optimises the declared primary KPI with residual exploration. Guardrail monitoring is next; we will not ship a blended score.

### 06 CTO
- **Accepted:** statistical winner selection (Wilson LB), not LLM; LLM only suggests prompt alternatives.
- **Pushed back:** full multi-tenant credit fairness and 10× scale proof in this submission window.
- **Wrong claim:** none material against the contract.
- **Reply:** Architecture is event-sourced aggregates + governance client + audit store. public_01 shows beneficial autonomy; public_02 exposes gaps in incident latency we will close before production.

## 4. Architecture

```
[sim HTTP] --advance--> Controller.run()
                |
                v
         events/chunks (NDJSON)
                |
         batch aggregate ──> SQLite (call_stats, hourly, caller_id_health, decisions, pending)
                |
         detect incidents (hallucination, DID drop)
                |
         experiment cycle (explore → Wilson promote → schedule weights)
                |
         auto-apply (if policy)  OR  POST /approvals
                |
         poll approvals → POST /apply
                |
         log decision row (C12)
```

State lives in `controller_state.db`. On restart: load `last_chunk`, reconcile pending approvals, continue.

## 5. The learning loop
- **Cohorts:** currently traffic is scored under `default` and under observed `cohort_id` from events; we do not yet author new cohort rules. Minimum dials before promotion: 80 (rate) / 40 connected (engagement).
- **Experiment design:** start uniform over option list; after min samples, soft-max to winner (70–75%) with residual exploration. Welcome and voice treated independently (no interaction model yet).
- **Winner:** Wilson score lower bound (z≈1.64) on the primary KPI; no LLM in the decision.
- **Decay / rollback:** not fully implemented; residual exploration gives a path back if the winner fades. Explicit EWMA rollback is a known gap.
- **Call time / caller ID:** hour_weights derived from historical primary-KPI by hour; caller IDs rotated when pickup trails the best peer by ≥5 pp.
- **Guardrail:** not yet enforced as a hard veto; observed as passive metric in dry-run dumps.

## 6. Governance
- **Lifecycle:** propose → pending → (approved|rejected|expired) → apply | clear; on `stale_base_version` we drop pending and re-evaluate next cycle.
- **Batching / timing:** at most one open proposal per workflow; we do not yet throttle to `max_requests_per_24h` proactively (API enforces).
- **Auto-apply:** used for schedule (and caller_id when shift is within policy); allocations/prompt always go through approvals.
- **Impact / affected / dry-run:** affected ≈ 2.5× daily batch; dry-run embeds observed voice/welcome/hourly tables; impact_score heuristic 0.35–0.9 by severity.
- **Supervisor mid-edit:** `config_changed` with actor supervisor clears stale pending; next proposal uses new base_version.

## 7. Incidents
- Hallucination: connected calls with `hallucination_flag` rate ≥ 8% on a prompt → LLM suggest-fixes → validation_run (n=150) → propose only if pass_rate ≥ policy minimum.
- Caller-ID: relative drop ≥ 5 pp and absolute pickup < ~0.22 → zero weight / auto-apply rotate.
- public_02: DID-flagged incident on ks_cancel was only partially mitigated (cumulative stats diluted the signal; share of flagged DID stayed ~50%). Supervisor edits were respected (no overwrite). MTTR for DID remains a weakness.

## 8. Scale
Not fully measured on public_03 in this pass. On public_01 (~3 workflows):
- Ingest: history chunk (~187k events) aggregated in seconds after batching.
- Decision lag: evaluates every 6 sim hours; wall-clock for 8 sim days ≪ 1 minute on a single core once history is loaded.
- Peak memory: dominated by one chunk parse (~hundreds of MB for large chunks); aggregates are tiny.
- At 10×: first bottleneck is single-process SQLite writers and full-chunk materialisation; partition by workflow_id and stream-parse NDJSON.

## 9. Reliability
- **Crash recovery:** `scripts/crash_recovery_demo.sh` kills the controller mid-run and restarts; `last_chunk` + pending approvals resume; no double-apply observed.
- **Idempotency:** proposal_id UUID; chunk cursor monotonic; apply only when status==approved and still pending in our store.
- **4xx/5xx:** retries on 429/5xx with backoff; 409 stale clears pending; 422 no_change is logged and ignored.

## 10. Audit & observability
Example (from store after a run):

```text
$ python -c "from controller.store import Store; s=Store('controller_state.db');
[print(d['ts'], d['kind'], d['detail'].get('change_summary') or d['detail'].get('purpose')) for d in s.query_decisions('nw_renewal', 10)]"
```

Would answer: proposal_submitted (experiment) → approval_decided → applied, with approval_id and purpose.

Alerts we would page: hallucination rate breach, caller-ID collapse, approval apply failures, credit exhaustion. We would not page on every exploration proposal.

## 11. Results

### public_01_dev
| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | gap captured |
|---|---|---|---|---|---|---|---|
| ks_cancel | 0.353 | +3.4% | 0.240 | +0.3% | 0.517 | +3.3% | 11% |
| nw_kyc | 0.383 | +1.8% | 0.240 | -0.8% | 0.621 | +1.5% | 31% |
| nw_renewal | 0.433 | +2.9% | 0.261 | -0.5% | 0.565 | +3.3% | 21% |
| **ALL** | **0.386** | **+2.9%** | 0.247 | -0.2% | 0.554 | +2.9% | 20% |

5 applied — beneficial 4, neutral 1. Credits 0.

### public_02_incidents
| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | gap captured |
|---|---|---|---|---|---|---|---|
| ks_cancel | 0.248 | +1.8% | 0.184 | +1.0% | 0.476 | +0.9% | 5% |
| nw_kyc | 0.379 | -1.2% | 0.246 | -0.8% | 0.587 | -2.1% | -24% |
| nw_renewal | 0.411 | -4.5% | 0.261 | -0.7% | 0.529 | -3.3% | -17% |
| **ALL** | **0.330** | **-1.6%** | 0.223 | -0.1% | 0.521 | -1.7% | -7% |

6 applied — beneficial 3, harmful 3. DID-flag share stayed high; some promotions after supervisor edits were net harmful. Credits 0.

**Interpretation:** Happy-path learning works. Incident latency and non-stationarity after supervisor edits need sliding-window stats and explicit rollback.

## 12. Trade-offs & rejected alternatives
| Option | Why considered | Why rejected / what instead |
|---|---|---|
| Full multi-armed bandit (Thompson) | Principled exploration | Harder to audit; Wilson + soft promote is explainable |
| Per-call streaming features store | Rich cohorts | Memory at load scale; aggregate first |
| Always auto-apply allocations | Speed | Violates governance on most workflows |
| LLM decides winner | Fast | Forbidden by constraints |
| In-memory only state | Simplicity | Fails crash-recovery requirement |

## 13. Known weaknesses
1. Cumulative (not windowed) rates slow DID-flag detection.
2. No explicit decay detector / rollback proposal.
3. Guardrail KPI is observed, not enforced as a hard gate.
4. Cohort rules are not authored; we only use simulator-assigned cohort_id.
5. public_02 net negative; incident path under-tested.
6. public_03 load numbers not captured in this pass.
7. Impact scores are heuristic, not counterfactual.

## 14. If this went to production at Formi next month
1. Sliding-window stats + mandatory caller-ID rotation playbook.
2. Guardrail veto on promotions.
3. EWMA decay → automatic rollback proposal.
4. Postgres-backed audit store + thin supervisor read API.
5. Per-workflow credit budgets and approver-hour aware scheduling.
6. Load-test public_03, publish ingest/decision lag SLOs.
7. Shadow mode on one real tenant before any auto-apply of allocations.
