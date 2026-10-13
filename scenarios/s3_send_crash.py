"""Scenario 3: the provider accepts the message, then the worker dies.

Two separate OS processes per adapter, against real Firestore:
  process 1 claims the approved action, the provider accepts, and the
            process is killed (os._exit(137)) before the receipt is recorded;
  process 2 starts with no local state after process 1's lease expired and
            runs the dispatcher on the same action.

Expected:
  idempotent adapter -> retry with the same key; provider holds ONE message
  plain adapter      -> NEEDS_RECONCILIATION; provider holds ONE message;
                        nothing is resent blindly
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import time

from common import ROOT, Evidence, db, run_id
from google.cloud import firestore

from ledger import actions, adapters, coordination

LEASE_S = 4


def child(action_id: str, adapter_name: str) -> None:
    d = db()
    mid = action_id.rsplit(".", 1)[0]
    lease = coordination.acquire_lease(d, mid, f"proc-{os.getpid()}", LEASE_S)
    ad = adapters.PlainMailbox(d) if adapter_name == "plain" else adapters.IdempotentMailbox(d)
    out = actions.dispatch(d, lease, action_id, ad, thread_token=f"thr-{mid}")
    print("RESULT", out["status"], out.get("dispatch_mode"))


def prepare(d, rid: str, tag: str) -> str:
    mid = f"m-{rid}-{tag}"
    cid = f"c-{rid}-{tag}"
    coordination.reserve_contact(d, cid, mid, dt.timedelta(hours=1))
    lease = coordination.acquire_lease(d, mid, "setup", 30)
    a, _ = actions.record_proposal(
        d, lease, purpose="first_contact", channel="email", recipient=f"riley.{rid}@example.com",
        contact_id=cid, body="Hi Riley, I can offer Tuesday at 10:00 or Thursday at 15:00. Which suits you?",
        note="", allowed_purposes=["first_contact"], policy_version="outbound-policy-v1",
        persona_sha="p", skills_sha="s", fc_ref={})
    actions.set_verdict(d, lease, a["action_id"], {"decision": "APPROVE", "reasons": ["fixture"],
                                                   "reviewer_mode": "fixture:inline", "stage": "reviewer"})
    coordination.release(d, lease, {"status": "RUNNING"})
    return a["action_id"]


def provider_count(d, aid: str) -> int:
    msgs = d.collection(adapters.PROVIDER_MESSAGES)
    by_key = msgs.where(filter=firestore.FieldFilter("idempotency_key", "==", aid)).stream()
    by_ref = msgs.where(filter=firestore.FieldFilter("client_reference", "==", aid)).stream()
    return len(list(by_key)) + len(list(by_ref))


def run_child(aid: str, adapter_name: str, fault: bool) -> subprocess.CompletedProcess:
    env = {**os.environ, "LEDGER_FAULT": "after_provider_accept" if fault else ""}
    return subprocess.run([sys.executable, "-W", "ignore", __file__, "child", aid, adapter_name],
                          env=env, capture_output=True, text=True, cwd=ROOT / "scenarios")


def main() -> bool:
    ev = Evidence("s3_send_crash", "Provider accepts, worker crashes before recording the receipt",
                  where="two local OS processes -> Firestore (Google Cloud); test adapters")
    d, rid = db(), run_id()
    for name in ("idempotent", "plain"):
        aid = prepare(d, rid, name)
        p1 = run_child(aid, name, fault=True)
        st1 = d.collection(actions.ACTIONS).document(aid).get().to_dict()
        ev.step(f"[{name}] process 1 killed after provider acceptance", exit_code=p1.returncode,
                action_status=st1["status"], provider_messages=provider_count(d, aid))
        time.sleep(LEASE_S + 1)
        p2 = run_child(aid, name, fault=False)
        st2 = d.collection(actions.ACTIONS).document(aid).get().to_dict()
        n = provider_count(d, aid)
        ev.step(f"[{name}] process 2 ran the dispatcher", exit_code=p2.returncode,
                stdout=p2.stdout.strip().splitlines()[-1:] , action_status=st2["status"],
                attempts=st2.get("attempts"), provider_messages=n,
                receipt=st2.get("receipt"))
        ev.check(f"[{name}] process 1 died with the injected fault", p1.returncode == 137, p1.returncode)
        ev.check(f"[{name}] provider holds exactly one message", n == 1, n)
        expected = "SENT" if name == "idempotent" else "NEEDS_RECONCILIATION"
        ev.check(f"[{name}] action ends {expected}", st2["status"] == expected, st2["status"])
        if name == "idempotent":
            ev.check("[idempotent] retry reused the original provider message",
                     (st2.get("receipt") or {}).get("replayed") is True, st2.get("receipt"))
    return ev.save()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "child":
        child(sys.argv[2], sys.argv[3])
    else:
        raise SystemExit(0 if main() else 1)
