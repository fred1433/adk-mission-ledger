"""Memory: quarantine, validation, publication through memories.create only.

What ran (README, scenario 5): the quarantine and the validator on real
Firestore; the publisher against FakeMemoryBank (fixture run) and, in the
live run, against Agent Platform Memory Bank under the memory-publisher
identity, with read-back, full listing and similarity retrieval.

Rules:
* The agent can only call `propose_memory`, which writes a QUARANTINED
  candidate to Firestore. Quarantine lives outside the Memory Bank.
* The validator reads the STORED candidate by id (deterministic rules, then a
  reviewer) and binds its verdict to a digest of fact, scope, topic, evidence
  and provenance; a record changed during review is not approved. Approval
  claims a per (scope, topic) slot in a transaction, so two contradictory
  facts on the same scope and topic cannot both be approved.
* The publisher re-reads the candidate in a transaction, refuses it unless
  its current content still matches the approved digest, and writes with
  `memories.create` under a stable caller-supplied memory id, so concurrent or
  repeated attempts converge on one memory, which is read back and compared
  with the approved payload. Never `generate` (its
  consolidation may UPDATE or DELETE existing approved memories), never
  IngestEvents, never add_session_to_memory.
* A publisher that died between `create` and its ledger write leaves the
  candidate PUBLISHING; the retry hits the same memory id and records it.
* Absence is checked by a complete scoped listing, not by similarity search.

Approvals are protected by this code path, not by IAM: the worker's
Firestore role could write a candidate's status. Separating approvals into a
database the worker cannot write is a production step (ARCHITECTURE.md).

The claim this supports: only approved payloads enter the bank through this
publishing path. It does not make the bank "true".
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid

from google.cloud import firestore

from .clock import wall_now
from .config import REPO_ROOT
from .coordination import doc_id

CANDIDATES = "memory_candidates"
APPROVALS = "memory_approvals"
TOPIC_SLOTS = "memory_topic_slots"
CONTACT_DATA = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|\+?\d[\d\s-]{7,}\d")


def scope_key(scope: dict) -> str:
    return json.dumps(scope, sort_keys=True)


def quarantine(db: firestore.Client, *, mission_id: str, fact: str, scope: dict,
               topic: str, evidence: str) -> dict:
    cid = "mem-" + uuid.uuid4().hex[:10]
    doc = {"candidate_id": cid, "mission_id": mission_id, "fact": fact, "scope": scope,
           "scope_key": scope_key(scope), "topic": topic, "evidence": evidence,
           "status": "QUARANTINED", "proposed_at": wall_now()}
    db.collection(CANDIDATES).document(cid).set(doc)
    return doc


def rule_checks(c: dict, supporting_missions: int) -> list[dict]:
    scope = c["scope"]
    is_contact = set(scope) == {"contact_id"}
    is_playbook = set(scope) == {"playbook"}
    checks = [
        ("scope_is_contact_or_playbook", is_contact or is_playbook),
        ("length_within_bounds", 10 <= len(c["fact"]) <= 300),
        ("no_raw_contact_data", not CONTACT_DATA.search(c["fact"])),
        ("has_evidence", bool(c.get("evidence"))),
        # A single mission is not enough to turn an observation into a rule
        # every agent will follow.
        ("playbook_needs_two_missions", (not is_playbook) or supporting_missions >= 2),
    ]
    return [{"check": n, "passed": bool(ok)} for n, ok in checks]


def fixture_reviewer(c: dict) -> dict:
    table = json.loads((REPO_ROOT / "fixtures" / "reviewer" / "memory.json").read_text())
    raw = table.get(c["topic"], {})
    verdict = raw.get("verdict") if raw.get("verdict") in ("APPROVE", "REJECT", "NEEDS_REVIEW") else "NEEDS_REVIEW"
    return {"decision": verdict, "reasons": raw.get("reasons", []), "reviewer_mode": "fixture:memory.json"}


def content_digest(c: dict) -> str:
    """What the approval covers: the exact fact, scope, topic, evidence and provenance."""
    bound = {k: c.get(k) for k in ("candidate_id", "mission_id", "fact", "scope", "topic", "evidence")}
    return hashlib.sha256(json.dumps(bound, sort_keys=True).encode()).hexdigest()


def memory_id_for(candidate_id: str) -> str:
    """Stable provider-side id ([a-z][a-z0-9-]{0,62}): every publication attempt
    for one candidate targets the same memory resource."""
    return "cand-" + hashlib.sha256(candidate_id.encode()).hexdigest()[:40]


def validate(db: firestore.Client, candidate_id: str, supporting_missions: int,
             reviewer=fixture_reviewer) -> dict:
    """Validate the STORED candidate, never a caller-supplied copy. The verdict
    is bound to a digest of what was reviewed; if the record changed while the
    reviewer ran, nothing is approved."""
    cref = db.collection(CANDIDATES).document(candidate_id)
    c = cref.get().to_dict()
    if c is None:
        raise KeyError(candidate_id)
    reviewed = content_digest(c)
    checks = rule_checks(c, supporting_missions)
    failed = [x["check"] for x in checks if not x["passed"]]
    if failed:
        decision = {"decision": "REJECT", "stage": "rules", "reasons": failed, "checks": checks}
    else:
        decision = {**reviewer(c), "stage": "reviewer", "checks": checks}
    slot = db.collection(TOPIC_SLOTS).document(doc_id(c["scope_key"] + "|" + c["topic"]))

    @firestore.transactional
    def _tx(tx):
        cur = cref.get(transaction=tx).to_dict()
        held = slot.get(transaction=tx).to_dict()
        if cur["status"] != "QUARANTINED":
            return cur["status"], cur.get("validation", decision)
        d = dict(decision)
        if content_digest(cur) != reviewed:
            d = {**d, "decision": "NEEDS_REVIEW", "stage": "changed",
                 "reasons": ["candidate changed while it was being reviewed"]}
        elif d["decision"] == "APPROVE" and held and held["candidate_id"] != candidate_id:
            d = {**d, "decision": "NEEDS_REVIEW", "stage": "conflict",
                 "reasons": [f"topic already approved for this scope by {held['candidate_id']}"]}
        status = {"APPROVE": "APPROVED", "REJECT": "REJECTED"}.get(d["decision"], "NEEDS_REVIEW")
        update = {"status": status, "validation": d, "validated_at": wall_now()}
        if status == "APPROVED":
            update["approved_digest"] = reviewed
            tx.set(slot, {"candidate_id": candidate_id, "fact": cur["fact"], "at": wall_now()})
        tx.update(cref, update)
        return status, d

    status, d = _tx(db.transaction())
    return {**c, "status": status, "validation": d}


class MemoryPublisher:
    """Runs under the publisher identity, never the worker's."""

    def __init__(self, db: firestore.Client, memories_api, engine_name: str):
        self.db, self.api, self.engine = db, memories_api, engine_name

    def publish(self, candidate_id: str) -> dict:
        cref = self.db.collection(CANDIDATES).document(candidate_id)

        @firestore.transactional
        def _claim(tx):
            cur = cref.get(transaction=tx).to_dict()
            if cur is None or cur["status"] not in ("APPROVED", "PUBLISHING", "PUBLISHED"):
                raise PermissionError(f"{candidate_id} is {cur and cur['status']}, not APPROVED")
            if content_digest(cur) != cur.get("approved_digest"):
                tx.update(cref, {"status": "NEEDS_REVIEW", "reason": "changed after approval"})
                return {**cur, "_refused": "candidate changed after approval"}
            if cur["status"] == "APPROVED":
                tx.update(cref, {"status": "PUBLISHING", "publish_attempts": firestore.Increment(1)})
            return cur

        c = _claim(self.db.transaction())
        if c.get("_refused"):
            raise PermissionError(f"{candidate_id}: {c['_refused']}")
        if c["status"] == "PUBLISHED":
            return {"memory_name": c["memory_name"], "mode": "already-published"}
        mid = memory_id_for(candidate_id)
        name = f"{self.engine}/memories/{mid}"
        mode = "create"
        try:
            self.api.create(name=self.engine, fact=c["fact"], scope=c["scope"],
                            config={"memory_id": mid, "wait_for_completion": True})
        except Exception as e:  # noqa: BLE001 - only "already exists" is acceptable
            if "409" not in str(e) and "ALREADY_EXISTS" not in str(e).upper():
                raise
            mode = "already-existed"
        got = self.api.get(name=name)
        if _fact(got) != c["fact"] or _scope(got) != c["scope"]:
            self.db.collection(CANDIDATES).document(candidate_id).update(
                {"status": "NEEDS_REVIEW", "reason": "provider memory differs from the approved payload"})
            raise RuntimeError(f"{name} does not hold the approved payload")
        if os.environ.get("LEDGER_FAULT") == "after_memory_create":
            raise RuntimeError("injected fault after memories.create, before the ledger write")
        self.db.collection(APPROVALS).document(candidate_id).set({
            "candidate_id": candidate_id, "memory_name": name, "fact": c["fact"],
            "scope": c["scope"], "scope_key": c["scope_key"], "topic": c["topic"],
            "mission_id": c["mission_id"], "evidence": c["evidence"], "published_at": wall_now(),
            "approved_digest": c["approved_digest"], "mode": mode})
        cref.update({"status": "PUBLISHED", "memory_name": name})
        return {"memory_name": name, "mode": mode}


