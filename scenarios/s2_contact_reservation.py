"""Scenario 2: one known contact, many concurrent missions; then fencing.

Runs from a local process against real Firestore.

A. 20 missions reach for the same contact at once, half through its email
   alias, half through its phone alias. Exactly one reservation owner.
B. The owner parks (worker gone, lease released). A new mission through the
   other alias is still refused: the reservation belongs to the mission, not
   to the process.
C. Fencing: worker W1 holds generation g, stalls past its lease; W2 takes
   over with g+1; W1 then tries to record an action and is refused.
"""

from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import time

from common import Evidence, db, run_id, seed_contact

from ledger import actions, contacts, coordination
from ledger.coordination import ContactReserved, FencingError

N = 20


def main() -> bool:
    ev = Evidence("s2_contact_reservation", "Contact reservation vs worker lease, and fencing",
                  where="local process -> Firestore (Google Cloud)")
    d, rid = db(), run_id()
    cid = f"c-{rid}"
    email = f"casey.{rid}@example.com"
    phone = "+1 555 " + "".join(c for c in rid if c.isdigit())[-7:]
    seed_contact(cid, [email, phone])
    ev.step("registered one contact with two aliases", contact_id=cid,
            aliases=[contacts.normalise_alias(email), contacts.normalise_alias(phone)])

    def attempt(i: int):
        alias = email if i % 2 == 0 else phone
        mid = f"m-{rid}-{i:02d}"
        ref = contacts.resolve(d, alias)
        try:
            coordination.reserve_contact(d, ref.contact_id, mid, dt.timedelta(hours=72))
            return mid, "won", ref.alias_key
        except ContactReserved as e:
            return mid, f"refused:{e.holder}", ref.alias_key

    t0 = time.monotonic()
    with cf.ThreadPoolExecutor(N) as pool:
        results = list(pool.map(attempt, range(N)))
    winners = [r for r in results if r[1] == "won"]
    ev.step(f"{N} concurrent reservation attempts", elapsed_s=round(time.monotonic() - t0, 2),
            results=[list(r) for r in results])
    ev.check("exactly one reservation owner", len(winners) == 1, [w[0] for w in winners])
    owner = winners[0][0] if winners else None
    ev.check("both aliases resolved to the same contact",
             {contacts.resolve(d, email).contact_id, contacts.resolve(d, phone).contact_id} == {cid}, cid)

    # B. the owner parks: lease taken then released, reservation untouched.
    lease = coordination.acquire_lease(d, owner, "w-park-test", 30)
    coordination.release(d, lease, {"status": "WAITING_REPLY"})
    ev.step("owner mission parked; its worker lease released", mission=owner, generation=lease.generation)
    try:
        coordination.reserve_contact(d, cid, f"m-{rid}-late", dt.timedelta(hours=72))
        ev.check("new mission refused while owner waits for a reply", False, "reservation granted")
    except ContactReserved as e:
        ev.check("new mission refused while owner waits for a reply", e.holder == owner,
                 f"refused, holder={e.holder}")

    # C. fencing
    mid = f"m-{rid}-fence"
    coordination.reserve_contact(d, f"c-{rid}-fence", mid, dt.timedelta(hours=1))
    w1 = coordination.acquire_lease(d, mid, "worker-1", 2)
    ev.step("worker-1 acquired lease", generation=w1.generation, lease_seconds=2)
    time.sleep(3)
    w2 = coordination.acquire_lease(d, mid, "worker-2", 30)
    ev.step("worker-1 stalled past expiry; worker-2 took over", generation=w2.generation)
    kwargs = dict(purpose="first_contact", channel="email", recipient=email,
                  contact_id=f"c-{rid}-fence", body="stale write attempt from worker one",
                  note="", allowed_purposes=["first_contact"], policy_version="outbound-policy-v1",
                  persona_sha="x", skills_sha="x", fc_ref={})
    try:
        actions.record_proposal(d, w1, **kwargs)
        ev.check("stale worker-1 cannot commit an action", False, "write accepted")
    except FencingError as e:
        ev.check("stale worker-1 cannot commit an action", True, str(e))
    a, created = actions.record_proposal(d, w2, **{**kwargs, "body": "write from the current owner"})
    ev.check("current worker-2 can commit", created and a["proposed_generation"] == w2.generation,
             {"action_id": a["action_id"], "generation": a["proposed_generation"]})
    return ev.save()


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
