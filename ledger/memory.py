"""Memory: quarantine, validation, publication through memories.create only.

What ran (README, scenario 5): the quarantine and the validator on real
Firestore; the publisher against FakeMemoryBank (fixture run) and, in the
live run, against Agent Platform Memory Bank under the memory-publisher
identity, with read-back, full listing and similarity retrieval.

Rules:
* The agent can only call `propose_memory`, which writes a QUARANTINED
  candidate to Firestore. Quarantine lives outside the Memory Bank.
* The validator (deterministic rules, then a reviewer) decides. Approval
  claims a per (scope, topic) slot in a transaction, so two contradictory
  facts on the same scope and topic cannot both be approved, in sequence or
  in parallel; the second is held for review.
* The publisher re-reads the candidate in a transaction and moves it
  APPROVED -> PUBLISHING itself; a forged or stale dict is refused. It writes
  with `memories.create` and the exact approved text. Never `generate` (its
  consolidation may UPDATE or DELETE existing approved memories), never
  IngestEvents, never add_session_to_memory.
* A publisher that died between `create` and its ledger write leaves the
  candidate PUBLISHING; the retry first looks for the exact fact in the scoped
  listing and records it instead of creating a duplicate.
* Absence is checked by a complete scoped listing, not by similarity search.

Approvals are protected by this code path, not by IAM: the worker's
Firestore role could write a candidate's status. Separating approvals into a
database the worker cannot write is a production step (ARCHITECTURE.md).

The claim this supports: only approved payloads enter the bank through this
publishing path. It does not make the bank "true".
"""

from __future__ import annotations

import json
import os
import re
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


def validate(db: firestore.Client, c: dict, supporting_missions: int, reviewer=fixture_reviewer) -> dict:
    checks = rule_checks(c, supporting_missions)
    failed = [x["check"] for x in checks if not x["passed"]]
    if failed:
        decision = {"decision": "REJECT", "stage": "rules", "reasons": failed, "checks": checks}
    else:
        decision = {**reviewer(c), "stage": "reviewer", "checks": checks}
    cref = db.collection(CANDIDATES).document(c["candidate_id"])
    slot = db.collection(TOPIC_SLOTS).document(doc_id(c["scope_key"] + "|" + c["topic"]))

    @firestore.transactional
    def _tx(tx):
        cur = cref.get(transaction=tx).to_dict()
        held = slot.get(transaction=tx).to_dict()
        if cur["status"] != "QUARANTINED":
            return cur["status"], decision
        d = dict(decision)
        if d["decision"] == "APPROVE" and held and held["candidate_id"] != c["candidate_id"]:
            d = {**d, "decision": "NEEDS_REVIEW", "stage": "conflict",
                 "reasons": [f"topic already approved for this scope by {held['candidate_id']}"]}
        status = {"APPROVE": "APPROVED", "REJECT": "REJECTED"}.get(d["decision"], "NEEDS_REVIEW")
        if status == "APPROVED":
            tx.set(slot, {"candidate_id": c["candidate_id"], "fact": c["fact"], "at": wall_now()})
        tx.update(cref, {"status": status, "validation": d, "validated_at": wall_now()})
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
            if cur is None or cur["status"] not in ("APPROVED", "PUBLISHING"):
                raise PermissionError(f"{candidate_id} is {cur and cur['status']}, not APPROVED")
            tx.update(cref, {"status": "PUBLISHING", "publish_attempts": firestore.Increment(1)})
            return cur

        c = _claim(self.db.transaction())
        memory_name, mode = None, "create"
        if c["status"] == "PUBLISHING":  # an earlier attempt may have created it
            for m in self.api.list(name=self.engine):
                if _fact(m) == c["fact"] and _scope(m) == c["scope"]:
                    memory_name, mode = _name(m), "reconciled-existing"
                    break
        if memory_name is None:
            op = self.api.create(name=self.engine, fact=c["fact"], scope=c["scope"])
            memory_name = getattr(getattr(op, "response", None), "name", None) or getattr(op, "name", None)
            if os.environ.get("LEDGER_FAULT") == "after_memory_create":
                raise RuntimeError("injected fault after memories.create, before the ledger write")
        self.db.collection(APPROVALS).document(candidate_id).set({
            "candidate_id": candidate_id, "memory_name": memory_name, "fact": c["fact"],
            "scope": c["scope"], "scope_key": c["scope_key"], "topic": c["topic"],
            "mission_id": c["mission_id"], "evidence": c["evidence"], "published_at": wall_now(),
            "mode": mode})
        cref.update({"status": "PUBLISHED", "memory_name": memory_name})
        return {"memory_name": memory_name, "mode": mode}


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

    def create(self, *, name: str, fact: str, scope: dict, config=None):
        self.calls.append("create")
        mid = f"{name}/memories/{len(self.store) + 1}"
        self.store[mid] = {"name": mid, "fact": fact, "scope": dict(scope)}

        class _Op:
            response = type("M", (), {"name": mid})

        return _Op()

    def list(self, *, name: str, config=None):
        self.calls.append("list")
        return list(self.store.values())

    def generate(self, **_):
        raise AssertionError("memories.generate must never be called by this publisher")

    def update(self, **_):
        raise AssertionError("memories.update must never be called by this publisher")
