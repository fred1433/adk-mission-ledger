# Reference architecture and decisions to validate

This is not a review of an existing system: no private implementation was seen. It sets out the decisions
this repository makes, what the recorded runs support, and what a real deployment would still need to
confirm.

## Has this run in production?

No. This repository is not an ADK agent in client production. It is a reference implementation, run on
Google Cloud with synthetic data, with its tested boundaries and limits written down. What broke while it
was being built is recorded in the README: a lock that BigQuery cannot provide, a live agent that ran out
of output budget and was wrongly marked done, an image missing a folder, a live reviewer that rejected
a correct confirmation because it never saw the contact's reply, a crashed segment whose task retries ran
out before its lease expired, and a document-id encoding change that orphaned earlier contact aliases. The
fifth live run, on the final code, passed end to end.

## What a 15-minute screen walkthrough would cover

1. `evidence/s1_mission_resume.json`: one mission parked by one Cloud Run revision and resumed by another,
   with the duplicate reply and the extra wake-up both ignored.
2. `evidence/s6_bigquery_merge_race.json`: the insert-only MERGE race with its job ids, then the
   Firestore transaction.
3. `evidence/s3_send_crash.json`: a process killed after the provider accepted, and the two recovery paths.
4. `evidence/live_attempts/attempt3_reviewer_without_reply.json`: the live reviewer rejecting a correct
   message, why that is the safe failure, and `evidence/s1_mission_resume_live.json`, the same flow passing
   once the reviewer sees the reply.
5. `ledger/actions.py` and `ledger/coordination.py`: under 400 lines that carry the guarantees.

## Resuming after 48 hours, and where the contact lock lives

- Nothing waits. The agent calls a long-running `wait_for_reply` that returns nothing; ADK pauses the
  invocation (`ResumabilityConfig(is_resumable=True)`); the worker records session id, invocation id and the
  pending function-call id, releases its lease and exits the segment.
- A reply reaches a signed webhook, which validates it, keys the event by provider, account and message id
  (duplicates fail on `create()`), attributes it only if the thread token maps to a mission AND the sender
  resolves to that mission's contact, then enqueues a Cloud Tasks wake-up whose name is a hash of the event
  and attempt. A crash between the two leaves a PENDING event that the scheduled `reconcile-wakes` job
  re-enqueues; a wake that outlived the queue's retries is re-armed with a new attempt. Event state only
  moves forward.
- The resumed segment takes a new lease (generation + 1), checks that the contact is still reserved for
  this mission, verifies the pinned manifest, and feeds the reply to ADK as the function response of the
  pending call. It consumes the event in the same fenced transaction that closes the segment, so a second
  wake is a no-op.
- Sessions TTL is 5 days for a 48-hour policy. The reservation's business timeout is 72 hours; a reply after
  that goes to reconciliation instead of authorising another message.
- The lock lives in Firestore, as two records with different lifetimes: the **contact reservation** (owned
  by the mission, ends on closure, cancellation or business timeout) and the **execution lease** (owned by a
  process, short, with a fencing generation checked inside every write). BigQuery stays the ledger and the
  report, and is never read to decide whether an action may run.
- Identity: aliases are normalised per channel, but only an authoritative alias directory says that two
  aliases are the same contact. No universal person matching is claimed.

## Memory Bank without unvalidated memory

- The agent has `propose_memory`, which writes a quarantined candidate to Firestore, outside the bank.
- A validator applies rules first (scope must be one contact or a named playbook; no raw contact data;
  evidence required; a playbook rule needs at least two missions), then a reviewer. Approval claims a
  (scope, topic) slot in a Firestore transaction, so a second, contradictory fact on the same slot is held
  for review, whether the two are validated in sequence or in parallel (scenario 5c).
- Validation reads the stored candidate by id and binds the approval to a digest of its fact, scope, topic,
  evidence and provenance. A separate `memory-publisher` identity writes approved facts with
  `memories.create` and the exact approved text, under a stable caller-supplied memory id: concurrent or
  repeated publishers converge on one memory, which is read back and compared with the approved payload. Never `generate` (its consolidation can update or delete existing memories), never IngestEvents,
  never `add_session_to_memory`. An approval ledger keeps provenance.
- IAM, proven live under the worker's real Cloud Run identity: it can read, and its `memories.create` is
  refused by the platform (403). The publisher has a custom role limited to `memories.create`, `get`,
  `list` (no update, delete, generate or rollback). The worker cannot impersonate the publisher.
- Absence is checked against the complete listing, with a positive control retrieved by similarity search.
- Approval status itself is protected by code, not by IAM: the worker's Firestore role could write it. The
  IAM boundary is at Memory Bank. Moving approvals to a database the worker cannot write closes that gap.
- Embeddings use `gemini-embedding-001`, which the Memory Bank setup guide lists for non-English content.
- The claim is narrow: only approved payloads enter the bank through this path. Corrections and deletions
  belong on a separate, controlled path.

## When not to use ADK

When the workflow is fixed and the only generative step is drafting text, an explicit state machine plus one
model call is simpler to test and to audit than an agent choosing its own transitions. The reservation,
lease, gate and dispatcher in this repository do not depend on ADK and would sit unchanged under either.

## Decisions to validate in a real environment

1. Provider semantics per channel: which ones honour an idempotency key (retry) and which need
   reconciliation, and who works the reconciliation queue.
2. Where the dispatcher runs: a separate service and identity holding provider credentials.
3. The reviewer's inputs: business facts, customer statements and untrusted text kept apart (customer
   statements can set a choice, never a commitment), and how often NEEDS_REVIEW is acceptable. The fixes
   made after the live run have not been exercised with a live model.
4. The alias directory: which system is authoritative for contacts.
5. Cloud Run worker with managed Sessions (tested) versus deploying the worker to Agent Runtime (not tested).
6. Opt-out handling: a reply starting with "stop" or "unsubscribe" suppresses the contact before the agent
   runs, and the gate then blocks any message to it. Which phrases and channels count needs a business rule.
7. ADK upgrade policy: `ResumabilityConfig` is experimental in 2.11 (`SkillToolset` is marked stable);
   pinned versions plus manifest checks contain the risk but do not remove it.

## Three risks

- A model reviewer is a reviewer, not a guarantee. The gate enforces its verdicts; it cannot make them
  right. Deterministic checks catch what can be stated as rules.
- Reconciliation is human work. A plain adapter that crashed mid-send produces a NEEDS_RECONCILIATION item
  that someone must close.
- Experimental APIs move. The deprecation warning on `vertexai.Client` in favour of `agentplatform.Client` is
  already visible in ADK 2.11 logs.

## Effort, by milestone

Ranges assume one engineer familiar with the stack and access to the real provider sandboxes.

| Milestone | Scope | Estimate |
|---|---|---|
| Reference demonstration | This repository | done |
| First supervised workflow | One real channel adapter with its idempotency semantics, dispatcher as its own service, Secret Manager, inbound authentication from the real provider, IAM in the target project, alerting on NEEDS_REVIEW and NEEDS_RECONCILIATION | 2 to 3 weeks |
| Production acceptance | Deploy and rollback procedure, monitoring and on-call notes, reviewer evaluation on real traffic samples, business sign-off on the review queue | 2 to 4 more weeks |

Monthly running cost at 60 missions a day: [COSTS.md](COSTS.md).
