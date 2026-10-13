"""Scenario 7: no mission is left RUNNING.

a. The model call fails inside a segment (scripted failure, local process,
   real Firestore, BigQuery and Sessions): the segment closes the mission as
   NEEDS_REVIEW at once, under its own lease.
b. A worker dies mid-segment (simulated: lease taken, status RUNNING, process
   gone). The sweep endpoint on Cloud Run, also run every 2 minutes by Cloud
   Scheduler, sends it to review once the lease has expired.
c. A mission whose lease is still live is left alone by the sweep.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.request

import google.auth.transport.requests
from common import Evidence, db, run_id, seed_contact, seed_mission
from google.oauth2 import id_token

from ledger import coordination, runtime, trace
from ledger.config import settings


def sweep() -> dict:
    url = settings().worker_url.rstrip("/")
    tok = id_token.fetch_id_token(google.auth.transport.requests.Request(), url)
    req = urllib.request.Request(url + "/tasks/sweep", method="POST", data=b"{}", headers={
        "Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def main() -> bool:
    ev = Evidence("s7_interrupted", "Failed or abandoned segments end in review, never RUNNING",
                  where="local process + sweep on Cloud Run -> Firestore, BigQuery, Sessions")
    d, rid = db(), run_id()

    mid = f"m-{rid}-fail"
    alias = f"lee.{rid}@example.com"
    seed_contact(f"c-{rid}-lee", [alias])
    seed_mission(mid, "model_failure", alias, {"first_name": "Lee"}, "Follow up with the contact.",
                 skills=("follow-up-writer",), local_runner=True)
    out = asyncio.run(runtime.start(mid))
    doc = d.collection("missions").document(mid).get().to_dict()
    kinds = [e["kind"] for e in trace.read(d, mid)]
    ev.step("a. model call failed inside the segment", result=out, trace=kinds[-3:])
    ev.check("a. mission closed as NEEDS_REVIEW by the failing segment itself",
             doc["status"] == "NEEDS_REVIEW" and ("segment.failed" in kinds or "agent.model_error" in kinds),
             {"status": doc["status"],
                                                                             "reason": doc.get("reason")})

    dead, alive = f"m-{rid}-dead", f"m-{rid}-alive"
    for m, secs in ((dead, 3), (alive, 600)):
        lease = coordination.acquire_lease(d, m, f"worker-{m[-5:]}", secs)
        coordination.fenced(d, lease, lambda tx, _d, m=m: tx.update(
            d.collection("missions").document(m), {"status": "RUNNING", "mission_id": m}))
    ev.step("b/c. two missions RUNNING: one owner gone (3 s lease), one alive (600 s lease)")
    time.sleep(5)
    res = sweep()
    st_dead = d.collection("missions").document(dead).get().to_dict()["status"]
    st_alive = d.collection("missions").document(alive).get().to_dict()["status"]
    swept_by = [e for e in trace.read(d, dead) if e["kind"] == "segment.interrupted"]
    ev.step("sweep called on Cloud Run", response=res, dead=st_dead, alive=st_alive)
    ev.check("b. abandoned mission sent to review once its lease expired",
             st_dead == "NEEDS_REVIEW" and bool(swept_by),
             {"status": st_dead, "by": swept_by[0]["process"] if swept_by else None})
    ev.check("c. mission with a live lease left alone", st_alive == "RUNNING", st_alive)
    lease = coordination.acquire_lease(d, alive, "worker-alive", 600)
    coordination.release(d, lease, {"status": "NEEDS_REVIEW", "reason": "scenario cleanup"})
    return ev.save()


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
