"""Monthly cost model for COMPLETE missions (all segments, replies, reviewer
calls, retries), at 60 missions/day = 1,800 per 30-day month.

List prices checked on 2026-10-13 on the official pages in SOURCES; gross
figures are before free tiers and credits, net figures apply only the free
tiers quoted on those pages. Workload numbers are assumptions, stated per
profile, to be replaced by measurements from a real pilot.

Run: python cost/model.py > COSTS.md
"""

from __future__ import annotations

MISSIONS_PER_MONTH = 60 * 30
CHECKED = "2026-10-13"

SOURCES = {
    "gemini": "https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing",
    "agent_platform": "https://cloud.google.com/products/gemini-enterprise-agent-platform/pricing",
    "firestore": "https://cloud.google.com/firestore/pricing",
    "bigquery": "https://cloud.google.com/bigquery/pricing",
    "tasks": "https://cloud.google.com/tasks/pricing",
    "run": "https://cloud.google.com/run/pricing",
}

# USD. Gemini 3.8 Flash, Global endpoint, <=200k context.
PRICES = {
    "flash_intro": {"in": 0.75, "cached": 0.075, "out": 3.75,
                    "label": "Gemini 3.8 Flash, introductory price through 2026-12-31"},
    "flash_2027": {"in": 1.50, "cached": 0.15, "out": 7.50,
                   "label": "Gemini 3.8 Flash, standard price from 2027-01-01"},
    "agent_compute_vcpu_h": 0.085,       # 1 vCPU-h per 1M writes or per 3M reads
    "agent_compute_free_vcpu_h": 50,
    "agent_storage_gib_month": 0.30,
    "embedding_per_1k": 0.00015,         # Gemini Embedding, per 1,000 "count" (unit not confirmed: see notes)
    "fs_read_100k": 0.03, "fs_write_100k": 0.09, "fs_free_reads_day": 50_000, "fs_free_writes_day": 20_000,
    "bq_tib": 6.25, "bq_free_tib": 1.0, "bq_min_mb": 10,
    "tasks_per_m": 0.40, "tasks_free_m": 1.0,
    "run_vcpu_s": 0.000024, "run_gib_s": 0.0000025, "run_req_m": 0.40,
    "run_free_vcpu_s": 180_000, "run_free_gib_s": 360_000, "run_free_req_m": 2.0,
}

# Per complete mission. Every number here is an assumption to measure.
PROFILES = {
    "light": dict(segments=1, agent_calls=5, agent_in=3_000, cached=0.5, agent_out=250,
                  reviewer_calls=1, reviewer_in=1_500, reviewer_out=120, retry=1.05,
                  memory_ops=0.2, fs_reads=40, fs_writes=30, tasks_ops=2, run_s=20, session_kb=30),
    "representative": dict(segments=2, agent_calls=9, agent_in=6_000, cached=0.5, agent_out=400,
                           reviewer_calls=2, reviewer_in=1_800, reviewer_out=150, retry=1.15,
                           memory_ops=0.5, fs_reads=80, fs_writes=60, tasks_ops=4, run_s=45, session_kb=80),
    "heavy": dict(segments=4, agent_calls=22, agent_in=12_000, cached=0.4, agent_out=700,
                  reviewer_calls=5, reviewer_in=2_500, reviewer_out=200, retry=1.3,
                  memory_ops=1.5, fs_reads=200, fs_writes=150, tasks_ops=10, run_s=150, session_kb=300),
}
BQ_POLLS_PER_DAY = 1_440  # intake query once a minute, 10 MB minimum each


