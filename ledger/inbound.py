"""Inbound replies: authenticate, record and deduplicate, correlate, wake.

Order matters. The event and its pending wake are made durable BEFORE the
webhook is acknowledged and before any task exists. If the process dies
between storing the event and creating the task, `reconcile_pending_wakes`
finds the PENDING event and enqueues it. Wake tasks are named after the
event, so a reconcile pass and the original attempt cannot both wake twice;
and even a second wake is a no-op, because the resume handler consumes the
event inside the fenced transaction that closes the segment.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac

from google.api_core import exceptions as gexc
from google.cloud import firestore

from . import tasks, trace
from .clock import wall_now
from .runtime import INBOUND, THREADS


def signature(secret: str, raw: bytes) -> str:
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def verify(secret: str, raw: bytes, given: str | None) -> bool:
    return bool(secret) and bool(given) and hmac.compare_digest(signature(secret, raw), given)


def accept(db: firestore.Client, payload: dict) -> dict:
    thread = db.collection(THREADS).document(payload["thread_token"]).get()
    if not thread.exists:
        db.collection("unmatched_inbound").add({**payload, "received_at": wall_now()})
        return {"status": "unmatched"}
    mission_id = thread.get("mission_id")
    pmid = payload["provider_message_id"]
    ref = db.collection(INBOUND).document(pmid)
    try:
        ref.create({"mission_id": mission_id, "provider_message_id": pmid,
                    "from": payload["from"], "body": payload["body"],
                    "simulated_received_at": payload.get("simulated_received_at"),
                    "received_at": wall_now(), "wake_state": "PENDING"})
    except gexc.AlreadyExists:
        trace.record(db, mission_id, "inbound.duplicate", provider_message_id=pmid)
        return {"status": "duplicate", "mission_id": mission_id}
    trace.record(db, mission_id, "inbound.recorded", provider_message_id=pmid,
                 simulated_received_at=payload.get("simulated_received_at"))
    return _wake(db, ref, mission_id, pmid)


def _wake(db, ref, mission_id: str, pmid: str) -> dict:
    try:
        name = tasks.enqueue("/tasks/resume", {"mission_id": mission_id, "event_id": pmid},
                             name=f"wake-{mission_id}-{pmid}")
    except Exception as e:  # noqa: BLE001 - the reconciler picks it up
        trace.record(db, mission_id, "wake.enqueue_failed", error=str(e)[:200])
        return {"status": "recorded", "wake": "pending"}
    ref.update({"wake_state": "ENQUEUED", "task": name})
    trace.record(db, mission_id, "wake.enqueued", task=name.rsplit("/", 1)[-1])
    return {"status": "recorded", "mission_id": mission_id, "wake": "enqueued"}


def reconcile_pending_wakes(db: firestore.Client, older_than_s: int = 30) -> list[str]:
    cutoff = wall_now() - dt.timedelta(seconds=older_than_s)
    q = db.collection(INBOUND).where(filter=firestore.FieldFilter("wake_state", "==", "PENDING"))
    done = []
    for snap in q.stream():
        d = snap.to_dict()
        if d["received_at"] <= cutoff:
            _wake(db, snap.reference, d["mission_id"], snap.id)
            done.append(snap.id)
    return done
