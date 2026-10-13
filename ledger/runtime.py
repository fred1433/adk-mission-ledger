"""Mission lifecycle: start a segment, park, resume a segment, finish.

Each handler is a bounded segment: take the execution lease, equip an empty
agent, run until it parks or finishes, dispatch approved actions, write the
new state, release the lease, discard the agent. Nothing is kept in process
memory between segments.
"""

from __future__ import annotations

import datetime as dt
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
from .coordination import MISSIONS, ContactReserved

SESSION_USER = "mission-runner"
THREADS = "threads"
INBOUND = "inbound_events"
_LOCAL_SESSIONS = InMemorySessionService()


def session_service():
    s = settings()
    if s.agent_engine_id:
        return VertexAiSessionService(project=s.project, location=s.location,
                                      agent_engine_id=s.agent_engine_id)
    return _LOCAL_SESSIONS  # unit tests only; traces record which backend ran


def _worker_id() -> str:
    p = trace.process_identity()
    return f"{p.get('revision') or 'local'}/{p.get('instance') or p.get('host')}/{p['pid']}/{uuid.uuid4().hex[:6]}"


def _mission(db, mission_id: str) -> dict:
    row = missions_bq.fetch_mission(mission_id)
    row["contact_id"] = contacts.resolve(db, row["contact_alias"]).contact_id
    row["contact_known"] = contacts.resolve(db, row["contact_alias"]).known
    return row


async def _segment(ctx: MissionCtx, model, session_id: str, message: types.Content,
                   invocation_id: str | None) -> dict:
    app, tool_names = agent_factory.build(ctx, model)
    runner = Runner(app=app, session_service=session_service())
    mid = ctx.mission["mission_id"]
    trace.record(ctx.db, mid, "agent.equipped", tools=tool_names, model=model.model,
                 persona=ctx.manifest["persona"], skills=ctx.manifest["skills"],
                 resuming_invocation=invocation_id)
    pending, final_text, inv = None, None, invocation_id
    async for ev in runner.run_async(user_id=SESSION_USER, session_id=session_id,
                                     invocation_id=invocation_id, new_message=message):
        if inv is None and ev.invocation_id:
            inv = ev.invocation_id
            ref = ctx.db.collection(MISSIONS).document(mid)
            coordination.fenced(ctx.db, ctx.lease, lambda tx, _d: tx.update(ref, {"invocation_id": inv}))
        for fc in ev.get_function_calls():
            trace.record(ctx.db, mid, "agent.call", name=fc.name, invocation_id=ev.invocation_id,
                         function_call_id=fc.id)
            if ev.long_running_tool_ids and fc.id in ev.long_running_tool_ids:
                pending = fc.id
        for part in (ev.content.parts if ev.content else []) or []:
            if part.text and not part.thought:
                final_text = part.text
    del runner, app  # the agent is discarded with its segment
    trace.record(ctx.db, mid, "agent.discarded", invocation_id=inv)
    return {"paused": pending is not None, "pending_call_id": pending,
            "invocation_id": inv, "final_text": final_text}


def _dispatch_all(ctx: MissionCtx) -> list[dict]:
    out = []
    for aid in actions.approved_unsent(ctx.db, ctx.mission["mission_id"]):
        channel = ctx.db.collection(actions.ACTIONS).document(aid).get().get("channel")
        out.append(actions.dispatch(ctx.db, ctx.lease, aid,
                                    adapters.for_channel(ctx.db, channel), ctx.thread_token))
    return out


