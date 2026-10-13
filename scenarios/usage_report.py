"""Live model usage and its list-price cost, from recorded data only.

Agent calls: usage metadata stored with each model event in Agent Platform
Sessions (every live attempt). Reviewer calls: `model.usage` trace entries,
recorded only from the final run on (earlier reviewer calls are counted, not
measured). Prices: Gemini 3.8 Flash, Global endpoint, introductory list price
checked 2026-10-13 (see COSTS.md). Usage-based estimate, not an invoice.

Usage: python usage_report.py <mission_id>=<label> ...
"""

from __future__ import annotations

import asyncio
import json
import sys

from common import ROOT, db

from ledger import runtime, trace

PRICE = {"in": 0.75, "cached": 0.075, "out": 3.75}  # USD per 1M tokens


def cost(u: dict) -> float:
    uncached = u["prompt"] - u["cached"]
    return (uncached * PRICE["in"] + u["cached"] * PRICE["cached"]
            + (u["output"] + u["thoughts"]) * PRICE["out"]) / 1e6


def main() -> None:
    d = db()
    rows, total = [], {"prompt": 0, "cached": 0, "output": 0, "thoughts": 0, "calls": 0, "usd": 0.0}
    for arg in sys.argv[1:]:
        mid, label = arg.split("=", 1)
        doc = d.collection("missions").document(mid).get().to_dict()
        sess = asyncio.run(runtime.session_service().get_session(
            app_name="mission_ledger", user_id="mission-runner", session_id=doc["session_id"]))
        agent = {"prompt": 0, "cached": 0, "output": 0, "thoughts": 0, "calls": 0}
        for e in sess.events:
            u = e.usage_metadata
            if u:
                agent["calls"] += 1
                agent["prompt"] += u.prompt_token_count or 0
                agent["cached"] += u.cached_content_token_count or 0
                agent["output"] += u.candidates_token_count or 0
                agent["thoughts"] += u.thoughts_token_count or 0
        t = trace.read(d, mid)
        rev = [e for e in t if e["kind"] == "model.usage" and e.get("source") == "reviewer"]
        reviewer = {"calls": len(rev), **{k: sum(e.get(k) or 0 for e in rev)
                                         for k in ("prompt", "cached", "output", "thoughts")}}
        verdicts = sum(1 for e in t if e["kind"] == "gate.verdict" and str(e.get("reviewer_mode", "")).startswith("live"))
        row = {"mission_id": mid, "label": label, "final_status": doc.get("status"),
               "agent": {**agent, "usd": round(cost(agent), 5)},
               "reviewer_measured": {**reviewer, "usd": round(cost(reviewer), 5)},
               "live_reviewer_verdicts": verdicts,
               "reviewer_calls_not_measured": max(0, verdicts - len(rev))}
        rows.append(row)
        for part in (agent, reviewer):
            for k in ("prompt", "cached", "output", "thoughts", "calls"):
                total[k] += part[k]
            total["usd"] += cost(part)
    total["usd"] = round(total["usd"], 4)
    out = {"price_basis": "Gemini 3.8 Flash, Global, introductory list price (USD per 1M tokens)",
           "prices": PRICE, "attempts": rows, "measured_total": total,
           "not_measured": "reviewer calls before the final run (see reviewer_calls_not_measured); "
                           "Memory Bank embeddings in s5 live (2 creates, 1 retrieval, a few dozen tokens)"}
    path = ROOT / "evidence" / "live_usage.json"
    path.write_text(json.dumps(out, indent=2, default=str) + "\n")
    print(json.dumps(total), "->", path.relative_to(ROOT))


if __name__ == "__main__":
    main()
