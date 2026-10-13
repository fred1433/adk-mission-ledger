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
    mid = d.collection(actions.ACTIONS).document(action_id).get().get("mission_id")
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


def run_child(aid: str, adapter_name: str, fault: bool | str) -> subprocess.CompletedProcess:
    env = {**os.environ, "LEDGER_FAULT": ("after_provider_accept" if fault is True else (fault or ""))}
    return subprocess.run([sys.executable, "-W", "ignore", __file__, "child", aid, adapter_name],
                          env=env, capture_output=True, text=True, cwd=ROOT / "scenarios")


def spawn_child(aid: str, adapter_name: str, fault: str) -> subprocess.Popen:
    env = {**os.environ, "LEDGER_FAULT": fault, "LEDGER_FAULT_SLEEP": str(LEASE_S + 6)}
    return subprocess.Popen([sys.executable, "-W", "ignore", __file__, "child", aid, adapter_name],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                            cwd=ROOT / "scenarios")


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

    # Slow but alive: process 1 claims, then stalls past its lease before
    # calling the provider; process 2 finds DISPATCHING on a plain adapter and
    # parks it for reconciliation; process 1 finally sends. Its receipt must
    # not be lost, and it must not report SENT.
    aid = prepare(d, rid, "slow")
    p1 = spawn_child(aid, "plain", "slow_before_send")
    time.sleep(2)
    claimed = d.collection(actions.ACTIONS).document(aid).get().to_dict()["status"]
    time.sleep(LEASE_S + 1)
    p2 = run_child(aid, "plain", fault=False)
    out1, _ = p1.communicate(timeout=60)
    st = d.collection(actions.ACTIONS).document(aid).get().to_dict()
    ev.step("[slow] process 1 stalled past its lease, process 2 ran, then process 1 sent",
            status_after_claim=claimed, process2=p2.stdout.strip().splitlines()[-1:],
            process1=out1.strip().splitlines()[-1:], final_status=st["status"],
            late_receipt=st.get("late_receipt"), provider_messages=provider_count(d, aid))
    ev.check("[slow] process 2 parked it for reconciliation",
             "NEEDS_RECONCILIATION" in " ".join(p2.stdout.splitlines()[-1:]), p2.stdout.strip()[-80:])
    ev.check("[slow] late provider receipt kept on the action", bool(st.get("late_receipt")),
             st.get("late_receipt"))
    ev.check("[slow] process 1 reports the real status, not SENT",
             "RESULT NEEDS_RECONCILIATION" in out1, out1.strip().splitlines()[-1:])

    # Draft publisher: a replay returns the same draft, no second effect.
    pub = adapters.DraftPublisher(d)
    r1 = pub.send(idempotency_key=f"draft-{rid}", to="Draft title", body="draft body", thread_token="",
                  mission_id=f"m-{rid}-draft")
    r2 = pub.send(idempotency_key=f"draft-{rid}", to="Draft title", body="draft body", thread_token="",
                  mission_id=f"m-{rid}-draft")
    ev.check("[draft] replay returns the same draft without a second write",
             r1.provider_message_id == r2.provider_message_id and r2.replayed and not r1.replayed,
             {"first": r1.__dict__, "second": r2.__dict__})
    return ev.save()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "child":
        child(sys.argv[2], sys.argv[3])
    else:
        raise SystemExit(0 if main() else 1)