async def start(mission_id: str) -> dict:
    db = coordination.client(settings().project)
    s = settings()
    lease = coordination.acquire_lease(db, mission_id, _worker_id(), s.lease_seconds)
    mref = db.collection(MISSIONS).document(mission_id)
    doc = mref.get().to_dict() or {}
    if doc.get("status") not in (None, "QUEUED"):
        coordination.release(db, lease, {})
        trace.record(db, mission_id, "start.noop", status=doc.get("status"))
        return {"mission_id": mission_id, "noop": True, "status": doc.get("status")}

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
        missions_bq.report(mission_id, "BLOCKED_CONTACT_RESERVED", {"holder": e.holder})
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
                      "contact_id": mission["contact_id"], "session_id": sess.id,
                      "manifest": pinned, "thread_token": thread_token, "started_at": wall_now()},
               merge=True)
        tx.set(db.collection(THREADS).document(thread_token), {"mission_id": mission_id})

    coordination.fenced(db, lease, _w)
    trace.record(db, mission_id, "manifest.pinned", manifest=pinned)

    ctx = MissionCtx(db, lease, mission, pinned, thread_token)
    prompt = types.Content(role="user", parts=[types.Part(text=mission["prompt"])])
    seg = await _segment(ctx, model, sess.id, prompt, None)
    sent = _dispatch_all(ctx)
    return _close_segment(ctx, seg, sent, event_ref=None)


async def resume(mission_id: str, event_id: str) -> dict:
    db = coordination.client(settings().project)
    s = settings()
    lease = coordination.acquire_lease(db, mission_id, _worker_id(), s.lease_seconds)
    mref = db.collection(MISSIONS).document(mission_id)
    doc = mref.get().to_dict() or {}
    eref = db.collection(INBOUND).document(event_id)
    ev = eref.get().to_dict() or {}
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

    coordination.fenced(db, lease, lambda tx, _d: tx.update(mref, {"status": "RESUMING"}))
    ctx = MissionCtx(db, lease, mission, doc["manifest"], doc["thread_token"])
    reply = types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(
        id=doc["pending_call_id"], name="wait_for_reply",
        response={"reply": ev["body"], "from": ev["from"],
                  "received_at_simulated": ev.get("simulated_received_at")}))])
    trace.record(db, mission_id, "resume.start", event_id=event_id,
                 invocation_id=doc["invocation_id"], pending_call_id=doc["pending_call_id"],
                 parked_by=doc.get("parked_by"))
    seg = await _segment(ctx, model, doc["session_id"], reply, doc["invocation_id"])
    sent = _dispatch_all(ctx)
    return _close_segment(ctx, seg, sent, event_ref=eref)


def _close_segment(ctx: MissionCtx, seg: dict, sent: list[dict], event_ref) -> dict:
    db, mid = ctx.db, ctx.mission["mission_id"]
    statuses = {a["action_id"]: a["status"] for a in sent}
    if any(st in ("NEEDS_RECONCILIATION", "BLOCKED") for st in statuses.values()):
        new_status = "NEEDS_REVIEW"
    elif seg["paused"]:
        new_status = "WAITING_REPLY"
    else:
        rejected = [d.id for d in db.collection(actions.ACTIONS)
                    .where(filter=firestore.FieldFilter("mission_id", "==", mid)).stream()
                    if d.get("status") in ("REJECTED", "NEEDS_REVIEW")]
        new_status = "NEEDS_REVIEW" if rejected else "DONE"
    update = {"status": new_status, "last_segment_text": seg["final_text"],
              "invocation_id": seg["invocation_id"]}
    if seg["paused"]:
        update.update(pending_call_id=seg["pending_call_id"], parked_at=wall_now(),
                      parked_by=trace.process_identity())
    ref = db.collection(MISSIONS).document(mid)

    def _w(tx, _d):
        if event_ref is not None:
            event_ref.get(transaction=tx)
            tx.update(event_ref, {"wake_state": "CONSUMED", "consumed_at": wall_now(),
                                  "consumed_generation": ctx.lease.generation})
        tx.update(ref, {**update, "lease.owner": None, "lease.expires_at": None,
                        "updated_at": wall_now()})

    coordination.fenced(db, ctx.lease, _w)
    if new_status in ("DONE",):
        coordination.release_contact(db, ctx.mission["contact_id"], mid, reason="mission closed")
    trace.record(db, mid, f"mission.{new_status.lower()}", dispatched=statuses,
                 generation=ctx.lease.generation, final_text=seg["final_text"])
    missions_bq.report(mid, new_status, {"dispatched": statuses, "generation": ctx.lease.generation})
    return {"mission_id": mid, "status": new_status, "dispatched": statuses}
