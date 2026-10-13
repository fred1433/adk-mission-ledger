"""Memory: quarantine, validation, publication through memories.create only.

Status in this repository: the quarantine and the validator ran on real
Firestore; the publisher ran against FakeMemoryBank (tests and scenario 5).
No call to Agent Platform Memory Bank has been made yet, because every
memory write computes embeddings, which is a paid model call that has not
been authorised. See README, "Not yet run".

Rules:
* The agent can only call `propose_memory`, which writes a QUARANTINED
  candidate to Firestore. Quarantine lives outside the Memory Bank.
* The validator (deterministic rules, then a reviewer) decides.
* Only approved candidates are published, with `memories.create` and the
  exact approved text. Never `generate` (its consolidation may UPDATE or
  DELETE existing approved memories), never IngestEvents, never
  add_session_to_memory.
* The approval ledger keeps provenance and scope. Conflicting proposals on the
  same scope and topic are held for review instead of overwriting.
* Absence is checked by a complete scoped listing, not by similarity search.

The claim this supports: only approved payloads enter the bank through this
publishing path. It does not make the bank "true".
"""

from __future__ import annotations

import json
import re
import uuid

from google.cloud import firestore

from .clock import wall_now
from .config import REPO_ROOT

CANDIDATES = "memory_candidates"
APPROVALS = "memory_approvals"
CONTACT_DATA = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|\+?\d[\d\s-]{7,}\d")


def quarantine(db: firestore.Client, *, mission_id: str, fact: str, scope: dict,
               topic: str, evidence: str) -> dict:
    cid = "mem-" + uuid.uuid4().hex[:10]
    doc = {"candidate_id": cid, "mission_id": mission_id, "fact": fact, "scope": scope,
           "topic": topic, "evidence": evidence, "status": "QUARANTINED", "proposed_at": wall_now()}
    db.collection(CANDIDATES).document(cid).set(doc)
    return doc


def rule_checks(c: dict, approved_same_topic: list[dict], supporting_missions: int) -> list[dict]:
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
        ("no_conflict_with_approved", not approved_same_topic),
    ]
    return [{"check": n, "passed": bool(ok)} for n, ok in checks]


def fixture_reviewer(c: dict) -> dict:
    table = json.loads((REPO_ROOT / "fixtures" / "reviewer" / "memory.json").read_text())
    raw = table.get(c["topic"], {})
    verdict = raw.get("verdict") if raw.get("verdict") in ("APPROVE", "REJECT", "NEEDS_REVIEW") else "NEEDS_REVIEW"
    return {"decision": verdict, "reasons": raw.get("reasons", []), "reviewer_mode": "fixture:memory.json"}


def validate(db: firestore.Client, c: dict, supporting_missions: int, reviewer=fixture_reviewer) -> dict:
    q = (db.collection(APPROVALS)
         .where(filter=firestore.FieldFilter("topic", "==", c["topic"]))
         .where(filter=firestore.FieldFilter("scope_key", "==", _scope_key(c["scope"]))))
    same = [s.to_dict() for s in q.stream()]
    checks = rule_checks(c, same, supporting_missions)
    failed = [x["check"] for x in checks if not x["passed"]]
    if failed:
        decision = {"decision": "NEEDS_REVIEW" if failed == ["no_conflict_with_approved"] else "REJECT",
                    "stage": "rules", "reasons": failed, "checks": checks}
    else:
        decision = {**reviewer(c), "stage": "reviewer", "checks": checks}
    status = {"APPROVE": "APPROVED", "REJECT": "REJECTED"}.get(decision["decision"], "NEEDS_REVIEW")
    db.collection(CANDIDATES).document(c["candidate_id"]).update(
        {"status": status, "validation": decision, "validated_at": wall_now()})
    return {**c, "status": status, "validation": decision}


class MemoryPublisher:
    """Runs under the publisher identity, never the worker's."""

    def __init__(self, db: firestore.Client, memories_api, engine_name: str):
        self.db, self.api, self.engine = db, memories_api, engine_name

    def publish(self, c: dict) -> dict:
        if c.get("status") != "APPROVED":
            raise PermissionError(f"{c['candidate_id']} is {c.get('status')}, not APPROVED")
        op = self.api.create(name=self.engine, fact=c["fact"], scope=c["scope"])
        memory_name = getattr(getattr(op, "response", None), "name", None) or getattr(op, "name", None)
        self.db.collection(APPROVALS).document(c["candidate_id"]).set({
            "candidate_id": c["candidate_id"], "memory_name": memory_name, "fact": c["fact"],
            "scope": c["scope"], "scope_key": _scope_key(c["scope"]), "topic": c["topic"],
            "mission_id": c["mission_id"], "evidence": c["evidence"], "published_at": wall_now()})
        self.db.collection(CANDIDATES).document(c["candidate_id"]).update({"status": "PUBLISHED"})
        return {"memory_name": memory_name}


class FakeMemoryBank:
    """In-process stand-in exposing the memories API surface used here.

    `generate` and `update` raise, so a test fails loudly if the publishing
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


def _scope_key(scope: dict) -> str:
    return json.dumps(scope, sort_keys=True)
