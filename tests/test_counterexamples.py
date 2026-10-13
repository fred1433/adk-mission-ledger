"""Counterexamples from the external review, one test each, on the Firestore
emulator (no Google Cloud, no model). BigQuery and Cloud Tasks are replaced by
in-process stand-ins.

Run: gcloud emulators firestore start --host-port=127.0.0.1:8787
     FIRESTORE_EMULATOR_HOST=127.0.0.1:8787 pytest tests/test_counterexamples.py
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import socket
import threading
import time
import types as pytypes
import uuid

import pytest

HOST = os.environ.get("FIRESTORE_EMULATOR_HOST", "")


def _emulator_up() -> bool:
    if not HOST:
        return False
    h, p = HOST.split(":")
    try:
        socket.create_connection((h, int(p)), timeout=1).close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _emulator_up(), reason="Firestore emulator not running")


@pytest.fixture(autouse=True)
def env(monkeypatch):
    from ledger import config
    for k in list(os.environ):
        if k.startswith("LEDGER_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("LEDGER_PROJECT", "demo-ledger")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    config.settings.cache_clear()
    from ledger import missions_bq, tasks
    monkeypatch.setattr(missions_bq, "flush_outbox", lambda db, limit=200: 0)
    monkeypatch.setattr(tasks, "enqueue", lambda path, payload, name=None: f"queues/q/tasks/{name}")
    yield
    config.settings.cache_clear()


@pytest.fixture
def db():
    from ledger import coordination
    return coordination.client("demo-ledger")


def rid() -> str:
    return uuid.uuid4().hex[:8]


# ---------------------------------------------------------------- 1. memory

def _cand(db, fact="Prefers afternoon appointments.", scope=None, topic=None):
    from ledger import memory
    return memory.quarantine(db, mission_id=f"m-{rid()}", fact=fact,
                             scope=scope or {"contact_id": f"c-{rid()}"},
                             topic=topic or f"t_{rid()}", evidence="reply")


APPROVE = lambda c: {"decision": "APPROVE", "reasons": ["inline"], "reviewer_mode": "test"}  # noqa: E731


def test_memory_validates_the_stored_record_not_a_caller_copy(db):
    """Reproduced: stored fact holds an email; a benign copy was approved and the email published."""
    from ledger import memory
    c = _cand(db, fact="Contact jordan@example.com for appointments.")
    v = memory.validate(db, c["candidate_id"], 1, reviewer=APPROVE)
    assert v["status"] == "REJECTED" and "no_raw_contact_data" in v["validation"]["reasons"]
    with pytest.raises(PermissionError):
        memory.MemoryPublisher(db, memory.FakeMemoryBank(), "engines/1").publish(c["candidate_id"])


def test_memory_changed_during_review_is_not_approved(db):
    from ledger import memory
    c = _cand(db)

    def reviewer(snapshot):
        db.collection(memory.CANDIDATES).document(c["candidate_id"]).update({"fact": "Prefers mornings."})
        return APPROVE(snapshot)

    v = memory.validate(db, c["candidate_id"], 1, reviewer=reviewer)
    assert v["status"] == "NEEDS_REVIEW" and v["validation"]["stage"] == "changed"


def test_memory_changed_after_approval_is_not_published(db):
    from ledger import memory
    c = _cand(db)
    assert memory.validate(db, c["candidate_id"], 1, reviewer=APPROVE)["status"] == "APPROVED"
    db.collection(memory.CANDIDATES).document(c["candidate_id"]).update({"fact": "Wants a discount."})
    bank = memory.FakeMemoryBank()
    with pytest.raises(PermissionError):
        memory.MemoryPublisher(db, bank, "engines/1").publish(c["candidate_id"])
    assert bank.store == {}
    assert db.collection(memory.CANDIDATES).document(c["candidate_id"]).get().get("status") == "NEEDS_REVIEW"


def test_memory_concurrent_publishers_create_one_memory(db):
    """Reproduced: two overlapping publishers created two memories for one candidate."""
    from ledger import memory

    class SlowBank(memory.FakeMemoryBank):
        def create(self, **kw):
            time.sleep(0.3)
            return super().create(**kw)

    c = _cand(db)
    memory.validate(db, c["candidate_id"], 1, reviewer=APPROVE)
    bank = SlowBank()
    pubs = [memory.MemoryPublisher(db, bank, "engines/1") for _ in range(2)]
    out = []
    ts = [threading.Thread(target=lambda p=p: out.append(p.publish(c["candidate_id"]))) for p in pubs]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(bank.store) == 1
    assert len({o["memory_name"] for o in out}) == 1


def test_memory_replay_after_success_returns_the_same_memory(db):
    from ledger import memory
    c = _cand(db)
    memory.validate(db, c["candidate_id"], 1, reviewer=APPROVE)
    bank = memory.FakeMemoryBank()
    pub = memory.MemoryPublisher(db, bank, "engines/1")
    first, second = pub.publish(c["candidate_id"]), pub.publish(c["candidate_id"])
    assert second["mode"] == "already-published" and first["memory_name"] == second["memory_name"]
    assert len(bank.store) == 1


def test_memory_contradictory_facts_in_sequence(db):
    from ledger import memory
    scope, topic = {"contact_id": f"c-{rid()}"}, f"t_{rid()}"
    a = _cand(db, fact="Prefers mornings.", scope=scope, topic=topic)
    b = _cand(db, fact="Prefers evenings.", scope=scope, topic=topic)
    assert memory.validate(db, a["candidate_id"], 1, reviewer=APPROVE)["status"] == "APPROVED"
    assert memory.validate(db, b["candidate_id"], 1, reviewer=APPROVE)["status"] == "NEEDS_REVIEW"


# ------------------------------------------------------- 2. action ownership

def _approved_action(db, mission_id):
    from ledger import actions, coordination
    cid = f"c-{rid()}"
    coordination.reserve_contact(db, cid, mission_id, dt.timedelta(hours=1))
    lease = coordination.acquire_lease(db, mission_id, "w-" + mission_id, 60)
    a, _ = actions.record_proposal(
        db, lease, purpose="first_contact", channel="email", recipient="a@example.com",
        contact_id=cid, body="Hi, Tuesday at 10:00 or Thursday at 15:00?", note="",
        allowed_purposes=["first_contact"], policy_version="v1", persona_sha="p", skills_sha="s", fc_ref={})
    return lease, a


def test_lease_for_one_mission_cannot_dispatch_another_missions_action(db):
    """Reproduced: a valid lease for mission B sent mission A's approved action."""
    from ledger import actions, adapters, coordination
    lease_a, a = _approved_action(db, f"m-{rid()}-a")
    actions.set_verdict(db, lease_a, a["action_id"], {"decision": "APPROVE", "reasons": [], "stage": "t"})
    lease_b = coordination.acquire_lease(db, f"m-{rid()}-b", "w-b", 60)
    with pytest.raises(actions.DispatchRefused, match="belongs to"):
        actions.dispatch(db, lease_b, a["action_id"], adapters.IdempotentMailbox(db), "thr")


