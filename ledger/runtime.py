"""Mission lifecycle: start a segment, park, resume a segment, finish.

Each handler is a bounded segment: take the execution lease, equip an empty
agent, run until it parks or finishes, dispatch approved actions, write the
new state, release the lease, discard the agent. Nothing is kept in process
memory between segments. The lease is renewed on every ADK event and before
each dispatch. Any exception inside a segment closes the mission as
NEEDS_REVIEW under the same lease, so a crashed model call never leaves a
mission RUNNING; if the process itself dies, `sweep_interrupted` (scheduled)
finds the expired lease and does the same.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService, VertexAiSessionService
from google.cloud import firestore
from google.genai import types

from . import (actions, adapters, agent_factory, contacts, coordination, manifest,
               missions_bq, models, trace)
from .agent_factory import APP_NAME, MissionCtx
from .clock import wall_now
from .config import settings
from .coordination import MISSIONS, ContactReserved, FencingError

SESSION_USER = "mission-runner"
THREADS = "threads"
INBOUND = "inbound_events"
INTERRUPTED = {"INITIALIZING", "RUNNING", "RESUMING"}
STOP_REQUEST = re.compile(r"^\s*(stop|unsubscribe|remove me|do not contact)\b", re.I)
_LOCAL_SESSIONS = InMemorySessionService()


def session_service():
    s = settings()
    if s.agent_engine_id:
        return VertexAiSessionService(project=s.project, location=s.location,
                                      agent_engine_id=s.agent_engine_id)
    return _LOCAL_SESSIONS  # unit tests only; traces record which backend ran


def is_stop_request(body: str) -> bool:
    return bool(STOP_REQUEST.search(body or ""))


def _worker_id() -> str:
    p = trace.process_identity()
    return f"{p.get('revision') or 'local'}/{p.get('instance') or p.get('host')}/{p['pid']}/{uuid.uuid4().hex[:6]}"


def _mission(db, mission_id: str) -> dict:
    row = missions_bq.fetch_mission(mission_id)
    ref = contacts.resolve(db, row["contact_alias"])
    row["contact_id"], row["contact_known"] = ref.contact_id, ref.known
    return row


def _renew(ctx: MissionCtx) -> None:
    ctx.lease = coordination.renew_if_due(ctx.db, ctx.lease, settings().lease_seconds)


async def _segment(ctx: MissionCtx, model, session_id: str, message: types.Content,
                   invocation_id: str | None) -> dict:
    app, tool_names = agent_factory.build(ctx, model)
    runner = Runner(app=app, session_service=session_service())
    mid = ctx.mission["mission_id"]
    trace.record(ctx.db, mid, "agent.equipped", tools=tool_names, model=model.model,
                 persona=ctx.manifest["persona"], skills=ctx.manifest["skills"],
                 resuming_invocation=invocation_id)
    pending, final_text, inv, model_error = None, None, invocation_id, None
    async for ev in runner.run_async(user_id=SESSION_USER, session_id=session_id,
                                     invocation_id=invocation_id, new_message=message):
        _renew(ctx)
        if inv is None and ev.invocation_id:
            inv = ev.invocation_id
            ref = ctx.db.collection(MISSIONS).document(mid)
            coordination.fenced(ctx.db, ctx.lease, lambda tx, _d: tx.update(ref, {"invocation_id": inv}))
        for fc in ev.get_function_calls():
            trace.record(ctx.db, mid, "agent.call", name=fc.name, invocation_id=ev.invocation_id,
                         function_call_id=fc.id)
            if ev.long_running_tool_ids and fc.id in ev.long_running_tool_ids:
                pending = fc.id
        if ev.usage_metadata:
            u = ev.usage_metadata
            trace.record(ctx.db, mid, "model.usage", source="agent", model=model.model,
                         prompt=u.prompt_token_count, cached=u.cached_content_token_count,
                         output=u.candidates_token_count, thoughts=u.thoughts_token_count)
        if ev.error_code:
            model_error = f"{ev.error_code}: {ev.error_message or ''}".strip()
            trace.record(ctx.db, mid, "agent.model_error", error=model_error)
        for part in (ev.content.parts if ev.content else []) or []:
            if part.text and not part.thought:
                final_text = part.text
    del runner, app  # the agent is discarded with its segment
    trace.record(ctx.db, mid, "agent.discarded", invocation_id=inv)
    return {"paused": pending is not None, "pending_call_id": pending,
            "invocation_id": inv, "final_text": final_text, "model_error": model_error}


def _dispatch_all(ctx: MissionCtx) -> list[dict]:
    out = []
    for aid in actions.approved_unsent(ctx.db, ctx.mission["mission_id"]):
        _renew(ctx)
        channel = ctx.db.collection(actions.ACTIONS).document(aid).get().get("channel")
        out.append(actions.dispatch(ctx.db, ctx.lease, aid,
                                    adapters.for_channel(ctx.db, channel), ctx.thread_token))
    return out


async def _guarded(ctx: MissionCtx, model, session_id, message, invocation_id, event_ref) -> dict:
    """Run segment + dispatch; any failure closes the mission for review."""
    try:
        seg = await _segment(ctx, model, session_id, message, invocation_id)
        sent = _dispatch_all(ctx)
    except FencingError:
        raise  # we are no longer the owner: write nothing more
    except Exception as e:  # noqa: BLE001 - model, tool or provider failure
        err = f"{type(e).__name__}: {str(e)[:300]}"
        trace.record(ctx.db, ctx.mission["mission_id"], "segment.failed", error=err,
                     generation=ctx.lease.generation)
        coordination.release(ctx.db, ctx.lease, {"status": "NEEDS_REVIEW", "reason": err})
        missions_bq.report(ctx.mission["mission_id"], "NEEDS_REVIEW", {"segment_failed": err}, db=ctx.db)
        return {"mission_id": ctx.mission["mission_id"], "status": "NEEDS_REVIEW", "error": err}
    return _close_segment(ctx, seg, sent, event_ref)


async def start(mission_id: str) -> dict:
    db = coordination.client(settings().project)
    s = settings()
    lease = coordination.acquire_lease(db, mission_id, _worker_id(), s.lease_seconds,
                                       init_status="INITIALIZING")
    mref = db.collection(MISSIONS).document(mission_id)
    prev = lease.previous_status
    if prev in INTERRUPTED:
        return _interrupted(db, lease, {"status": prev})
    if prev not in (None, "QUEUED"):
        coordination.release(db, lease, {})
        trace.record(db, mission_id, "start.noop", status=prev)
        return {"mission_id": mission_id, "noop": True, "status": prev}
    try:
        return await _start_initialized(db, s, lease, mref, mission_id)
    except (FencingError, coordination.LeaseUnavailable):
        raise
    except Exception as e:  # noqa: BLE001 - initialisation failed: review, not limbo
        err = f"{type(e).__name__}: {str(e)[:300]}"
        trace.record(db, mission_id, "init.failed", error=err, generation=lease.generation)
        coordination.release(db, lease, {"status": "NEEDS_REVIEW", "reason": err})
        missions_bq.report(mission_id, "NEEDS_REVIEW", {"init_failed": err}, db=db)
        return {"mission_id": mission_id, "status": "NEEDS_REVIEW", "error": err}


async def _start_initialized(db, s, lease, mref, mission_id: str) -> dict:
    mission = _mission(db, mission_id)
    trace.record(db, mission_id, "lease.acquired", generation=lease.generation)
    timeout = dt.timedelta(hours=s.reply_wait_policy_hours + 24)
    try:
        res = coordination.reserve_contact(db, mission["contact_id"], mission_id, timeout)
    except ContactReserved as e:
        coordination.release(db, lease, {"status": "BLOCKED_CONTACT_RESERVED", "blocked_by": e.holder,
                                         "contact_id": mission["contact_id"]})
        trace.record(db, mission_id, "reservation.refused", contact_id=e.contact_id, holder=e.holder,
                     via_alias=mission["contact_alias"])
        missions_bq.report(mission_id, "BLOCKED_CONTACT_RESERVED", {"holder": e.holder}, db=db)
        return {"mission_id": mission_id, "status": "BLOCKED_CONTACT_RESERVED", "holder": e.holder}
    trace.record(db, mission_id, "reservation.held", contact_id=mission["contact_id"],
                 business_expires_at=res["business_expires_at"].isoformat(),
                 contact_known=mission["contact_known"])

    model, descriptor = models.for_mission(mission)
    sess = await session_service().create_session(app_name=APP_NAME, user_id=SESSION_USER,
                                                  ttl=f"{s.session_ttl_seconds}s") \
        if s.agent_engine_id else await session_service().create_session(app_name=APP_NAME, user_id=SESSION_USER)
    pinned = manifest.build(mission, descriptor, sess.id)
    thread_token = "thr-" + uuid.uuid4().hex[:16]

    def _w(tx, _d):
        tx.set(mref, {"mission_id": mission_id, "status": "RUNNING", "kind": mission["kind"],
                      "contact_id": mission["contact_id"], "session_id": sess.id, "turn": 0,
                      "manifest": pinned, "thread_token": thread_token, "started_at": wall_now()},
               merge=True)
        tx.set(db.collection(THREADS).document(thread_token), {"mission_id": mission_id})

    coordination.fenced(db, lease, _w)
    trace.record(db, mission_id, "manifest.pinned", manifest=pinned)

    ctx = MissionCtx(db, lease, mission, pinned, thread_token)
    prompt = types.Content(role="user", parts=[types.Part(text=mission["prompt"])])
    return await _guarded(ctx, model, sess.id, prompt, None, None)


async def resume(mission_id: str, event_id: str) -> dict:
    db = coordination.client(settings().project)
    s = settings()
    lease = coordination.acquire_lease(db, mission_id, _worker_id(), s.lease_seconds)
    mref = db.collection(MISSIONS).document(mission_id)
    doc = mref.get().to_dict() or {}
    eref = db.collection(INBOUND).document(event_id)
    ev = eref.get().to_dict() or {}
    if doc.get("status") in INTERRUPTED:
        return _interrupted(db, lease, doc)
    if ev and ev.get("mission_id") != mission_id:
        coordination.release(db, lease, {})
        trace.record(db, mission_id, "resume.refused", event_id=event_id,
                     reason=f"event belongs to {ev.get('mission_id')}")
        return {"mission_id": mission_id, "noop": True, "refused": "event belongs to another mission"}
    if ev.get("wake_state") == "CONSUMED" or doc.get("status") != "WAITING_REPLY":
        coordination.release(db, lease, {})
        trace.record(db, mission_id, "resume.noop", event_id=event_id,
                     event_state=ev.get("wake_state"), mission_status=doc.get("status"),
                     generation=lease.generation)
        return {"mission_id": mission_id, "noop": True}

    mission = _mission(db, mission_id)
    trace.record(db, mission_id, "lease.acquired", generation=lease.generation)
    if not coordination.reservation_held_by(db, doc["contact_id"], mission_id):
        coordination.release(db, lease, {"status": "NEEDS_RECONCILIATION",
                                         "reason": "reply arrived after the reservation ended"})
        trace.record(db, mission_id, "resume.reconcile", event_id=event_id)
        return {"mission_id": mission_id, "status": "NEEDS_RECONCILIATION"}

    model, descriptor = models.for_mission(mission)
    mismatches = manifest.verify(doc["manifest"], mission, descriptor)
    trace.record(db, mission_id, "manifest.verified", mismatches=mismatches)
    if mismatches:
        coordination.release(db, lease, {"status": "NEEDS_REVIEW", "manifest_mismatch": mismatches})
        return {"mission_id": mission_id, "status": "NEEDS_REVIEW", "mismatches": mismatches}

    # One more reply consumed: new turn, so the same purpose may be used again.
    coordination.fenced(db, lease, lambda tx, _d: tx.update(
        mref, {"status": "RESUMING", "turn": firestore.Increment(1)}))
    ctx = MissionCtx(db, lease, mission, doc["manifest"], doc["thread_token"])
    # The reviewer sees what the contact actually wrote, as CUSTOMER STATEMENTS:
    # they can establish a choice or preference, never a commitment the
    # business facts do not grant (live run 3 rejected a correct confirmation
    # because the reviewer saw only the original facts).
    mission["customer_statements"] = [{"from": ev["from"], "body": ev["body"],
                                       "attributed_contact": ev.get("contact_id")}]
    reply = types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(
        id=doc["pending_call_id"], name="wait_for_reply",
        response={"reply": ev["body"], "from": ev["from"],
                  "received_at_simulated": ev.get("simulated_received_at")}))])
    trace.record(db, mission_id, "resume.start", event_id=event_id,
                 invocation_id=doc["invocation_id"], pending_call_id=doc["pending_call_id"],
                 parked_by=doc.get("parked_by"))
    return await _guarded(ctx, model, doc["session_id"], reply, doc["invocation_id"], eref)


def _interrupted(db, lease, doc: dict) -> dict:
    """We hold a fresh lease, so the previous segment's lease expired while it
    was RUNNING or RESUMING: that worker died mid-segment. Do not guess what
    it did; stop for a human. Approved actions stay idempotent if retried.
    """
    coordination.release(db, lease, {"status": "NEEDS_REVIEW",
                                     "reason": f"segment interrupted while {doc['status']}"})
    trace.record(db, lease.mission_id, "segment.interrupted", previous_status=doc["status"],
                 generation=lease.generation)
    missions_bq.report(lease.mission_id, "NEEDS_REVIEW", {"interrupted": doc["status"]}, db=db)
    return {"mission_id": lease.mission_id, "status": "NEEDS_REVIEW"}


def sweep_interrupted(db: firestore.Client) -> list[dict]:
    """Scheduled. Send every RUNNING/RESUMING mission whose lease has expired
    to review. Lease acquisition is the test: it only succeeds once the old
    owner's lease is over, so a live segment is never touched."""
    out = []
    q = db.collection(MISSIONS).where(filter=firestore.FieldFilter("status", "in", sorted(INTERRUPTED)))
    for snap in q.stream():
        lease_doc = (snap.to_dict().get("lease") or {})
        exp = lease_doc.get("expires_at")
        if exp is not None and exp > wall_now():
            continue
        try:
            lease = coordination.acquire_lease(db, snap.id, "sweeper/" + uuid.uuid4().hex[:6], 60)
        except coordination.LeaseUnavailable:
            continue
        if lease.previous_status in INTERRUPTED:
            out.append(_interrupted(db, lease, {"status": lease.previous_status}))
        else:
            coordination.release(db, lease, {})
    missions_bq.flush_outbox(db)
    return out


