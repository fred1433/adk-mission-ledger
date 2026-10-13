"""The action boundary: propose, gate, dispatch.

The agent can only PROPOSE. A proposal is persisted with a durable business
action id (mission id + declared purpose) before anything else happens, so an
at-least-once tool re-execution, or a regenerated model turn proposing the
same purpose, lands on the same record instead of creating a second send.

A privileged dispatcher, outside the agent's tool list, executes only an
APPROVED action whose approval digest still matches the stored payload,
re-checking contact ownership, suppression and the fencing generation right
before calling the adapter.

Outcomes after a crash between provider acceptance and receipt recording:
  * adapter honours idempotency  -> retry with the same key, no second effect
  * adapter gives no guarantee    -> NEEDS_RECONCILIATION, never a blind resend
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any

from google.cloud import firestore

from . import coordination, trace
from .clock import wall_now
from .coordination import Lease

ACTIONS = "actions"
SUPPRESSION = "suppressed_contacts"

TERMINAL = {"SENT", "REJECTED", "NEEDS_REVIEW", "NEEDS_RECONCILIATION", "BLOCKED"}


class DispatchRefused(Exception):
    pass


def action_id(mission_id: str, purpose: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,40}", purpose):
        raise ValueError(f"invalid purpose {purpose!r}")
    return f"{mission_id}.{purpose}"


def payload_digest(a: dict) -> str:
    bound = {k: a.get(k) for k in ("channel", "contact_id", "recipient", "body",
                                   "policy_version", "persona_sha", "skills_sha")}
    return hashlib.sha256(json.dumps(bound, sort_keys=True).encode()).hexdigest()


def record_proposal(db: firestore.Client, lease: Lease, *, purpose: str, channel: str,
                    recipient: str, contact_id: str, body: str, note: str,
                    allowed_purposes: list[str], policy_version: str,
                    persona_sha: str, skills_sha: str, fc_ref: dict) -> tuple[dict, bool]:
    """Persist the proposal (fenced). Returns (action, created)."""
    if purpose not in allowed_purposes:
        raise ValueError(f"purpose {purpose!r} not declared by the loaded skills {allowed_purposes}")
    aid = action_id(lease.mission_id, purpose)
    ref = db.collection(ACTIONS).document(aid)

    def _w(tx, _mission):
        snap = ref.get(transaction=tx)
        if snap.exists:
            return snap.to_dict(), False
        doc = {
            "action_id": aid, "mission_id": lease.mission_id, "purpose": purpose,
            "channel": channel, "recipient": recipient, "contact_id": contact_id,
            "body": body, "model_note": note, "policy_version": policy_version,
            "persona_sha": persona_sha, "skills_sha": skills_sha,
            "status": "PROPOSED", "proposed_at": wall_now(),
            "proposed_generation": lease.generation, "first_tool_call": fc_ref,
            "attempts": 0,
        }
        tx.set(ref, doc)
        return doc, True

    return coordination.fenced(db, lease, _w)


def set_verdict(db: firestore.Client, lease: Lease, aid: str, verdict: dict) -> dict:
    ref = db.collection(ACTIONS).document(aid)

    def _w(tx, _m):
        a = ref.get(transaction=tx).to_dict()
        if a["status"] != "PROPOSED":
            return a
        update: dict[str, Any] = {"gate": verdict, "gated_at": wall_now()}
        if verdict["decision"] == "APPROVE":
            update["status"] = "APPROVED"
            update["approval"] = {"digest": payload_digest(a), "policy_version": a["policy_version"],
                                  "reviewer_mode": verdict.get("reviewer_mode"),
                                  "generation": lease.generation, "approved_at": wall_now()}
        else:
            update["status"] = "REJECTED" if verdict["decision"] == "REJECT" else "NEEDS_REVIEW"
        tx.update(ref, update)
        return {**a, **update}

    return coordination.fenced(db, lease, _w)


def is_suppressed(db: firestore.Client, contact_id: str, tx=None) -> bool:
    ref = db.collection(SUPPRESSION).document(contact_id.replace("/", "_"))
    return (ref.get(transaction=tx) if tx else ref.get()).exists


def dispatch(db: firestore.Client, lease: Lease, aid: str, adapter, thread_token: str) -> dict:
    """Execute one approved action. Idempotent; never resends blindly."""
    ref = db.collection(ACTIONS).document(aid)

    def _claim(tx, _m):
        snap = ref.get(transaction=tx)
        if not snap.exists:
            raise DispatchRefused(f"{aid}: no such action")
        a = snap.to_dict()
        if a["status"] == "SENT":
            return a, "already-sent"
        if a["status"] not in ("APPROVED", "DISPATCHING"):
            raise DispatchRefused(f"{aid}: status {a['status']} is not dispatchable")
        appr = a.get("approval") or {}
        if appr.get("digest") != payload_digest(a):
            tx.update(ref, {"status": "BLOCKED", "blocked_reason": "payload changed after approval"})
            return {**a, "status": "BLOCKED"}, "digest-mismatch"
        if not coordination.reservation_held_by(db, a["contact_id"], a["mission_id"], tx):
            tx.update(ref, {"status": "BLOCKED", "blocked_reason": "contact reservation not held"})
            return {**a, "status": "BLOCKED"}, "reservation-lost"
        if is_suppressed(db, a["contact_id"], tx):
            tx.update(ref, {"status": "BLOCKED", "blocked_reason": "contact suppressed"})
            return {**a, "status": "BLOCKED"}, "suppressed"
        if a["status"] == "DISPATCHING" and not adapter.supports_idempotency:
            tx.update(ref, {"status": "NEEDS_RECONCILIATION",
                            "reconciliation_reason": "earlier attempt may have been accepted; "
                                                     "adapter has no idempotency guarantee"})
            return {**a, "status": "NEEDS_RECONCILIATION"}, "reconcile"
        mode = "retry-same-key" if a["status"] == "DISPATCHING" else "first-attempt"
        tx.update(ref, {"status": "DISPATCHING", "attempts": firestore.Increment(1),
                        "adapter": adapter.name, "dispatch_generation": lease.generation,
                        "dispatch_started_at": wall_now()})
        return a, mode

    a, mode = coordination.fenced(db, lease, _claim)
    trace.record(db, lease.mission_id, "dispatch.claim", action_id=aid, mode=mode,
                 generation=lease.generation, adapter=adapter.name)
    if mode not in ("first-attempt", "retry-same-key"):
        return {**a, "dispatch_mode": mode}

    receipt = adapter.send(idempotency_key=aid, to=a["recipient"], body=a["body"],
                           thread_token=thread_token, mission_id=a["mission_id"])
    trace.record(db, lease.mission_id, "provider.accepted", action_id=aid,
                 provider_message_id=receipt.provider_message_id, replayed=receipt.replayed)
    if os.environ.get("LEDGER_FAULT") == "after_provider_accept":
        trace.record(db, lease.mission_id, "fault.injected", point="after_provider_accept",
                     action_id=aid)
        os._exit(137)

    # The receipt is a fact about the outside world: record it even if our
    # lease lapsed meanwhile, but only over a DISPATCHING record.
    @firestore.transactional
    def _done(tx):
        cur = ref.get(transaction=tx).to_dict()
        if cur["status"] == "DISPATCHING":
            tx.update(ref, {"status": "SENT", "receipt": receipt.__dict__, "sent_at": wall_now()})
        return cur["status"]

    _done(db.transaction())
    trace.record(db, lease.mission_id, "dispatch.sent", action_id=aid,
                 provider_message_id=receipt.provider_message_id, mode=mode)
    return {**a, "status": "SENT", "receipt": receipt.__dict__, "dispatch_mode": mode}


def approved_unsent(db: firestore.Client, mission_id: str) -> list[str]:
    q = db.collection(ACTIONS).where(filter=firestore.FieldFilter("mission_id", "==", mission_id))
    return sorted(s.id for s in q.stream() if s.get("status") in ("APPROVED", "DISPATCHING"))
