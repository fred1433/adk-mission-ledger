"""Cloud Tasks wake-ups. Cloud Tasks is the alarm clock, not the waiting
process: nothing stays alive during the reply wait. Tasks may run more than
once and an HTTP target gets at most 30 minutes, so every handler is
idempotent and finishes a bounded segment.
"""

from __future__ import annotations

import json
import re

from google.api_core import exceptions as gexc
from google.cloud import tasks_v2

from .config import settings


def enqueue(path: str, payload: dict, name: str | None = None) -> str:
    s = settings()
    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(s.project, s.location, s.tasks_queue)
    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": s.worker_url.rstrip("/") + path,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload).encode(),
            "oidc_token": {"service_account_email": s.worker_service_account,
                           "audience": s.worker_url},
        },
    }
    if name:
        task["name"] = f"{parent}/tasks/" + re.sub(r"[^A-Za-z0-9_-]", "-", name)[:450]
    try:
        return client.create_task(parent=parent, task=task).name
    except gexc.AlreadyExists:
        return task["name"] + " (already existed)"