def _close_segment(ctx: MissionCtx, seg: dict, sent: list[dict], event_ref) -> dict:
    db, mid = ctx.db, ctx.mission["mission_id"]
    statuses = {a["action_id"]: a["status"] for a in sent}
    mref = db.collection(MISSIONS).document(mid)
    awaited = (mref.get().to_dict() or {}).get("awaited_action_id") if seg["paused"] else None
    awaited_status = (db.collection(actions.ACTIONS).document(awaited).get().to_dict() or {}).get("status") \
        if awaited else None
    reason = None
    if any(st in ("NEEDS_RECONCILIATION", "BLOCKED") for st in statuses.values()):
        new_status, reason = "NEEDS_REVIEW", "dispatch did not complete"
    elif seg["paused"]:
        # Only wait for an answer to a message that was actually sent.
        if awaited_status == "SENT":
            new_status = "WAITING_REPLY"
        else:
            new_status, reason = "NEEDS_REVIEW", f"waiting on {awaited} which is {awaited_status}"
    else:
        mine = [d.to_dict() for d in db.collection(actions.ACTIONS)
                .where(filter=firestore.FieldFilter("mission_id", "==", mid)).stream()]
        latest = {}
        for a in sorted(mine, key=lambda a: (a.get("turn", 0), a.get("revision", 1))):
            latest[(a["purpose"], a.get("turn", 0))] = a
        # A segment that ends without any sent action, with an unresolved
        # proposal, or on a model error, is not a finished mission.
        done = bool(latest) and all(a["status"] == "SENT" for a in latest.values()) \
            and not seg.get("model_error")
        new_status = "DONE" if done else "NEEDS_REVIEW"
    update = {"status": new_status, "last_segment_text": seg["final_text"],
              "invocation_id": seg["invocation_id"], "close_reason": reason}
    if new_status == "WAITING_REPLY":
        update.update(pending_call_id=seg["pending_call_id"], parked_at=wall_now(),
                      parked_by=trace.process_identity())
    res_ref = db.collection(coordination.RESERVATIONS).document(
        coordination.doc_id(ctx.mission["contact_id"]))

    def _w(tx, _d):
        # all reads first
        res = res_ref.get(transaction=tx).to_dict() if new_status == "DONE" else None
        if event_ref is not None:
            event_ref.get(transaction=tx)
        if event_ref is not None:
            tx.update(event_ref, {"wake_state": "CONSUMED", "consumed_at": wall_now(),
                                  "consumed_generation": ctx.lease.generation})
        tx.update(mref, {**update, "lease.owner": None, "lease.expires_at": None,
                         "updated_at": wall_now()})
        # Closing and releasing the contact happen together, or not at all.
        if res and res.get("mission_id") == mid and res.get("status") == "HELD":
            tx.update(res_ref, {"status": "RELEASED", "released_at": wall_now(),
                                "release_reason": "mission closed"})
        missions_bq.stage_report(tx, db, mid, new_status,
                                 {"dispatched": statuses, "generation": ctx.lease.generation,
                                  "reason": reason})

    coordination.fenced(db, ctx.lease, _w)
    missions_bq.flush_outbox(db)
    trace.record(db, mid, f"mission.{new_status.lower()}", dispatched=statuses,
                 generation=ctx.lease.generation, final_text=seg["final_text"], reason=reason)
    return {"mission_id": mid, "status": new_status, "dispatched": statuses, "reason": reason}