def _fact(m):
    return m["fact"] if isinstance(m, dict) else getattr(m, "fact", None)


def _scope(m):
    return dict(m["scope"] if isinstance(m, dict) else (getattr(m, "scope", None) or {}))


def _name(m):
    return m["name"] if isinstance(m, dict) else getattr(m, "name", None)


class FakeMemoryBank:
    """In-process stand-in exposing the memories API surface used here.

    `generate` and `update` raise, so a run fails loudly if the publishing
    path ever drifts away from `create`.
    """

    def __init__(self):
        self.store: dict[str, dict] = {}
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def create(self, *, name: str, fact: str, scope: dict, config=None):
        self.calls.append("create")
        mid = (config or {}).get("memory_id") or str(len(self.store) + 1)
        full = f"{name}/memories/{mid}"
        with self._lock:
            if full in self.store:
                raise RuntimeError("409 ALREADY_EXISTS: " + full)
            self.store[full] = {"name": full, "fact": fact, "scope": dict(scope)}
        return type("Op", (), {"response": type("M", (), {"name": full})})()

    def get(self, *, name: str, config=None):
        self.calls.append("get")
        return self.store[name]

    def list(self, *, name: str, config=None):
        self.calls.append("list")
        return list(self.store.values())

    def generate(self, **_):
        raise AssertionError("memories.generate must never be called by this publisher")

    def update(self, **_):
        raise AssertionError("memories.update must never be called by this publisher")
