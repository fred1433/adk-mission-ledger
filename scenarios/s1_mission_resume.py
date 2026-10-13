"""Scenario 1: one mission, end to end, across a worker restart.

Topology under test: the ADK worker runs on Cloud Run (not deployed to Agent
Runtime) and uses Agent Platform Sessions on an Agent Runtime instance that
holds no agent code. Firestore holds execution state; BigQuery holds the
mission row and reports; Cloud Tasks delivers wake-ups.

  1. A mission row is written to BigQuery; a Cloud Task starts it.
  2. Process A (Cloud Run): lease, contact reservation, pinned manifest,
     agent equipped, first message proposed, approved, sent once; the agent
     parks on wait_for_reply; lease released; agent discarded.
  3. A competing mission for the same contact, through another alias, is
     refused while A waits.
  4. A new Cloud Run revision is rolled out, retiring process A's revision.
  5. The contact's reply is delivered to the signed webhook TWICE. Its
     timestamp is declared 48 h after parking: the wait is simulated.
  6. Process B (new revision, no local state) resumes the same ADK invocation
     from the pinned manifest, proposes the confirmation, sends it once,
     closes the mission and releases the contact.
  7. An extra wake-up task for the same event is a no-op.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import time
import urllib.request

import google.auth.transport.requests
from common import Evidence, db, jsonable, run_id, seed_contact, seed_mission
from google.cloud import firestore
from google.oauth2 import id_token

from ledger import adapters, inbound, tasks, trace
from ledger.config import settings

LIVE = "--live" in sys.argv


def wait_status(d, mid: str, want: set[str], timeout: int = 240) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        doc = d.collection("missions").document(mid).get().to_dict() or {}
        if doc.get("status") in want:
            return doc
        time.sleep(3)
    raise TimeoutError(f"{mid} did not reach {want}; last {doc.get('status')}")


def post(path: str, payload: dict, headers: dict | None = None) -> dict:
    url = settings().worker_url.rstrip("/")
    tok = id_token.fetch_id_token(google.auth.transport.requests.Request(), url)
    raw = json.dumps(payload).encode()
    req = urllib.request.Request(url + path, data=raw, method="POST", headers={
        "Authorization": f"Bearer {tok}", "Content-Type": "application/json", **(headers or {})})
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def provider_messages(d, mid: str) -> list[dict]:
    q = d.collection(adapters.PROVIDER_MESSAGES).where(filter=firestore.FieldFilter("mission_id", "==", mid))
    return sorted((s.to_dict() for s in q.stream()), key=lambda m: m["accepted_at"])


def session_facts(session_id: str) -> dict:
    import vertexai
    s = settings()
    c = vertexai.Client(project=s.project, location=s.location)
    name = f"projects/{s.project}/locations/{s.location}/reasoningEngines/{s.agent_engine_id}/sessions/{session_id}"
    sess = c.agent_engines.sessions.get(name=name)
    events = list(c.agent_engines.sessions.events.list(name=name))
    return {"name": name, "expire_time": str(getattr(sess, "expire_time", None)),
            "event_count": len(events)}


def main() -> bool:
    try:
        return _main()
    except Exception as e:  # record the failure as evidence instead of losing it
        print("SCENARIO ERROR", repr(e))
        if _CURRENT.get("ev"):
            _CURRENT["ev"].check("scenario completed without error", False, repr(e)[:300])
            _CURRENT["ev"].save(**_CURRENT.get("extra", {}))
        return False


_CURRENT: dict = {}


def _main() -> bool:
    kind = "consultation_follow_up"
    ev = Evidence("s1_mission_resume" + ("_live" if LIVE else ""),
                  "One mission across a worker restart" + (" (live Gemini)" if LIVE else ""),
                  where="Cloud Run worker + Cloud Scheduler/Cloud Tasks + Firestore + BigQuery + Agent Platform Sessions",
                  paid_spend=("Gemini on the worker (agent and reviewer), capped at "
                              "LEDGER_MAX_MODEL_CALLS per process" if LIVE else "none"))
    if LIVE:
        ev.doc["model_mode"] = "live (worker env)"
    _CURRENT["ev"] = ev
    d, rid = db(), run_id()
    cid = f"c-{rid}-jordan"
    email, phone = f"jordan.{rid}@example.com", "+1 555 " + "".join(c for c in rid if c.isdigit())[-7:]
    seed_contact(cid, [email, phone])
    facts = {"first_name": "Jordan", "slots": ["Tuesday 10:00", "Thursday 15:00"],
             "topic": "consultation"}
    prompt = ("Follow up with the contact who asked for a consultation. Contact: " + email + ". Offer the "
              "two slots in the facts, wait for the answer, then confirm the slot they choose. Facts: "
              + json.dumps(facts))
    mid_a, mid_b = f"m-{rid}-a", f"m-{rid}-b"
    seed_mission(mid_a, kind, email, facts, prompt)
    _CURRENT["extra"] = {"mission_id": mid_a, "competing_mission_id": mid_b}
    ev.step("mission row written to BigQuery", mission=mid_a,
            table=f"{settings().project}.{settings().bq_dataset}.missions")
    got = post("/tasks/intake", {})
    ev.step("intake on Cloud Run read BigQuery and enqueued start tasks (the scheduled intake may "
            "have done it first; task names make that harmless)", intake=got)
    doc_a = wait_status(d, mid_a, {"WAITING_REPLY", "NEEDS_REVIEW", "DONE"})
    ev.check("A parked waiting for a reply", doc_a["status"] == "WAITING_REPLY", doc_a["status"])
    parked_by = doc_a.get("parked_by", {})
    ev.step("process A parked the mission", parked_by=parked_by, generation=doc_a["generation"],
            session_id=doc_a["session_id"], invocation_id=doc_a["invocation_id"],
            pending_call_id=doc_a["pending_call_id"], manifest=doc_a["manifest"])
    msgs = provider_messages(d, mid_a)
    ev.check("first message accepted by the provider once", len(msgs) == 1, [m["body"] for m in msgs])

    seed_mission(mid_b, kind, phone, facts, prompt)
    got_b = post("/tasks/intake", {})
    ev.step("competing mission row written; intake run again", intake=got_b)
    doc_b = wait_status(d, mid_b, {"BLOCKED_CONTACT_RESERVED", "WAITING_REPLY", "NEEDS_REVIEW"})
    ev.check("competing mission via the phone alias refused while A waits",
             doc_b["status"] == "BLOCKED_CONTACT_RESERVED" and doc_b.get("blocked_by") == mid_a,
             {"status": doc_b["status"], "blocked_by": doc_b.get("blocked_by")})

    s = settings()
    out = subprocess.run(["gcloud", "run", "services", "update", "ledger-worker", "--region", s.location,
                          "--project", s.project, "--update-env-vars", f"LEDGER_ROLLOUT={rid}",
                          "--format=value(status.latestReadyRevisionName)", "--quiet"],
                         capture_output=True, text=True)
    new_rev = out.stdout.strip()
    ev.step("new Cloud Run revision rolled out; process A's revision retired",
            old_revision=parked_by.get("revision"), new_revision=new_rev)

    reply = {"provider": "sandbox", "account": "demo", "provider_message_id": f"sbx-reply-{rid}",
             "thread_token": doc_a["thread_token"],
             "from": email, "body": "Thursday at 15:00 works for me.",
             "simulated_received_at": (doc_a["parked_at"] + dt.timedelta(hours=48)).isoformat()}
    raw_sig = inbound.signature(s.webhook_secret, json.dumps(reply).encode())
    r1 = post("/inbound", reply, {"X-Sandbox-Signature": raw_sig})
    r2 = post("/inbound", reply, {"X-Sandbox-Signature": raw_sig})
    ev.step("reply delivered to the webhook twice (48 h wait simulated by timestamp)",
            first=r1, second=r2, simulated_received_at=reply["simulated_received_at"])
    ev.check("second delivery recognised as duplicate", r2.get("status") == "duplicate", r2)

    final = wait_status(d, mid_a, {"DONE", "NEEDS_REVIEW", "NEEDS_RECONCILIATION"})
    ev.check("mission closed", final["status"] == "DONE", final["status"])
    msgs = provider_messages(d, mid_a)
    ev.check("provider holds exactly two messages (first contact + confirmation)", len(msgs) == 2,
             [m["body"] for m in msgs])

    tasks.enqueue("/tasks/resume", {"mission_id": mid_a, "event_id": inbound.event_id(reply)})
    t0 = time.time()
    while time.time() - t0 < 90 and not any(e["kind"] == "resume.noop" for e in trace.read(d, mid_a)):
        time.sleep(3)

    t = trace.read(d, mid_a)
    starts = [e for e in t if e["kind"] == "resume.start"]
    procs = {e["kind"]: e["process"] for e in t if e["kind"] in ("manifest.pinned", "resume.start")}
    ev.check("exactly one resume advanced the mission", len(starts) == 1, len(starts))
    ev.check("an extra wake-up task was a no-op", any(e["kind"] == "resume.noop" for e in t),
             [e["kind"] for e in t if e["kind"] == "resume.noop"])
    pa, pb = procs.get("manifest.pinned", {}), procs.get("resume.start", {})
    ev.check("resume ran in a different process on a different revision",
             pa.get("revision") != pb.get("revision") and pa.get("instance") != pb.get("instance"),
             {"parked_by": pa, "resumed_by": pb})
    verified = [e for e in t if e["kind"] == "manifest.verified"]
    ev.check("pinned manifest verified before resuming", bool(verified) and verified[0]["mismatches"] == [],
             verified[0]["mismatches"] if verified else None)
    res = d.collection("contact_reservations").document(cid).get().to_dict()
    ev.check("contact reservation released on closure", res["status"] == "RELEASED", res["status"])
    sf = session_facts(final["session_id"])
    ev.step("Agent Platform Sessions record", **sf)
    calls = [e["name"] for e in t if e["kind"] == "agent.call"]
    cands = [x.to_dict() for x in d.collection("memory_candidates")
             .where(filter=firestore.FieldFilter("mission_id", "==", mid_a)).stream()]
    ev.step("memory candidates left in quarantine by this mission",
            candidates=[{"candidate_id": c["candidate_id"], "topic": c["topic"], "status": c["status"]}
                        for c in cands])
    compact = [{"at": str(e["at"])[11:23], "kind": e["kind"],
                "where": (e["process"].get("revision") or "local") + "/" + str(e["process"].get("instance"))[-6:],
                **{k: e[k] for k in ("name", "action_id", "decision", "mode", "generation",
                                     "provider_message_id", "reasons") if k in e}} for e in t]
    return ev.save(mission_id=mid_a, competing_mission_id=mid_b, agent_calls=calls,
                   provider_messages=jsonable(msgs), trace=jsonable(compact),
                   final_text=final.get("last_segment_text"))


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
