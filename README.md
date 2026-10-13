# ADK missions that survive worker restarts

> Reference implementation tested on Google Cloud with synthetic data. Not deployed for a client.
> External delivery uses test adapters (no real recipient exists). The 48-hour reply interval is simulated.
> Runs marked "live" used Gemini; all other runs use hand-written fixture model turns and reviewer verdicts.

A services business runs agents that do outreach, content and follow-ups. Each business need arrives as a
row in BigQuery (a prompt plus a persona and the skills it needs). A worker equips an empty ADK agent for
that one mission, lets it act through a gate, parks it while a contact takes two days to answer, throws the
agent away, and resumes the same workflow later in a different process. This repository is the small,
tested core of that loop: the boundaries where such systems usually go wrong.

- **One contact, one owner.** A contact reservation belongs to the mission, survives the worker, and blocks
  every competing mission until the mission closes. A separate short lease, renewed while the segment runs
  and carrying a fencing generation, decides which process may act.
- **No blind resend.** The agent can only propose. A privileged dispatcher sends what the gate approved,
  once; after a crash it retries with the same key, or stops for reconciliation if the provider cannot
  deduplicate. A late provider receipt is kept, never dropped.
- **Resume, not replay.** The paused ADK invocation is resumed by id, from a pinned manifest of code,
  persona, skills and model, by a process that shares nothing with the one that parked it.
- **Nothing stays RUNNING.** A failing segment closes its mission for review itself; a scheduled sweep
  catches workers that died.
- **Memory only after approval.** Candidates are quarantined; only approved facts are written, with
  `memories.create`, by an identity the worker cannot use.

## One mission, end to end, live (2026-10-13, commit `cd959e0`)

Gemini 3.8 Flash as agent and as reviewer. Cloud Scheduler and Cloud Tasks, a Cloud Run worker,
Firestore, BigQuery, Agent Platform Sessions. Full record:
[`evidence/s1_mission_resume_live.json`](evidence/s1_mission_resume_live.json).
**A** = revision `ledger-worker-00033-2j4`, **B** = revision `ledger-worker-00034-j7m` (different instance).

| UTC | Proc | Event |
|---|---|---|
| 15:11:09 | intake | `/tasks/intake` reads `missions` in BigQuery, enqueues `start-m-10131511-b528-a` |
| 15:11:11 | A | lease generation 1; contact reserved for the mission |
| 15:11:27 | A | manifest pinned: code `cd959e0`, persona and skill hashes, model `gemini-3.8-flash`, session `7699973559602380800` (TTL 5 days) |
| 15:11:31 | A | Gemini: `list_skills`, `load_skill` x2, then `propose_action` |
| 15:11:41 | A | gate: 8 deterministic checks pass; live reviewer APPROVE |
| 15:11:44 | A | Gemini calls `wait_for_reply`; invocation pauses; agent discarded; provider accepts `sbx-2b26563195f4`; mission WAITING_REPLY, lease released, reservation kept |
| 15:11:49 | intake | a competing mission for the same contact, through its phone alias, is enqueued; it is refused: BLOCKED_CONTACT_RESERVED |
| 15:12:16 | deploy | new revision rolled out; A's revision retired |
| 15:12:27 | B | signed webhook records the reply (declared received 2026-10-15 15:11, +48 h, simulated); wake task enqueued |
| 15:12:29 | B | lease generation 2; manifest verified; ADK resumes invocation `e-2060d297...` with the reply as the response to the pending call |
| 15:12:50 | B | the same reply delivered again: duplicate, ignored |
| 15:12:56 | B | Gemini proposes "We have confirmed your consultation for Thursday at 15:00"; live reviewer, now given the reply, APPROVE; sent once (`sbx-1080f99e5765`) |
| 15:13:02 | B | mission DONE; contact released; report row appended to BigQuery |
| 15:13:05 | B | an extra wake-up task for the same event: no-op (generation 3) |

11 of 11 assertions pass. The provider holds exactly two messages for the mission; Sessions holds 15
events. The fixture run of the same scenario on the same commit is
[`evidence/s1_mission_resume.json`](evidence/s1_mission_resume.json) (11/11).

## The race that breaks, then the fix

BigQuery is the mission ledger, so it is tempting to lock contacts there with an insert-only `MERGE`.
The documentation says why that fails: "A MERGE DML statement does not conflict with other concurrently
running DML statements as long as the statement only inserts rows and does not delete or update any
existing rows." The MERGE is atomic; the problem is that insert-only MERGEs do not see each other.

