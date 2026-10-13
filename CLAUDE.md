# Maintenance notes for coding agents

Invariants. A change that breaks one of these is wrong even if tests pass.

1. **No unvalidated memory.** Only `ledger/memory.py::MemoryPublisher.publish` writes to Memory Bank, only
   for APPROVED candidates, only with `memories.create`. Never add `generate`, IngestEvents or
   `add_session_to_memory`. The worker identity never gets a memory write permission.
2. **No send outside the gate.** The agent's tools are `propose_action`, `wait_for_reply`, `propose_memory`
   and skills. Never give it a send tool or provider credentials. `actions.dispatch` only executes APPROVED
   actions whose digest still matches.
3. **Reservation before contact.** A mission reserves its contact in Firestore before any proposal; the
   reservation is released only on closure, cancellation or business timeout, never because a process ended.
4. **Every mission write is fenced.** Writes about a mission go through `coordination.fenced` with the
   current lease. Do not write mission state with a plain `update`.
5. **BigQuery never authorises.** It is read for mission rows and written for reports only.
6. **No blind resend.** An action left DISPATCHING on an adapter without idempotency goes to
   NEEDS_RECONCILIATION.
7. **Resume against the pinned manifest.** If you change skills, personas, fixtures or the model, parked
   missions will stop for review on resume. That is intended.
8. **No mission left RUNNING.** Segment work goes through `runtime._guarded`; keep the sweep scheduled and
   the queue's retry window longer than the lease.
9. **Action ids are business ids** (mission, purpose, turn, revision). Never derive them from model output
   ids, which change when a turn is regenerated.
10. **No paid model calls by default.** Fixture mode is the default; live needs two environment switches.

Before committing: `pytest tests` (offline). Scenario scripts need a throwaway project (see README).