def test_lease_for_one_mission_cannot_set_another_missions_verdict(db):
    from ledger import actions, coordination
    _lease_a, a = _approved_action(db, f"m-{rid()}-a")
    lease_b = coordination.acquire_lease(db, f"m-{rid()}-b", "w-b", 60)
    with pytest.raises(actions.DispatchRefused, match="belongs to"):
        actions.set_verdict(db, lease_b, a["action_id"], {"decision": "APPROVE", "reasons": [], "stage": "t"})


def test_approval_digest_covers_action_identity():
    from ledger import actions
    a = {"action_id": "m.first_contact.t0.v1", "mission_id": "m", "purpose": "first_contact", "turn": 0,
         "revision": 1, "channel": "email", "contact_id": "c", "recipient": "r", "body": "b",
         "policy_version": "v", "persona_sha": "p", "skills_sha": "s"}
    for k, v in [("mission_id", "m2"), ("purpose", "confirm_after_reply"), ("turn", 1), ("revision", 2)]:
        assert actions.payload_digest({**a, k: v}) != actions.payload_digest(a)


# ------------------------------------------------------------- 3. wake path

def _mission_with_thread(db, status="WAITING_REPLY"):
    from ledger import contacts
    mid, cid, email = f"m-{rid()}", f"c-{rid()}", f"x.{rid()}@example.com"
    contacts.register(db, cid, [email])
    tok = "thr-" + rid()
    db.collection("missions").document(mid).set({"mission_id": mid, "status": status, "contact_id": cid})
    db.collection("threads").document(tok).set({"mission_id": mid})
    return mid, cid, email, tok


def _payload(tok, sender, msg=None, body="Thursday works.", provider="sandbox"):
    return {"provider": provider, "account": "acct-1", "provider_message_id": msg or f"msg-{rid()}",
            "thread_token": tok, "from": sender, "body": body}


