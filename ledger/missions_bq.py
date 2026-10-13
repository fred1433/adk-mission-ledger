"""BigQuery: the mission ledger (one row = one business request as a prompt)
and the append-only report table. Never used for locking or authorisation.
"""

from __future__ import annotations

import json

from google.cloud import bigquery

from .clock import wall_now
from .config import settings
from .trace import process_identity


def _client() -> bigquery.Client:
    return bigquery.Client(project=settings().project)


def table(name: str) -> str:
    s = settings()
    return f"{s.project}.{s.bq_dataset}.{name}"


def fetch_mission(mission_id: str) -> dict:
    job = _client().query(
        f"SELECT * FROM `{table('missions')}` WHERE mission_id = @id LIMIT 1",
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("id", "STRING", mission_id)],
            maximum_bytes_billed=50 * 1024 * 1024))
    rows = [dict(r) for r in job.result()]
    if not rows:
        raise KeyError(mission_id)
    row = rows[0]
    row["facts"] = json.loads(row.get("facts_json") or "{}")
    return row


def insert_missions(rows: list[dict]) -> None:
    errors = _client().insert_rows_json(table("missions"), rows)
    if errors:
        raise RuntimeError(errors)


def report(mission_id: str, status: str, detail: dict) -> None:
    row = {"mission_id": mission_id, "reported_at": wall_now().isoformat(), "status": status,
           "detail_json": json.dumps(detail, default=str),
           "worker": json.dumps(process_identity()), "model_mode": settings().model_mode}
    errors = _client().insert_rows_json(table("mission_reports"), [row])
    if errors:
        raise RuntimeError(errors)
