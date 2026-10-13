"""Scenario 5: memory candidates from scenario 1, validated, then published.

Input: the two candidates mission A left in quarantine (Firestore):
  * contact scope  "Prefers afternoon appointments."           -> approve
  * playbook scope "Offering a discount makes hesitant ..."    -> reject
    (rule: one mission cannot become a playbook rule; reviewer agrees)

Default mode: the publisher writes to FakeMemoryBank (in process) and the
worker identity is checked with testIamPermissions from inside Cloud Run.
--live: the publisher writes to Agent Platform Memory Bank with
memories.create under the memory-publisher service account; the approved
fact is read back unchanged and retrieved by similarity search (positive
control); the complete scoped listing is checked for the rejected text; and
the Cloud Run worker, under its own identity, reads successfully and is
refused a memories.create.
"""

from __future__ import annotations

import json
import sys
import urllib.request

import google.auth.transport.requests
from common import Evidence, db
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
                  where=("local validator -> Firestore; publisher -> " +
                         ("Agent Platform Memory Bank as memory-publisher" if LIVE else "FakeMemoryBank (in process)")
                         + "; worker identity checks on Cloud Run"))
    s, d = settings(), db()
    engine = f"projects/{s.project}/locations/{s.location}/reasoningEngines/{s.agent_engine_id}"
    cands = [x.to_dict() for x in d.collection(memory.CANDIDATES)
             .where(filter=firestore.FieldFilter("mission_id", "==", mission_id)).stream()]
    ev.step("quarantined candidates from the mission", mission_id=mission_id,
            candidates=[{k: c[k] for k in ("candidate_id", "topic", "scope", "fact", "status")} for c in cands])

    if LIVE:
        import google.auth
        import vertexai
        src, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds = impersonated_credentials.Credentials(
            source_credentials=src, target_principal=f"memory-publisher@{s.project}.iam.gserviceaccount.com",
            target_scopes=["https://www.googleapis.com/auth/cloud-platform"])
        api = vertexai.Client(project=s.project, location=s.location, credentials=creds).agent_engines.memories
        # Verification reads run as the operator: the publisher role is
        # create/get/list only and cannot run similarity search.
        reader = vertexai.Client(project=s.project, location=s.location).agent_engines.memories
    else:
        api = memory.FakeMemoryBank()
    pub = memory.MemoryPublisher(d, api, engine)

    results = {}
    for c in cands:
        v = memory.validate(d, c, supporting_missions=1)
        results[c["topic"]] = v
        ev.step(f"validated {c['topic']}", status=v["status"], stage=v["validation"]["stage"],
                reasons=v["validation"]["reasons"], reviewer_mode=v["validation"].get("reviewer_mode"))
    good = results.get("preferred_time_of_day")
    bad = results.get("discount_for_hesitation")
    ev.check("contact-scoped preference approved", good and good["status"] == "APPROVED", good and good["status"])
    ev.check("single-mission playbook lesson rejected", bad and bad["status"] == "REJECTED",
             bad and bad["validation"]["reasons"])
    try:
        pub.publish(bad)
        ev.check("publisher refuses a rejected candidate", False, "published")
    except PermissionError as e:
        ev.check("publisher refuses a rejected candidate", True, str(e))

    published = pub.publish(good)
    ev.step("approved fact published with memories.create", **published)
    if LIVE:
        got = api.get(name=published["memory_name"])
        listing = list(reader.list(name=engine))
        texts = [m.fact for m in listing]
        retrieved = list(reader.retrieve(name=engine, scope=good["scope"],
                                      similarity_search_params={"search_query": "what time of day suits them", "top_k": 3}))
        ev.check("approved fact read back unchanged", got.fact == good["fact"], got.fact)
        ev.check("rejected text absent from the complete listing", bad["fact"] not in texts,
                 {"memories_listed": len(texts)})
        ev.check("positive control: approved fact retrieved by similarity search",
                 any(r.memory.fact == good["fact"] for r in retrieved),
                 [r.memory.fact for r in retrieved])
        probe = worker_call("/diag/memory-write-probe", "POST")
        ev.step("worker identity probe on Cloud Run", **probe)
        ev.check("worker identity: valid read succeeds", probe.get("read_ok") is True, probe.get("memories_listed"))
        ev.check("worker identity: valid memories.create refused by the platform",
                 probe.get("write_refused") is True, probe.get("write_error"))
    else:
        texts = [m["fact"] for m in api.list(name=engine)]
        ev.check("approved fact stored unchanged (fake bank)", texts == [good["fact"]], texts)
        ev.check("rejected text absent from the complete listing (fake bank)", bad["fact"] not in texts, len(texts))
        ev.check("publisher used create only", set(api.calls) <= {"create", "list"}, api.calls)
    ident = worker_call("/diag/identity")
    ev.step("worker identity permissions (testIamPermissions from inside Cloud Run)", **ident)
    ev.check("worker lacks memories.create/update/delete/generate",
             {"aiplatform.memories.create", "aiplatform.memories.update", "aiplatform.memories.delete",
              "aiplatform.memories.generate"} <= set(ident["denied_on_project"]), ident["denied_on_project"])
    ev.check("worker cannot impersonate the publisher",
             not [p for p in ident["can_impersonate_publisher"] if not p.startswith("ERROR")],
             ident["can_impersonate_publisher"])
    return ev.save()


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