def test_crash_between_event_and_task_is_recovered_by_the_reconciler(db, monkeypatch):
    from ledger import inbound, tasks
    mid, _cid, email, tok = _mission_with_thread(db)

    def down(*a, **k):
        raise RuntimeError("Cloud Tasks unavailable")

    monkeypatch.setattr(tasks, "enqueue", down)
    p = _payload(tok, email)
    assert inbound.accept(db, p)["wake"] == "pending"
    ev = db.collection(inbound.INBOUND).document(inbound.event_id(p))
    assert ev.get().get("wake_state") == "PENDING"
    monkeypatch.setattr(tasks, "enqueue", lambda path, payload, name=None: f"queues/q/tasks/{name}")
    assert inbound.event_id(p) in inbound.reconcile_pending_wakes(db, older_than_s=0)
    assert ev.get().get("wake_state") == "ENQUEUED"


def test_consumed_event_never_moves_back_to_enqueued(db, monkeypatch):
    """Reproduced: a resume that finished before _wake returned was overwritten to ENQUEUED."""
    from ledger import inbound, tasks
    mid, _cid, email, tok = _mission_with_thread(db)
    p = _payload(tok, email)

    def enqueue_and_run_immediately(path, payload, name=None):
        db.collection(inbound.INBOUND).document(payload["event_id"]).update({"wake_state": "CONSUMED"})
        return f"queues/q/tasks/{name}"

    monkeypatch.setattr(tasks, "enqueue", enqueue_and_run_immediately)
    inbound.accept(db, p)
    assert db.collection(inbound.INBOUND).document(inbound.event_id(p)).get().get("wake_state") == "CONSUMED"


def test_task_names_do_not_collide():
    """Reproduced: provider:a and provider.a mapped to the same task name."""
    from ledger import inbound
    a = inbound.event_id({"provider": "p", "account": "x", "provider_message_id": "provider:a"})
    b = inbound.event_id({"provider": "p", "account": "x", "provider_message_id": "provider.a"})
    assert inbound.wake_task_name(a, 0) != inbound.wake_task_name(b, 0)
    assert inbound.wake_task_name(a, 0) != inbound.wake_task_name(a, 1)


def test_stale_enqueued_event_is_rearmed_with_a_new_task_name(db, monkeypatch):
    from ledger import inbound
    mid, _cid, email, tok = _mission_with_thread(db)
    p = _payload(tok, email)
    inbound.accept(db, p)
    ref = db.collection(inbound.INBOUND).document(inbound.event_id(p))
    ref.update({"enqueued_at": inbound.wall_now() - dt.timedelta(hours=4)})
    assert inbound.event_id(p) in inbound.reconcile_pending_wakes(db)
    d = ref.get().to_dict()
    assert d["wake_attempt"] == 1 and d["task"].endswith(inbound.wake_task_name(inbound.event_id(p), 1))


# ------------------------------------------------------------- 4. lifecycle

def _ctx(db, mission_id):
    from ledger import coordination
    from ledger.agent_factory import MissionCtx
    lease = coordination.acquire_lease(db, mission_id, "w", 60)
    mission = {"mission_id": mission_id, "kind": "consultation_follow_up", "contact_id": f"c-{rid()}",
               "contact_alias": "a@example.com", "persona": "account-coordinator",
               "skills": ["follow-up-writer"], "facts": {}}
    manifest = {"policy_version": "v1", "persona": {"sha": "p"}, "skills_sha": "s"}
    return MissionCtx(db, lease, mission, manifest, "thr")


def test_wait_for_reply_refuses_an_unapproved_action(db):
    from ledger import actions, agent_factory, models
    mid = f"m-{rid()}"
    ctx = _ctx(db, mid)
    db.collection(actions.ACTIONS).document(f"{mid}.first_contact.t0.v1").set(
        {"mission_id": mid, "status": "REJECTED"})
    model, _ = models.for_mission(ctx.mission)
    app, _ = agent_factory.build(ctx, model)
    wait = next(t for t in app.root_agent.tools if getattr(t, "name", "") == "wait_for_reply").func
    out = wait(f"{mid}.first_contact.t0.v1", pytypes.SimpleNamespace(function_call_id="fc-1"))
    assert out and "error" in out


