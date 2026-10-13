"""Who may act, decided in Firestore transactions.

Two separate lifecycles:

* Execution lease (missions/{id}.lease): short, owned by one worker process,
  carries a monotonically increasing `generation` (fencing token). Every write
  a worker makes about a mission goes through `fenced()`, which re-reads the
  generation inside the same transaction. A worker that stalled past its lease
  and was replaced cannot commit anything afterwards.

* Contact reservation (contact_reservations/{contact_id}): owned by a MISSION,
  not by a process. It survives worker shutdown while the mission waits for a
  reply. It ends on closure, cancellation or the business timeout, never
  because a process died. Expiry is checked logically inside the transaction;
  Firestore TTL deletion ("typically within 24 hours") is only for cleanup.

BigQuery holds missions and reporting. It is never consulted to decide whether
an action may run: a late BigQuery status cannot cause a second execution.
"""

from __future__ import annotations

import dataclasses
import urllib.parse
import datetime as dt
from typing import Any, Callable

from google.cloud import firestore

from .clock import wall_now

MISSIONS = "missions"
RESERVATIONS = "contact_reservations"
TX_ATTEMPTS = 25


class LeaseUnavailable(Exception):
    """Another live worker holds this mission."""


class FencingError(Exception):
    """This worker's generation is stale; its write was refused."""


class ContactReserved(Exception):
    def __init__(self, contact_id: str, holder: str, status: str):
        super().__init__(f"contact {contact_id} reserved by {holder} ({status})")
        self.contact_id, self.holder, self.status = contact_id, holder, status


@dataclasses.dataclass(frozen=True)
class Lease:
    mission_id: str
    worker_id: str
    generation: int
    expires_at: dt.datetime


def doc_id(key: str) -> str:
    """Collision-free Firestore document id for an arbitrary key ("c/1" != "c_1")."""
    return urllib.parse.quote(key, safe="")


def client(project: str | None = None) -> firestore.Client:
    return firestore.Client(project=project) if project else firestore.Client()


def acquire_lease(db: firestore.Client, mission_id: str, worker_id: str, seconds: int) -> Lease:
    ref = db.collection(MISSIONS).document(mission_id)

    @firestore.transactional
    def _tx(tx: firestore.Transaction) -> Lease:
        snap = ref.get(transaction=tx)
        data = snap.to_dict() or {}
        lease = data.get("lease") or {}
        now = wall_now()
        owner = lease.get("owner")
        if owner and owner != worker_id and lease.get("expires_at") and lease["expires_at"] > now:
            raise LeaseUnavailable(f"{mission_id} held by {owner} until {lease['expires_at']}")
        generation = int(data.get("generation", 0)) + 1
        expires = now + dt.timedelta(seconds=seconds)
        tx.set(ref, {
            "generation": generation,
            "lease": {"owner": worker_id, "generation": generation, "expires_at": expires},
            "updated_at": now,
        }, merge=True)
        return Lease(mission_id, worker_id, generation, expires)

    return _tx(db.transaction(max_attempts=TX_ATTEMPTS))


def fenced(db: firestore.Client, lease: Lease,
           fn: Callable[[firestore.Transaction, dict], Any]) -> Any:
    """Run fn(tx, mission_doc) only if `lease` is still the current owner."""
    ref = db.collection(MISSIONS).document(lease.mission_id)

    @firestore.transactional
    def _tx(tx: firestore.Transaction):
        data = ref.get(transaction=tx).to_dict() or {}
        current = data.get("lease") or {}
        if int(data.get("generation", -1)) != lease.generation or current.get("owner") != lease.worker_id:
            raise FencingError(
                f"stale generation {lease.generation} for {lease.mission_id}; "
                f"current is {data.get('generation')} owned by {current.get('owner')}")
        if current.get("expires_at") and current["expires_at"] <= wall_now():
            raise FencingError(f"lease generation {lease.generation} expired at {current['expires_at']}")
        return fn(tx, data)

    return _tx(db.transaction(max_attempts=TX_ATTEMPTS))


def renew_if_due(db: firestore.Client, lease: Lease, seconds: int) -> Lease:
    """Extend the lease once less than half of it remains. Fenced, so a worker
    that already lost its generation finds out here and stops."""
    remaining = (lease.expires_at - wall_now()).total_seconds()
    return renew(db, lease, seconds) if remaining < seconds / 2 else lease


def renew(db: firestore.Client, lease: Lease, seconds: int) -> Lease:
    expires = wall_now() + dt.timedelta(seconds=seconds)
    ref = db.collection(MISSIONS).document(lease.mission_id)
    fenced(db, lease, lambda tx, _d: tx.update(ref, {"lease.expires_at": expires}))
    return dataclasses.replace(lease, expires_at=expires)


def release(db: firestore.Client, lease: Lease, updates: dict) -> None:
    """Hand the mission back (parked or finished). Generation is kept."""
    ref = db.collection(MISSIONS).document(lease.mission_id)

    def _w(tx, _d):
        tx.update(ref, {**updates, "lease.owner": None, "lease.expires_at": None,
                        "updated_at": wall_now()})

    fenced(db, lease, _w)


def reserve_contact(db: firestore.Client, contact_id: str, mission_id: str,
                    business_timeout: dt.timedelta) -> dict:
    ref = db.collection(RESERVATIONS).document(doc_id(contact_id))

    @firestore.transactional
    def _tx(tx: firestore.Transaction) -> dict:
        data = ref.get(transaction=tx).to_dict()
        now = wall_now()
        if data and data.get("status") == "HELD":
            if data["mission_id"] == mission_id:
                return data
            if data["business_expires_at"] > now:
                raise ContactReserved(contact_id, data["mission_id"], data.get("mission_status", "HELD"))
        doc = {
            "contact_id": contact_id, "mission_id": mission_id, "status": "HELD",
            "acquired_at": now, "business_expires_at": now + business_timeout,
            "previous_holder": (data or {}).get("mission_id"),
        }
        tx.set(ref, doc)
        return doc

    return _tx(db.transaction(max_attempts=TX_ATTEMPTS))


def reservation_held_by(db: firestore.Client, contact_id: str, mission_id: str,
                        tx: firestore.Transaction | None = None) -> bool:
    ref = db.collection(RESERVATIONS).document(doc_id(contact_id))
    data = (ref.get(transaction=tx) if tx else ref.get()).to_dict()
    return bool(data and data.get("status") == "HELD" and data.get("mission_id") == mission_id
                and data["business_expires_at"] > wall_now())


def release_contact(db: firestore.Client, contact_id: str, mission_id: str, reason: str) -> bool:
    ref = db.collection(RESERVATIONS).document(doc_id(contact_id))

    @firestore.transactional
    def _tx(tx: firestore.Transaction) -> bool:
        data = ref.get(transaction=tx).to_dict()
        if not data or data.get("mission_id") != mission_id or data.get("status") != "HELD":
            return False
        tx.update(ref, {"status": "RELEASED", "released_at": wall_now(), "release_reason": reason})
        return True

    return _tx(db.transaction(max_attempts=TX_ATTEMPTS))
