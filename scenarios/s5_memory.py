"""Scenario 5: memory candidates from scenario 1, validated, then published.

Input: the two candidates mission A of scenario 1 left in quarantine:
  * contact scope  "Prefers afternoon appointments."           -> approve
  * playbook scope "Offering a discount makes hesitant ..."    -> reject
    (rule: one mission cannot become a playbook rule)

Then three adversarial cases on real Firestore:
  a. a candidate still QUARANTINED is handed to the publisher: refused,
     because the publisher re-reads the record instead of trusting its input;
  b. the publisher dies between memories.create and its ledger write; the
     retry finds the fact in the listing and creates no duplicate;
  c. two contradictory facts on the same contact and topic are validated in
     parallel: one is approved, the other held for review.

Default: the publisher writes to FakeMemoryBank (in process) and the worker
identity is checked with testIamPermissions from inside Cloud Run.
--live: Agent Platform Memory Bank under the memory-publisher identity, with
read-back, full listing, similarity retrieval and a refused write probe.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import os
import sys
import urllib.request

import google.auth.transport.requests
from common import Evidence, db, run_id
from google.auth import impersonated_credentials
from google.cloud import firestore
from google.oauth2 import id_token

from ledger import memory
from ledger.config import settings

LIVE = "--live" in sys.argv


def worker_call(path: str, method: str = "GET") -> dict:
    url = settings().worker_url.rstrip("/")
    tok = id_token.fetch_id_token(google.auth.transport.requests.Request(), url)
    req = urllib.request.Request(url + path, method=method, data=b"{}" if method == "POST" else None,
                                 headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def main() -> bool:
    mission_id = sys.argv[sys.argv.index("--mission") + 1]
    ev = Evidence("s5_memory" + ("_live" if LIVE else ""),
                  "Quarantine, validation, create-only publication" + (" (live Memory Bank)" if LIVE else ""),
                  where=("local validator and publisher -> Firestore; bank: " +
                         ("Agent Platform Memory Bank as memory-publisher" if LIVE else "FakeMemoryBank (in process)")
                         + "; worker identity checks inside Cloud Run"),
                  paid_spend="Memory Bank embeddings (create, retrieve)" if LIVE else "none")
    s, d = settings(), db()
    engine = f"projects/{s.project}/locations/{s.location}/reasoningEngines/{s.agent_engine_id}"
    cands = [x.to_dict() for x in d.collection(memory.CANDIDATES)
             .where(filter=firestore.FieldFilter("mission_id", "==", mission_id)).stream()]
    ev.step("quarantined candidates from scenario 1", mission_id=mission_id,
            candidates=[{k: c[k] for k in ("candidate_id", "topic", "scope", "fact", "status")} for c in cands])

    if LIVE:
        import google.auth
        import vertexai
        src, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds = impersonated_credentials.Credentials(
            source_credentials=src, target_principal=f"memory-publisher@{s.project}.iam.gserviceaccount.com",
            target_scopes=["https://www.googleapis.com/auth/cloud-platform"])
        api = vertexai.Client(project=s.project, location=s.location, credentials=creds).agent_engines.memories
        reader = vertexai.Client(project=s.project, location=s.location).agent_engines.memories
    else:
        api = reader = memory.FakeMemoryBank()
    pub = memory.MemoryPublisher(d, api, engine)

    results = {c["topic"]: memory.validate(d, c, supporting_missions=1) for c in cands}
    for topic, v in results.items():
        ev.step(f"validated {topic}", status=v["status"], stage=v["validation"]["stage"],
                reasons=v["validation"]["reasons"], reviewer_mode=v["validation"].get("reviewer_mode"))
    good, bad = results.get("preferred_time_of_day"), results.get("discount_for_hesitation")
    ev.check("contact-scoped preference approved", bool(good) and good["status"] == "APPROVED", good and good["status"])
    ev.check("single-mission playbook lesson rejected", bool(bad) and bad["status"] == "REJECTED",
             bad and bad["validation"]["reasons"])
    try:
        pub.publish(bad["candidate_id"])
        ev.check("publisher refuses a rejected candidate", False, "published")
    except PermissionError as e:
        ev.check("publisher refuses a rejected candidate", True, str(e))

    published = pub.publish(good["candidate_id"])
    ev.step("approved fact published with memories.create", **published)
    texts = [memory._fact(m) for m in reader.list(name=engine)]
    if LIVE:
        got = api.get(name=published["memory_name"])
        retrieved = list(reader.retrieve(name=engine, scope=good["scope"],
                                         similarity_search_params={"search_query": "what time of day suits them", "top_k": 3}))
        ev.check("approved fact read back unchanged", got.fact == good["fact"], got.fact)
        ev.check("positive control: approved fact retrieved by similarity search",
                 any(r.memory.fact == good["fact"] for r in retrieved), [r.memory.fact for r in retrieved])
        probe = worker_call("/diag/memory-write-probe", "POST")
        ev.step("worker identity probe on Cloud Run", **probe)
        ev.check("worker identity: valid read succeeds", probe.get("read_ok") is True, probe.get("memories_listed"))
        ev.check("worker identity: valid memories.create refused by the platform",
                 probe.get("write_refused") is True, probe.get("write_error"))
    else:
        ev.check("approved fact stored unchanged", good["fact"] in texts, texts)
        ev.check("publisher used create and list only", set(api.calls) <= {"create", "list"}, api.calls)
    ev.check("rejected text absent from the complete listing", bad["fact"] not in texts, {"listed": len(texts)})

    # a. a QUARANTINED candidate handed straight to the publisher
    rid = run_id()
    scope = {"contact_id": f"c-{rid}-mem"}
    q = memory.quarantine(d, mission_id=f"m-{rid}-mem", fact="Prefers email over phone.", scope=scope,
                          topic="preferred_channel", evidence="said so in the reply")
    try:
        pub.publish(q["candidate_id"])
        ev.check("a. publisher refuses a candidate that is still QUARANTINED", False, "published")
    except PermissionError as e:
        ev.check("a. publisher refuses a candidate that is still QUARANTINED", True, str(e))

    # b. crash between memories.create and the ledger write, then retry
    v = memory.validate(d, q, supporting_missions=1, reviewer=lambda c: {
        "decision": "APPROVE", "reasons": ["inline"], "reviewer_mode": "fixture:inline"})
    os.environ["LEDGER_FAULT"] = "after_memory_create"
    try:
        pub.publish(q["candidate_id"])
    except RuntimeError as e:
        ev.step("b. publisher died after memories.create", error=str(e))
    os.environ["LEDGER_FAULT"] = ""
    retry = pub.publish(q["candidate_id"])
    copies = [m for m in reader.list(name=engine) if memory._fact(m) == q["fact"] and memory._scope(m) == scope]
    ev.check("b. retry reconciles instead of creating a duplicate",
             retry["mode"] == "reconciled-existing" and len(copies) == 1,
             {"retry": retry, "copies_in_bank": len(copies), "validated": v["status"]})

    # c. contradictory facts, validated in parallel
    scope_c = {"contact_id": f"c-{rid}-pair"}
    pair = [memory.quarantine(d, mission_id=f"m-{rid}-pair", fact=f, scope=scope_c, topic="preferred_time_of_day",
                              evidence="reply") for f in ("Prefers mornings.", "Prefers evenings.")]
    approve = lambda c: {"decision": "APPROVE", "reasons": ["inline"], "reviewer_mode": "fixture:inline"}  # noqa: E731
    with cf.ThreadPoolExecutor(2) as pool:
        outs = list(pool.map(lambda c: memory.validate(d, c, 1, reviewer=approve), pair))
    statuses = sorted(o["status"] for o in outs)
    ev.check("c. two contradictory facts: one approved, one held for review",
             statuses == ["APPROVED", "NEEDS_REVIEW"], [(o["fact"], o["status"]) for o in outs])

    ident = worker_call("/diag/identity")
    ev.step("worker identity permissions (testIamPermissions from inside Cloud Run)", **ident)
    ev.check("worker lacks memories.create/update/delete/generate",
             {"aiplatform.memories.create", "aiplatform.memories.update", "aiplatform.memories.delete",
              "aiplatform.memories.generate"} <= set(ident["denied_on_project"]), ident["denied_on_project"])
    ev.check("worker cannot impersonate the publisher",
             ident["can_impersonate_publisher"] == [], ident["can_impersonate_publisher"])
    return ev.save()


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