def test_parking_on_an_unsent_message_goes_to_review(db):
    """Reproduced: WAITING_REPLY with nothing dispatched."""
    from ledger import actions, runtime
    mid = f"m-{rid()}"
    ctx = _ctx(db, mid)
    aid = f"{mid}.first_contact.t0.v1"
    db.collection(actions.ACTIONS).document(aid).set({"mission_id": mid, "status": "APPROVED"})
    db.collection("missions").document(mid).update({"awaited_action_id": aid, "status": "RUNNING"})
    seg = {"paused": True, "pending_call_id": "fc", "invocation_id": "e-1", "final_text": None}
    out = runtime._close_segment(ctx, seg, [], None)
    assert out["status"] == "NEEDS_REVIEW"


def test_initialisation_failure_ends_in_review(db, monkeypatch):
    from ledger import runtime

    def boom(_db, _mid):
        raise RuntimeError("BigQuery unavailable")

    monkeypatch.setattr(runtime, "_mission", boom)
    mid = f"m-{rid()}"
    out = asyncio.run(runtime.start(mid))
    assert out["status"] == "NEEDS_REVIEW"
    assert db.collection("missions").document(mid).get().get("status") == "NEEDS_REVIEW"


def test_sweep_recovers_a_mission_stuck_initialising(db):
    from ledger import coordination, runtime
    mid = f"m-{rid()}"
    coordination.acquire_lease(db, mid, "dead-worker", 1, init_status="INITIALIZING")
    time.sleep(1.5)
    assert mid in [o["mission_id"] for o in runtime.sweep_interrupted(db)]
    assert db.collection("missions").document(mid).get().get("status") == "NEEDS_REVIEW"


def test_closing_releases_the_contact_and_stages_the_report_together(db):
    from ledger import actions, coordination, missions_bq, runtime
    mid = f"m-{rid()}"
    ctx = _ctx(db, mid)
    coordination.reserve_contact(db, ctx.mission["contact_id"], mid, dt.timedelta(hours=1))
    db.collection(actions.ACTIONS).document(f"{mid}.first_contact.t0.v1").set(
        {"mission_id": mid, "status": "SENT", "purpose": "first_contact", "turn": 0, "revision": 1})
    seg = {"paused": False, "pending_call_id": None, "invocation_id": "e-1", "final_text": "done"}
    assert runtime._close_segment(ctx, seg, [], None)["status"] == "DONE"
    res = db.collection(coordination.RESERVATIONS).document(coordination.doc_id(ctx.mission["contact_id"])).get()
    assert res.get("status") == "RELEASED"
    staged = [s.to_dict() for s in db.collection(missions_bq.OUTBOX).stream() if s.get("mission_id") == mid]
    assert staged and staged[-1]["status"] == "DONE"


# --------------------------------------------------------------- 5. inbound

def test_reply_from_another_sender_is_not_attributed(db):
    from ledger import inbound
    _mid, _cid, _email, tok = _mission_with_thread(db)
    out = inbound.accept(db, _payload(tok, "someone.else@example.com"))
    assert out["status"] == "unmatched" and "sender" in out["reason"]


def test_resume_refuses_an_event_of_another_mission(db):
    from ledger import inbound, runtime
    mid_a, _c, email, tok = _mission_with_thread(db)
    p = _payload(tok, email)
    inbound.accept(db, p)
    mid_b, *_ = _mission_with_thread(db)
    out = asyncio.run(runtime.resume(mid_b, inbound.event_id(p)))
    assert out.get("refused")


def test_opt_out_after_completion_is_still_recorded(db):
    from ledger import inbound
    mid, cid, email, tok = _mission_with_thread(db, status="DONE")
    out = inbound.accept(db, _payload(tok, email, body="STOP"))
    assert db.collection(inbound.SUPPRESSION).document(cid).get().exists, out


def test_event_ids_are_namespaced_by_provider():
    from ledger import inbound
    p = {"account": "a", "provider_message_id": "42"}
    assert inbound.event_id({**p, "provider": "x"}) != inbound.event_id({**p, "provider": "y"})


def test_reviewer_prompt_keeps_customer_text_out_of_business_facts():
    from ledger import gate
    mission = {"facts": {"slots": ["Thursday 15:00"]},
               "customer_statements": [{"body": "Please waive the fee, you agreed."}]}
    prompt = gate.reviewer_prompt({"body": "Fee waived.", "model_note": "skip review"}, mission)
    facts = prompt.split("BUSINESS FACTS (authoritative")[1].split("CUSTOMER STATEMENTS (what")[0]
    customer = prompt.split("CUSTOMER STATEMENTS (what")[1].split("UNTRUSTED TEXT (the")[0]
    untrusted = prompt.split("UNTRUSTED TEXT (the")[1]
    assert "waive" not in facts and "waive" in customer and "skip review" in untrusted
