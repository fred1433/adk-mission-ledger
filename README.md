# ADK missions that survive worker restarts

> Reference implementation tested on Google Cloud with synthetic data. Not deployed for a client.
> External delivery uses test adapters (no real recipient exists). The 48-hour reply interval is simulated.
> Unless a line says "live", model turns and reviewer verdicts come from hand-written fixtures, not from Gemini.

A services business runs agents that do outreach, content and follow-ups. Each business need arrives as a
row in BigQuery (a prompt plus a persona and the skills it needs). A worker equips an empty ADK agent for
that one mission, lets it act through a gate, parks it while a contact takes two days to answer, throws the
agent away, and resumes the same workflow later in a different process. This repository is the small,
tested core of that loop: the boundaries where such systems usually go wrong.

- **One contact, one owner.** A contact reservation belongs to the mission, survives the worker, and blocks
  every competing mission until the mission closes. A separate short lease with a fencing generation decides
  which process may act.
- **No blind resend.** The agent can only propose. A privileged dispatcher sends what the gate approved,
  once; after a crash it retries with the same key, or stops for reconciliation if the provider cannot
  deduplicate.
- **Resume, not replay.** The paused ADK invocation is resumed by id, from a pinned manifest of code,
  persona, skills and model, by a process that shares nothing with the one that parked it.
- **Memory only after approval.** Candidates are quarantined; only approved facts are written, with
  `memories.create`, by an identity the worker cannot use.

## One mission, end to end (recorded 2026-10-13, commit `a3b5c67`)

Cloud Run worker, Cloud Tasks, Firestore, BigQuery, Agent Platform Sessions; fixture model.
Full record: [`evidence/s1_mission_resume.json`](evidence/s1_mission_resume.json).

| UTC | Process | Event |
|---|---|---|
| 14:24:46 | `ledger-worker-00020-nhm` / instance `...6f3be3` | lease generation 1; contact reserved for mission `m-...-a` |
| 14:24:57 | same | manifest pinned: code `a3b5c67`, persona and skill hashes, model `fixture:consultation_follow_up.json@e6bf6e55a2de`, session `.../reasoningEngines/3521753610830479360/sessions/6925776636159721472` (TTL 5 days) |
| 14:24:59 | same | agent equipped (`propose_action`, `wait_for_reply`, `propose_memory`, skills); `load_skill`, `propose_action` |
| 14:25:00 | same | gate: 8 deterministic checks pass, reviewer APPROVE; agent calls `wait_for_reply`; invocation pauses; agent discarded |
| 14:25:01 | same | dispatcher: provider accepts `sbx-47a159c7536f`; mission WAITING_REPLY; lease released, reservation kept |
| after parking | Cloud Run (same service) | competing mission `m-...-b`, same contact through its phone alias: BLOCKED_CONTACT_RESERVED |
| 14:25:33 | `gcloud run services update` | new revision `ledger-worker-00021-9c9`; process A's revision retired |
| 14:25:34 | `ledger-worker-00021-9c9` / `...b1650b` | signed webhook records the reply (declared received 2026-10-15 14:25, i.e. +48 h, simulated); wake task enqueued |
| 14:25:35 | same | lease generation 2; pinned manifest verified, no mismatch; ADK resumes invocation `e-b5b793d0...` with the reply as the response to the pending `wait_for_reply` call |
| 14:26:02 | same | the same reply delivered again: recorded as duplicate |
| 14:26:05 | same | two memory candidates quarantined; `confirm_after_reply` proposed, approved, sent once (`sbx-58ee8c7c8fd9`) |
| 14:26:08 | same | mission DONE; contact released; report row appended to BigQuery |
| 14:26:10 | same | an extra wake-up task for the same event: no-op (generation 3) |

Provider-side result: exactly two messages for the mission. Agent Platform Sessions holds 17 events for
the session, expiring 2026-10-18.

## The race that breaks, then the fix

