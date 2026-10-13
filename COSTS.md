# Monthly cost model: 1,800 complete missions (60/day, 30 days)

Prices checked 2026-10-13 on the official pages listed at the end. Gross = before free tiers; net = after the free tiers quoted on those pages, before any account credit. Workload numbers are assumptions (see `cost/model.py`), not measurements.

## Gemini 3.8 Flash, introductory price through 2026-12-31

| Line | light gross | light net | representative gross | representative net | heavy gross | heavy net |
|---|---:|---:|---:|---:|---:|---:|
| Gemini (agent + reviewer) | $23.53 | $23.53 | $81.97 | $81.97 | $462.37 | $462.37 |
| Sessions + Memory Bank operations | $0.00 | $0.00 | $0.00 | $0.00 | $0.01 | $0.00 |
| Sessions/Memory storage | $0.00 | $0.00 | $0.01 | $0.01 | $0.03 | $0.03 |
| Memory Bank embeddings | $0.00 | $0.00 | $0.01 | $0.01 | $0.04 | $0.04 |
| Firestore | $0.07 | $0.00 | $0.16 | $0.00 | $0.46 | $0.00 |
| BigQuery (incl. 1/min intake polling) | $2.69 | $0.00 | $2.82 | $0.00 | $3.13 | $0.00 |
| Cloud Tasks | $0.00 | $0.00 | $0.00 | $0.00 | $0.01 | $0.00 |
| Cloud Run (1 vCPU, 1 GiB, request-billed) | $1.00 | $0.00 | $2.47 | $0.00 | $9.31 | $4.10 |
| **Total, Google list price** | $27.31 | $23.54 | $87.46 | $81.99 | $475.37 | $466.55 |

## Gemini 3.8 Flash, standard price from 2027-01-01

| Line | light gross | light net | representative gross | representative net | heavy gross | heavy net |
|---|---:|---:|---:|---:|---:|---:|
| Gemini (agent + reviewer) | $47.06 | $47.06 | $163.94 | $163.94 | $924.74 | $924.74 |
| Sessions + Memory Bank operations | $0.00 | $0.00 | $0.00 | $0.00 | $0.01 | $0.00 |
| Sessions/Memory storage | $0.00 | $0.00 | $0.01 | $0.01 | $0.03 | $0.03 |
| Memory Bank embeddings | $0.00 | $0.00 | $0.01 | $0.01 | $0.04 | $0.04 |
| Firestore | $0.07 | $0.00 | $0.16 | $0.00 | $0.46 | $0.00 |
| BigQuery (incl. 1/min intake polling) | $2.69 | $0.00 | $2.82 | $0.00 | $3.13 | $0.00 |
| Cloud Tasks | $0.00 | $0.00 | $0.00 | $0.00 | $0.01 | $0.00 |
| Cloud Run (1 vCPU, 1 GiB, request-billed) | $1.00 | $0.00 | $2.47 | $0.00 | $9.31 | $4.10 |
| **Total, Google list price** | $50.84 | $47.07 | $169.43 | $163.96 | $937.74 | $928.92 |

## Sensitivity: no prompt caching

The profiles assume 40 to 50% of agent input is cached. The live runs recorded **zero** cached tokens. With no caching, the Gemini line becomes (introductory price): light $33.10, representative $119.70, heavy $629.17.

## Not in these totals (shown so they are not mistaken for zero)

- Firestore reads made by the intake itself: about 120 mission records checked every minute is 5,184,000 reads a month, about $1.56 gross before the free tier; the per-mission Firestore line does not include them.
- Cloud Scheduler jobs (intake, sweep, wake reconciliation) and long-term storage of traces and reports.
- The introductory Gemini price is a promotion applied as 50% credits back on net spend; the pre-credit invoice line is about double.
- Voice and telephony minutes, SMS, social network and messaging provider fees, email delivery provider.
- Human review time for NEEDS_REVIEW and NEEDS_RECONCILIATION missions.
- Deploying the worker to Agent Runtime instead of Cloud Run (not the tested topology).
- Logging beyond the 50 GiB/project/month free allotment, egress, support plan, taxes, credits.

## Assumptions per complete mission

| Profile | segments | agent_calls | agent_in | cached | agent_out | reviewer_calls | reviewer_in | reviewer_out | retry | memory_ops | fs_reads | fs_writes | tasks_ops | run_s | session_kb |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| light | 1 | 5 | 3000 | 0.5 | 250 | 1 | 1500 | 120 | 1.05 | 0.2 | 40 | 30 | 2 | 20 | 30 |
| representative | 2 | 9 | 6000 | 0.5 | 400 | 2 | 1800 | 150 | 1.15 | 0.5 | 80 | 60 | 4 | 45 | 80 |
| heavy | 4 | 22 | 12000 | 0.4 | 700 | 5 | 2500 | 200 | 1.3 | 1.5 | 200 | 150 | 10 | 150 | 300 |

`agent_in` is tokens per agent call including history; `retry` multiplies everything for re-runs; `run_s` is active request seconds, dominated by waiting on the model in live mode.

## Notes

- Embedding price: the pricing page quotes Gemini Embedding per 1,000 "count" without naming the unit or the model id; the line above assumes 1,000 tokens. It stays under one cent a month at these volumes either way.
- Gemini 3 models other than 3.1 Pro Preview cost 10% more on regional endpoints than on Global; the table uses Global.
- Sessions and Memory Bank are billed as Agent Compute since 2026-09-01: 1 vCPU-hour ($0.085) per 1M writes or 3M reads, first 50 vCPU-hours per month free per account; storage $0.30/GiB-month.

## Sources

- gemini: https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing
- agent_platform: https://cloud.google.com/products/gemini-enterprise-agent-platform/pricing
- firestore: https://cloud.google.com/firestore/pricing
- bigquery: https://cloud.google.com/bigquery/pricing
- tasks: https://cloud.google.com/tasks/pricing
- run: https://cloud.google.com/run/pricing
