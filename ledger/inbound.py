"""Inbound replies: authenticate, validate, attribute, record, wake.

Order matters. The event and its pending wake are made durable BEFORE any
task exists. `reconcile_pending_wakes`, scheduled every 2 minutes, enqueues
PENDING events (crash between storing and enqueueing) and re-arms ENQUEUED
events whose tasks have outlived the queue's retry window without the event
being consumed. Each re-arm bumps `wake_attempt`, and task names are a hash
of (provider, account, message id, attempt), so distinct events never share a
task name and a re-arm is not swallowed by a deleted task's reserved name.
Event state only moves forward: PENDING -> ENQUEUED -> CONSUMED.

Attribution: an event is accepted for a mission only if its thread token maps
to that mission AND its sender resolves to the mission's contact. Opt-outs
are recorded here, from any attributable sender, whatever the state of the
mission, so a "stop" after completion still suppresses the contact.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac

from google.api_core import exceptions as gexc
from google.cloud import firestore

from . import contacts, tasks, trace
from .clock import wall_now
from .coordination import MISSIONS, doc_id

INBOUND = "inbound_events"
THREADS = "threads"
SUPPRESSION = "suppressed_contacts"
ADVANCING = ("PENDING", "ENQUEUED", "CONSUMED")
REARM_AFTER = dt.timedelta(hours=3)  # longer than the queue's ~2 h retry window
REQUIRED = {"provider": str, "account": str, "provider_message_id": str, "thread_token": str,
            "from": str, "body": str}


def signature(secret: str, raw: bytes) -> str:
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def verify(secret: str, raw: bytes, given: str | None) -> bool:
    return bool(secret) and bool(given) and hmac.compare_digest(signature(secret, raw), given)


def event_id(payload: dict) -> str:
    """Namespaced by provider and account: message ids are only unique there."""
    return doc_id(f"{payload['provider']}:{payload['account']}:{payload['provider_message_id']}")


def wake_task_name(ev_id: str, attempt: int) -> str:
    return "wake-" + hashlib.sha256(f"{ev_id}#{attempt}".encode()).hexdigest()[:48]


def validate(payload: dict) -> list[str]:
    errs = [f"{k} missing or not {t.__name__}" for k, t in REQUIRED.items()
            if not isinstance(payload.get(k), t) or not payload.get(k)]
    if len(payload.get("body", "")) > 20_000:
        errs.append("body too long")
    return errs


def _suppress(db, contact_id: str, ev_id: str) -> None:
    db.collection(SUPPRESSION).document(doc_id(contact_id)).set(
        {"contact_id": contact_id, "source_event": ev_id, "at": wall_now()})


def accept(db: firestore.Client, payload: dict) -> dict:
    from .runtime import is_stop_request  # local import: runtime imports this module's names

    errs = validate(payload)
    if errs:
        return {"status": "rejected", "errors": errs}
    ev_id = event_id(payload)
    try:
        sender = contacts.resolve(db, payload["from"])
    except ValueError:
        sender = None
    if sender and sender.known and is_stop_request(payload["body"]):
        _suppress(db, sender.contact_id, ev_id)  # whatever the mission's state

    thread = db.collection(THREADS).document(payload["thread_token"]).get()
    mission = (db.collection(MISSIONS).document(thread.get("mission_id")).get().to_dict()
               if thread.exists else None)
    reason = None
    if mission is None:
        reason = "unknown thread"
    elif sender is None or sender.contact_id != mission.get("contact_id"):
        reason = "sender does not resolve to the mission's contact"
    if reason:
        db.collection("unmatched_inbound").document(ev_id).set(
            {**payload, "reason": reason, "received_at": wall_now()})
        return {"status": "unmatched", "reason": reason,
                "suppressed": bool(sender and sender.known and is_stop_request(payload["body"]))}

    mission_id = mission["mission_id"]
    ref = db.collection(INBOUND).document(ev_id)
    try:
        ref.create({"mission_id": mission_id, "event_id": ev_id, "provider": payload["provider"],
                    "account": payload["account"], "provider_message_id": payload["provider_message_id"],
                    "from": payload["from"], "contact_id": sender.contact_id, "body": payload["body"],
                    "simulated_received_at": payload.get("simulated_received_at"),
                    "received_at": wall_now(), "wake_state": "PENDING", "wake_attempt": 0})
    except gexc.AlreadyExists:
        trace.record(db, mission_id, "inbound.duplicate", event_id=ev_id)
        return {"status": "duplicate", "mission_id": mission_id}
    trace.record(db, mission_id, "inbound.recorded", event_id=ev_id,
                 simulated_received_at=payload.get("simulated_received_at"))
    return _wake(db, ref, mission_id, ev_id, attempt=0)


def _advance_to_enqueued(db, ref, attempt: int, task: str) -> str:
    """Monotonic: never moves a CONSUMED event back, never overwrites a newer attempt."""
    @firestore.transactional
    def _tx(tx):
        cur = ref.get(transaction=tx).to_dict()
        if cur["wake_state"] == "CONSUMED" or cur.get("wake_attempt", 0) > attempt:
            return cur["wake_state"]
        tx.update(ref, {"wake_state": "ENQUEUED", "task": task, "enqueued_at": wall_now()})
        return "ENQUEUED"

    return _tx(db.transaction())


def _wake(db, ref, mission_id: str, ev_id: str, attempt: int) -> dict:
    try:
        name = tasks.enqueue("/tasks/resume", {"mission_id": mission_id, "event_id": ev_id},
                             name=wake_task_name(ev_id, attempt))
    except Exception as e:  # noqa: BLE001 - the scheduled reconciler picks it up
        trace.record(db, mission_id, "wake.enqueue_failed", error=str(e)[:200])
        return {"status": "recorded", "mission_id": mission_id, "wake": "pending"}
    state = _advance_to_enqueued(db, ref, attempt, name)
    trace.record(db, mission_id, "wake.enqueued", task=name.rsplit("/", 1)[-1], state=state)
    return {"status": "recorded", "mission_id": mission_id, "wake": state.lower()}


def reconcile_pending_wakes(db: firestore.Client, older_than_s: int = 30) -> list[str]:
    now = wall_now()
    done = []
    q = db.collection(INBOUND).where(filter=firestore.FieldFilter("wake_state", "in", ["PENDING", "ENQUEUED"]))
    for snap in q.stream():
        d = snap.to_dict()
        if d["wake_state"] == "PENDING" and d["received_at"] <= now - dt.timedelta(seconds=older_than_s):
            _wake(db, snap.reference, d["mission_id"], snap.id, d.get("wake_attempt", 0))
            done.append(snap.id)
        elif d["wake_state"] == "ENQUEUED" and d.get("enqueued_at") and d["enqueued_at"] <= now - REARM_AFTER:
            mission = db.collection(MISSIONS).document(d["mission_id"]).get().to_dict() or {}
            if mission.get("status") != "WAITING_REPLY":
                continue
            attempt = d.get("wake_attempt", 0) + 1

            @firestore.transactional
            def _bump(tx, ref=snap.reference, attempt=attempt):
                cur = ref.get(transaction=tx).to_dict()
                if cur["wake_state"] != "ENQUEUED":
                    return False
                tx.update(ref, {"wake_attempt": attempt, "wake_state": "PENDING"})
                return True

            if _bump(db.transaction()):
                _wake(db, snap.reference, d["mission_id"], snap.id, attempt)
                done.append(snap.id)
    return done