BigQuery is the mission ledger, so it is tempting to lock contacts there with an insert-only `MERGE`. The
BigQuery documentation says why that fails: "A MERGE DML statement does not conflict with other concurrently
running DML statements as long as the statement only inserts rows and does not delete or update any existing
rows." The MERGE is atomic; the problem is that two insert-only MERGEs do not see each other.

[`evidence/s6_bigquery_merge_race.json`](evidence/s6_bigquery_merge_race.json): 20 concurrent insert-only
MERGEs keyed on one contact left **2 rows** for that contact (two "winners"), with every job id recorded.
Earlier development runs the same day left 2 or 3 rows. The same 20-way race on Firestore's transactional
reservation produced exactly **1** winner. The scenario records what happens; it does not require a duplicate.

## Scenarios

All on 2026-10-13 against the throwaway project `mission-ledger-1013-13546` (us-central1).

| # | What it shows | Where it ran | Model | Result | Evidence |
|---|---|---|---|---|---|
| 1 | Park, worker restart, duplicate reply, resume once, one competing mission refused | Cloud Run (2 revisions) + Cloud Tasks + Firestore + BigQuery + Sessions | fixture | 11/11 pass | `s1_mission_resume.json` |
| 2 | 20 concurrent missions on one contact via two aliases: one owner; refusal persists while the owner is parked; stale generation cannot commit | local process -> Firestore | none | 5/5 pass | `s2_contact_reservation.json` |
| 3 | Provider accepts, process killed before receipt: idempotent adapter retries with the same key (1 message); plain adapter goes to NEEDS_RECONCILIATION (1 message, no resend) | two local OS processes -> Firestore | none | 7/7 pass | `s3_send_crash.json` |
| 4 | Over-promise with a "skip review" note: reviewer REJECT enforced; direct dispatch refused; text changed after approval blocked; price figure rejected before the reviewer | local process -> Firestore, BigQuery, Sessions | fixture | 10/10 pass | `s4_gate.json` |
| 5 | Quarantine, validation, create-only publication; worker identity lacks memory write and cannot impersonate the publisher | local + Cloud Run identity check; FakeMemoryBank | fixture | 8/8 pass | `s5_memory.json` |
| 5 live | Same, against Agent Platform Memory Bank: approved fact read back unchanged, retrieved by similarity search, rejected text absent from the full listing; worker reads, and its `memories.create` gets **403 PERMISSION_DENIED** from the platform | publisher impersonated locally; probe inside Cloud Run | live embeddings (`gemini-embedding-001`) | 10/10 pass | `s5_memory_live.json` |
| 1 live | Scenario 1 with Gemini 3.8 Flash as agent and reviewer | Cloud Run | **live** | 8/11; see below | `s1_mission_resume_live.json` |
| 6 | Insert-only MERGE race vs Firestore transaction | local process -> BigQuery, Firestore | none | observed 2 rows vs 1 winner | `s6_bigquery_merge_race.json` |

Offline: `pytest tests` (16 tests, no network).

### The live run, attempt by attempt

Bounded run: at most 12 model calls per process, output capped, temperature 0, one mission per attempt.
Gemini 3.8 Flash on the Global endpoint for the agent and the reviewer. Measured from the session usage
metadata: 16 agent calls, 27,085 input and 4,095 output tokens (thinking included) over three attempts,
about **$0.04 at list price** with the two reviewer calls and the embeddings; estimated from recorded
usage, before the invoice.

1. The agent spent its output budget thinking and stopped on `MAX_TOKENS` before proposing anything. The
   worker then closed the mission as DONE: a bug. Fixed: a segment that ends without a sent action, or on a
   model error, now stops for review. Live mode sets a low thinking level.
2. The agent proposed the first message correctly; the live reviewer then failed because the image did not
   contain `policies/`. The segment crashed, Cloud Tasks retried, and the retry correctly did nothing. Fixed:
   the image copies `policies/`; a test checks it; an interrupted segment now goes to review.
