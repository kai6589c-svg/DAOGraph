# DAOGraph changelog

_Changes to the public framework API._

---

## 📦 0.2.0 — 2026-10-05

Research-focused alpha:

- Optional sync/async adaptation and goal-verification callbacks with unchanged defaults
- `ReplanDecision`, `GoalCheck`, unfinished-task `PlanChange`, and an opt-in `incomplete` result
- Evidence-linked named `Signal` records with unknown-value support
- Reused-plan validation and per-action constraints; exact pending-action approval resume
- Compatibility verified with an actual checkpoint produced by 0.1.0
- Bounded research application with conflict review, provenance, claim and citation checks,
  recoverable read observations, and justified abstention
- Frozen 48-case offline corpus: 12 development and 36 evaluation cases, network denial,
  deterministic repetitions, visible fixed-depth candidates, JSON traces and reports
- Optional pinned HTTPS demonstration and verified offline response replay
- English documentation, examples, benchmark fixtures, and manual online CI

On the frozen evaluation suite, all three modes resolved 36/36 expected outcomes.
Selective control used 138 planner calls against 225 for always-replan (38.7% fewer),
with 105 retrieval attempts in each mode. Fixed control used 36 planner calls and
was cheaper on these controlled cases. No model-cost or production-quality claim
is made. See [the research guide](docs/research.md).

## 📦 0.1.0 — 2026-10-05

Initial alpha implementation:

- Situation-driven reconstruction of transient execution DAGs
- Stable task occurrence ids and dependency validation
- Six normalized situation signals with evidence and custom assessors
- Minimal immutable execution constraints and host approval pauses
- JSON approval checkpoints with definition and revision checks
- Synchronous and asynchronous execution, incremental event streams
- JSON state snapshots and atomic partial updates with optional reducers
- Runnable research, approval, and async tool examples
- English public documentation, package metadata, MIT license, CI

This version uses sequential task dispatch, host-trusted checkpoint storage, and
application-managed external-effect idempotency.