[`evidence/s6_bigquery_merge_race.json`](evidence/s6_bigquery_merge_race.json): 20 insert-only MERGEs
keyed on one contact were submitted at once. BigQuery does not run them all together (it "runs up to 2 of
them concurrently, after which up to 20 are queued as PENDING"), and the two statements that inserted ran at
the same time. Result: **2 rows** for one contact, every job id recorded. The same race against Firestore's
transactional reservation produced exactly **1** winner. The scenario records what happens; it does not
require a duplicate.

## Scenarios

All on 2026-10-13 in the throwaway project `mission-ledger-1013-13546` (us-central1), commit `cd959e0`
unless stated.

Click a number for its evidence. Unmarked rows use fixture model turns or no model at all.

| # | What it shows *(where it ran)* | Result |
|---|---|---|
| [1](evidence/s1_mission_resume.json) | BigQuery intake, park, worker restart, duplicate reply, one resume, competing mission refused *(Cloud Run on 2 revisions, Scheduler, Tasks, Firestore, BigQuery, Sessions)* | 11/11 |
| [1 live](evidence/s1_mission_resume_live.json) | The same with Gemini as agent and reviewer | 11/11 |
| [2](evidence/s2_contact_reservation.json) | 20 concurrent missions on one contact via two aliases: one owner; refusal persists while parked; stale generation cannot write *(local process, Firestore)* | 5/5 |
| [3](evidence/s3_send_crash.json) | Crash after provider acceptance (idempotent: same key, 1 message; plain: reconciliation, 1 message); slow worker's late receipt kept, not reported as SENT; draft replay *(2 to 3 local OS processes, Firestore)* | 11/11 |
| [4](evidence/s4_gate.json) | Over-promise with a "skip review" note rejected; direct dispatch refused; edit after approval blocked; price rejected before the reviewer; revised text and later turns get new ids *(local process, Firestore, BigQuery, Sessions)* | 13/13 |
| [5](evidence/s5_memory.json) | Create-only publication; forged input refused; crash after create reconciled without duplicate; contradictory facts: one approved, one held; worker cannot write memories or impersonate the publisher *(local, FakeMemoryBank, identity check in Cloud Run)* | 11/11 |
| [5 live](evidence/s5_memory_live.json) | Memory Bank with live embeddings: fact read back unchanged and retrieved by similarity search; rejected text absent from the full listing; the worker's `memories.create` gets **403** from the platform *(commit `2e406f9`, earlier publisher code)* | 10/10 |
| [6](evidence/s6_bigquery_merge_race.json) | Insert-only MERGE race vs Firestore transaction *(local process, BigQuery, Firestore)* | 2 rows vs 1 winner |
| [7](evidence/s7_interrupted.json) | Model failure closes the mission for review; dead worker swept after its lease expires; live lease untouched *(local, sweep on Cloud Run)* | 3/3 |

Offline: `pytest tests` (18 tests, no network).

### Live runs, attempt by attempt

Bounded runs: at most 12 model calls per process, output capped, low thinking level, temperature 0, one
mission per attempt. Measured usage per attempt: [`evidence/live_usage.json`](evidence/live_usage.json)
(agent calls from Sessions usage metadata; reviewer calls traced from the final run). Total measured:
25 calls, 40,843 input and 5,387 output tokens (thinking included), **$0.051 at list price**; two reviewer
calls in attempt 3 and the Memory Bank embeddings were not metered and add well under a cent. Estimates
from recorded usage, before the invoice.

1. The agent spent its output budget thinking and stopped on `MAX_TOKENS` before proposing anything; the
   worker wrongly closed the mission as DONE. Fixed: no sent action or a model error means review.
2. The agent proposed the first message; the live reviewer then failed because the image lacked
   `policies/`. Fixed, with a test. The mission was left RUNNING ([evidence](evidence/live_attempts/attempt2_missing_policy_file.json)).
3. First message approved and sent once; resume on another revision; then the live reviewer **rejected** a
   correct confirmation because it saw the mission facts but not the contact's reply. Mission stopped for a
   human with one message sent ([evidence](evidence/live_attempts/attempt3_reviewer_without_reply.json)).
   Fixed: on resume the reviewer receives the verified inbound reply.
4. The first model call got 403: the model-caller role had been granted seconds before and had not
   propagated. No tokens. The exception left the mission RUNNING, and Cloud Tasks spent its 5 retries on
   409 before the 120 s lease expired ([evidence](evidence/live_attempts/attempt4_model_role_not_propagated.json)).
   Fixed: segments catch failures and close for review; a scheduled sweep handles dead workers; the queue
   now retries for about two hours. The missions of attempts 2 and 4 were later sent to review by the
   interrupted-segment rule.
5. Final code, role granted 11 minutes before and probed from inside the worker
   (`/diag/model-permission`): **11/11**, shown above.

When the BigQuery intake first ran, it also started three competing-mission rows that earlier failed
attempts had left unstarted. Because a document-id encoding change had just made their contact aliases
unreadable, they ran against provisional contacts and were not blocked (sandbox sends only). The lookup now
falls back to the old encoding; a real deployment would migrate the records.

## Topology (as tested)

```
Scheduler (1 min) -> /tasks/intake : BigQuery missions -> Cloud Tasks start-<id>
Cloud Tasks (OIDC) -> Cloud Run worker (ADK, Python)
   Firestore : leases, reservations, actions, inbound events, trace
   Agent Platform Sessions (Agent Runtime instance, no code deployed)
   BigQuery  : mission_reports (append only)
Signed webhook /inbound -> Firestore event (dedup) -> Cloud Tasks wake -> /tasks/resume
Scheduler (2 min) -> /tasks/sweep : expired RUNNING missions -> NEEDS_REVIEW
memory-publisher identity -> memories.create -> Agent Platform Memory Bank
```

The worker runs on Cloud Run; it is not deployed to Agent Runtime, whose instance only hosts Sessions and
Memory Bank (embedding model `gemini-embedding-001`). Firestore decides who may act; BigQuery never does.

## What this is, and what it is not

- It is the control layer: intake, reservations, leases, gate, dispatcher, resume, sweep, memory
  publication, with recorded failures. It is not a full platform: no real email, voice, social or CMS
  integration, no dashboard.
- It has not run in production for anyone. All contacts are synthetic (`example.com`, fictional numbers).
- In scenarios 2 to 4, 6 and 7a the workers are local processes against real Google services.
- Approvals (action approval, memory candidate status) are enforced by code, not by IAM: the worker's
  Firestore role could write those fields. Production would keep approvals in a database the worker cannot
  write. Memory writes to Memory Bank, by contrast, are enforced by IAM (scenario 5 live).
- The dispatcher runs inside the worker process here; in production it would run under its own identity.
  The webhook secret is an environment variable here; Secret Manager in production.

## Layout

```
ledger/     the worker: coordination (leases, reservations), actions
            (proposal, dispatch), gate, runtime (segments, sweep), intake,
            agent_factory, manifest, memory, inbound
skills/     versioned skills, loaded with ADK SkillToolset
personas/   persona instructions
policies/   reviewer rubric (not visible to the agent)
fixtures/   hand-written model turns and reviewer verdicts
scenarios/  recorded scenarios, usage_report.py; output in evidence/
infra/      setup.sh, deploy.sh (+ scheduler jobs), DDL, IAM roles
cost/       monthly cost model -> COSTS.md
```

Pinned: `google-adk==2.11.0`, `google-cloud-aiplatform==1.165.1`, `google-cloud-firestore==2.34.1`,
`google-cloud-bigquery==3.46.1`, `google-cloud-tasks==2.26.0`, `google-genai==2.30.0`, Python 3.12
(`requirements.lock`). `ResumabilityConfig` is marked experimental in ADK 2.11 (it warns at startup);
`SkillToolset` is marked stable. ADK 2.11 reaches Sessions through `vertexai.Client`, which now warns that
`agentplatform.Client` replaces it.

## Run it

```
uv venv -p 3.12 .venv && uv pip install -r requirements.lock pytest
.venv/bin/python -m pytest tests          # offline
PROJECT=... ./infra/setup.sh              # throwaway project; prints ENGINE_ID
PROJECT=... ENGINE_ID=... WEBHOOK_SECRET=... ./infra/deploy.sh
cd scenarios && python s1_mission_resume.py   # LEDGER_* env: ledger/config.py
```

Paid model calls need both `LEDGER_MODEL_MODE=live` and `LEDGER_ALLOW_PAID_MODEL_CALLS=yes`, plus the
`ledgerModelCaller` role on the worker (check it with `/diag/model-permission` first); none of these are set
by default.

See [ARCHITECTURE.md](ARCHITECTURE.md) for decisions to validate and [COSTS.md](COSTS.md) for the monthly model.
