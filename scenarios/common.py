"""Shared helpers for the recorded scenarios: environment, seeding, evidence.

Each scenario writes evidence/<name>.json with the code commit, pinned
package versions, model mode, where each step ran, timestamps and every
assertion with its observed value. Failed assertions are written too.
"""

from __future__ import annotations

import datetime as dt
import importlib.metadata as md
import json
import os
import pathlib
import subprocess
import sys
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ledger import contacts, coordination, missions_bq  # noqa: E402
from ledger.config import settings  # noqa: E402

PACKAGES = ["google-adk", "google-cloud-aiplatform", "google-cloud-firestore",
            "google-cloud-bigquery", "google-cloud-tasks", "google-genai"]


def run_id() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%m%d%H%M") + "-" + uuid.uuid4().hex[:4]


def db():
    return coordination.client(settings().project)


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "--short=12", "HEAD"],
                                       text=True).strip()
    except Exception:  # noqa: BLE001
        return "uncommitted"


def seed_contact(contact_id: str, aliases: list[str]) -> None:
    contacts.register(db(), contact_id, aliases)


def seed_mission(mission_id: str, kind: str, alias: str, facts: dict, prompt: str,
                 skills=("follow-up-writer", "reply-triage")) -> dict:
    row = {"mission_id": mission_id, "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
           "kind": kind, "prompt": prompt, "persona": "account-coordinator",
           "skills": list(skills), "contact_alias": alias, "facts_json": json.dumps(facts),
           "requested_by": "scenario-script"}
    missions_bq.insert_missions([row])
    return row


class Evidence:
    def __init__(self, name: str, title: str, where: str):
        self.path = ROOT / "evidence" / f"{name}.json"
        self.doc = {
            "scenario": name, "title": title, "where": where,
            "started_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "code_commit": git_commit(),
            "packages": {p: md.version(p) for p in PACKAGES},
            "model_mode": settings().model_mode,
            "paid_model_calls": settings().paid_models_enabled,
            "project": settings().project, "region": settings().location,
            "steps": [], "assertions": [],
        }

    def step(self, what: str, **data) -> None:
        self.doc["steps"].append({"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
                                  "what": what, **data})
        print(f"  - {what}", {k: v for k, v in data.items() if k != 'trace'} if data else "")

    def check(self, name: str, ok: bool, observed) -> bool:
        self.doc["assertions"].append({"assertion": name, "passed": bool(ok), "observed": observed})
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {observed}")
        return ok

    def save(self, **extra) -> bool:
        self.doc.update(extra)
        self.doc["ended_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        self.doc["passed"] = all(a["passed"] for a in self.doc["assertions"])
        self.path.write_text(json.dumps(self.doc, indent=2, default=str) + "\n")
        print(f"evidence -> {self.path.relative_to(ROOT)}  passed={self.doc['passed']}")
        return self.doc["passed"]


def jsonable(x):
    return json.loads(json.dumps(x, default=str))
