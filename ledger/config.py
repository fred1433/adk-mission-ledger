"""Runtime settings, read from the environment once.

Every paid model path is gated twice: LEDGER_MODEL_MODE must be "live" AND
LEDGER_ALLOW_PAID_MODEL_CALLS must be "yes". The default is "fixture".
"""

from __future__ import annotations

import dataclasses
import functools
import json
import os
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


@dataclasses.dataclass(frozen=True)
class Settings:
    project: str
    location: str
    agent_engine_id: str
    bq_dataset: str
    tasks_queue: str
    worker_url: str
    worker_service_account: str
    model_mode: str
    allow_paid_model_calls: bool
    live_model: str
    webhook_secret: str
    lease_seconds: int
    session_ttl_seconds: int
    reply_wait_policy_hours: int
    policy_version: str
    max_model_calls: int
    live_model_location: str

    @property
    def paid_models_enabled(self) -> bool:
        return self.model_mode == "live" and self.allow_paid_model_calls


@functools.cache
def settings() -> Settings:
    env = os.environ.get
    return Settings(
        project=env("LEDGER_PROJECT", ""),
        location=env("LEDGER_LOCATION", "us-central1"),
        agent_engine_id=env("LEDGER_AGENT_ENGINE_ID", ""),
        bq_dataset=env("LEDGER_BQ_DATASET", "mission_ledger"),
        tasks_queue=env("LEDGER_TASKS_QUEUE", "mission-wakeups"),
        worker_url=env("LEDGER_WORKER_URL", ""),
        worker_service_account=env("LEDGER_WORKER_SA", ""),
        model_mode=env("LEDGER_MODEL_MODE", "fixture"),
        allow_paid_model_calls=env("LEDGER_ALLOW_PAID_MODEL_CALLS", "no") == "yes",
        live_model=env("LEDGER_LIVE_MODEL", ""),
        webhook_secret=env("LEDGER_WEBHOOK_SECRET", ""),
        lease_seconds=int(env("LEDGER_LEASE_SECONDS", "120")),
        # Reply-wait policy is 48 h; sessions live 5 days so a late but
        # in-policy reply never lands on an expired session.
        session_ttl_seconds=int(env("LEDGER_SESSION_TTL_SECONDS", str(5 * 24 * 3600))),
        reply_wait_policy_hours=int(env("LEDGER_REPLY_WAIT_HOURS", "48")),
        policy_version=env("LEDGER_POLICY_VERSION", "outbound-policy-v1"),
        max_model_calls=int(env("LEDGER_MAX_MODEL_CALLS", "12")),
        live_model_location=env("LEDGER_LIVE_MODEL_LOCATION", "global"),
    )


@functools.cache
def build_info() -> dict:
    """Code revision baked into the image at deploy time (see infra/deploy.sh)."""
    path = REPO_ROOT / "ledger" / "_build.json"
    if path.exists():
        return json.loads(path.read_text())
    return {"git_commit": "uncommitted-local", "built_at": None}