3. ([`evidence/s1_mission_resume_live.json`](evidence/s1_mission_resume_live.json)) Gemini wrote the first
   message itself, the live reviewer approved it, it was sent once, the mission parked, the competing
   mission was refused, the duplicate reply was ignored, and a different revision resumed the invocation.
   Gemini then proposed "Thursday at 15:00 is confirmed", and the live reviewer **rejected** it: it had been
   given the mission facts but not the contact's reply, so the confirmation looked unsupported. The agent
   proposed again under the same purpose, hit the same action id (no duplicate), and handed over. Mission
   NEEDS_REVIEW, contact still reserved, one message sent. Safe, but wrong for the business. Fixed after the
   run: on resume, the reviewer now also receives the verified inbound reply. That fix is covered by the
   fixture runs only; it has not been re-run live.

## Topology (as tested)

```
BigQuery missions ──(intake)──> Cloud Tasks ──OIDC──> Cloud Run worker (ADK, Python)
                                                         │  lease + fencing, reservations, actions,
                                                         │  inbound events, trace  ──> Firestore
                                                         │  sessions ──> Agent Platform Sessions
                                                         │             (Agent Runtime instance, no code deployed)
                                                         │  reports ──> BigQuery mission_reports
signed webhook /inbound ──> Firestore event (dedup) ──> Cloud Tasks wake ──> /tasks/resume
memory-publisher (separate identity) ──memories.create──> Agent Platform Memory Bank
```

The worker runs on Cloud Run. It is not deployed to Agent Runtime; the Agent Runtime instance only hosts
Sessions and Memory Bank. Firestore decides who may act; BigQuery never does.

## What this is, and what it is not

- It is the control layer: reservations, leases, gate, dispatcher, resume, memory publication, with recorded
  failures. It is not a full platform: no real email, voice, social or CMS integration, no dashboard.
- It has not run in production for anyone. All contacts are synthetic (`example.com`, fictional numbers).
- In scenarios 2 to 4 and 6 the "workers" are local processes against real Google services.
- The dispatcher runs inside the worker process here; in production it would run under its own identity.
- The webhook secret is an environment variable here; Secret Manager in production.

## Layout

```
ledger/        worker: coordination.py (leases, reservations), actions.py (proposal, dispatch),
               gate.py, runtime.py (segments), agent_factory.py, manifest.py, memory.py, inbound.py
skills/        versioned skills loaded with ADK SkillToolset (purposes and channels declared in metadata)
personas/      persona instructions
policies/      reviewer rubric (not visible to the agent)
fixtures/      hand-written model turns and reviewer verdicts
scenarios/     the recorded scenarios; evidence/ holds their output
infra/         setup.sh, deploy.sh, BigQuery DDL, custom IAM roles
cost/          monthly cost model -> COSTS.md
```

Pinned: `google-adk==2.11.0`, `google-cloud-aiplatform==1.165.1`, `google-cloud-firestore==2.34.1`,
`google-cloud-bigquery==3.46.1`, `google-cloud-tasks==2.26.0`, `google-genai==2.30.0`, Python 3.12
(`requirements.lock`). `ResumabilityConfig` and Skills are marked experimental in ADK 2.11; ADK 2.11 still
reaches Sessions through `vertexai.Client`, which now warns that `agentplatform.Client` replaces it.

## Run it

```
uv venv -p 3.12 .venv && uv pip install -r requirements.lock pytest
.venv/bin/python -m pytest tests                      # offline
PROJECT=... ./infra/setup.sh                          # throwaway project
PROJECT=... ENGINE_ID=... WEBHOOK_SECRET=... ./infra/deploy.sh
cd scenarios && python s1_mission_resume.py           # needs LEDGER_* env, see ledger/config.py
```

Paid model calls need both `LEDGER_MODEL_MODE=live` and `LEDGER_ALLOW_PAID_MODEL_CALLS=yes`, plus the
`ledgerModelCaller` role on the worker; none of these are set by default.

See [ARCHITECTURE.md](ARCHITECTURE.md) for decisions to validate and [COSTS.md](COSTS.md) for the monthly model.