def monthly(p: dict, price: dict) -> dict:
    n = MISSIONS_PER_MONTH * p["retry"]
    a_in = p["agent_calls"] * p["agent_in"] * n
    r_in = p["reviewer_calls"] * p["reviewer_in"] * n
    out = (p["agent_calls"] * p["agent_out"] + p["reviewer_calls"] * p["reviewer_out"]) * n
    cached = a_in * p["cached"]
    gemini = ((a_in - cached + r_in) * price["in"] + cached * price["cached"] + out * price["out"]) / 1e6

    writes = (p["agent_calls"] * 2 + 3) * n
    reads = p["segments"] * 3 * n
    vcpu_h = writes / 1e6 + reads / 3e6
    sessions_gross = vcpu_h * PRICES["agent_compute_vcpu_h"]
    sessions_net = max(0.0, vcpu_h - PRICES["agent_compute_free_vcpu_h"]) * PRICES["agent_compute_vcpu_h"]
    storage = p["session_kb"] * n / 1024 / 1024 * (5 / 30) * PRICES["agent_storage_gib_month"]
    embed = p["memory_ops"] * n * 2 * 40 / 1000 * PRICES["embedding_per_1k"]

    fs_r, fs_w = p["fs_reads"] * n, p["fs_writes"] * n
    fs_gross = fs_r / 1e5 * PRICES["fs_read_100k"] + fs_w / 1e5 * PRICES["fs_write_100k"]
    fs_net = (max(0, fs_r - PRICES["fs_free_reads_day"] * 30) / 1e5 * PRICES["fs_read_100k"]
              + max(0, fs_w - PRICES["fs_free_writes_day"] * 30) / 1e5 * PRICES["fs_write_100k"])

    bq_tib = (BQ_POLLS_PER_DAY * 30 + p["segments"] * n) * PRICES["bq_min_mb"] / 1024 / 1024
    bq_gross = bq_tib * PRICES["bq_tib"]
    bq_net = max(0.0, bq_tib - PRICES["bq_free_tib"]) * PRICES["bq_tib"]

    t_m = p["tasks_ops"] * n / 1e6
    tasks_gross, tasks_net = t_m * PRICES["tasks_per_m"], max(0.0, t_m - PRICES["tasks_free_m"]) * PRICES["tasks_per_m"]

    vcpu_s = p["run_s"] * n
    run_gross = vcpu_s * PRICES["run_vcpu_s"] + vcpu_s * PRICES["run_gib_s"] + p["segments"] * 3 * n / 1e6 * PRICES["run_req_m"]
    run_net = (max(0, vcpu_s - PRICES["run_free_vcpu_s"]) * PRICES["run_vcpu_s"]
               + max(0, vcpu_s - PRICES["run_free_gib_s"]) * PRICES["run_gib_s"])

    rows = {
        "Gemini (agent + reviewer)": (gemini, gemini),
        "Sessions + Memory Bank operations": (sessions_gross, sessions_net),
        "Sessions/Memory storage": (storage, storage),
        "Memory Bank embeddings": (embed, embed),
        "Firestore": (fs_gross, fs_net),
        "BigQuery (incl. 1/min intake polling)": (bq_gross, bq_net),
        "Cloud Tasks": (tasks_gross, tasks_net),
        "Cloud Run (1 vCPU, 1 GiB, request-billed)": (run_gross, run_net),
    }
    rows["Total, Google list price"] = (sum(v[0] for v in rows.values()), sum(v[1] for v in rows.values()))
    return rows


def render() -> str:
    out = [f"# Monthly cost model: {MISSIONS_PER_MONTH:,} complete missions (60/day, 30 days)", "",
           f"Prices checked {CHECKED} on the official pages listed at the end. Gross = before free tiers; "
           "net = after the free tiers quoted on those pages, before any account credit. "
           "Workload numbers are assumptions (see `cost/model.py`), not measurements.", ""]
    for key in ("flash_intro", "flash_2027"):
        price = PRICES[key]
        out += [f"## {price['label']}", "", "| Line | " + " | ".join(f"{p} gross | {p} net" for p in PROFILES) + " |",
                "|---|" + "---:|" * (2 * len(PROFILES))]
        tables = {name: monthly(p, price) for name, p in PROFILES.items()}
        for line in next(iter(tables.values())):
            cells = []
            for name in PROFILES:
                g, nt = tables[name][line]
                cells += [f"${g:,.2f}", f"${nt:,.2f}"]
            label = f"**{line}**" if line.startswith("Total") else line
            out.append(f"| {label} | " + " | ".join(cells) + " |")
        out.append("")
    out += ["## Not in these totals (shown so they are not mistaken for zero)", "",
            "- Voice and telephony minutes, SMS, social network and messaging provider fees, email delivery provider.",
            "- Human review time for NEEDS_REVIEW and NEEDS_RECONCILIATION missions.",
            "- Deploying the worker to Agent Runtime instead of Cloud Run (not the tested topology).",
            "- Logging beyond the 50 GiB/project/month free allotment, egress, support plan, taxes, credits.", "",
            "## Assumptions per complete mission", "", "| Profile | " + " | ".join(next(iter(PROFILES.values()))) + " |",
            "|---|" + "---:|" * len(next(iter(PROFILES.values())))]
    for name, p in PROFILES.items():
        out.append(f"| {name} | " + " | ".join(str(v) for v in p.values()) + " |")
    out += ["", "`agent_in` is tokens per agent call including history; `retry` multiplies everything for "
            "re-runs; `run_s` is active request seconds, dominated by waiting on the model in live mode.", "",
            "## Notes", "",
            "- Embedding price: the pricing page quotes Gemini Embedding per 1,000 \"count\" without naming the unit "
            "or the model id; the line above assumes 1,000 tokens. It stays under one cent a month at these volumes either way.",
            "- Gemini 3 models other than 3.1 Pro Preview cost 10% more on regional endpoints than on Global; the table uses Global.",
            "- Sessions and Memory Bank are billed as Agent Compute since 2026-09-01: 1 vCPU-hour ($0.085) per 1M writes "
            "or 3M reads, first 50 vCPU-hours per month free per account; storage $0.30/GiB-month.", "",
            "## Sources", ""] + [f"- {k}: {v}" for k, v in SOURCES.items()]
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    print(render(), end="")
