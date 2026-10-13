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


OUTBOX = "report_outbox"


def report_row(mission_id: str, status: str, detail: dict) -> dict:
    return {"mission_id": mission_id, "reported_at": wall_now().isoformat(), "status": status,
            "detail_json": json.dumps(detail, default=str),
            "worker": json.dumps(process_identity()), "model_mode": settings().model_mode}


def stage_report(tx, db, mission_id: str, status: str, detail: dict) -> None:
    """Write the report row to a Firestore outbox inside the caller's transaction."""
    tx.set(db.collection(OUTBOX).document(), report_row(mission_id, status, detail))


def report(mission_id: str, status: str, detail: dict, db=None) -> None:
    """Outside a transaction: stage, then try to flush. BigQuery being down never
    loses a report; the sweep flushes what is left."""
    from google.cloud import firestore as _fs
    db = db or _fs.Client(project=settings().project)
    db.collection(OUTBOX).document().set(report_row(mission_id, status, detail))
    flush_outbox(db)


def flush_outbox(db, limit: int = 200) -> int:
    snaps = list(db.collection(OUTBOX).limit(limit).stream())
    if not snaps:
        return 0
    try:
        errors = _client().insert_rows_json(table("mission_reports"), [s.to_dict() for s in snaps],
                                            row_ids=[s.id for s in snaps])
    except Exception:  # noqa: BLE001 - stays in the outbox for the next flush
        return 0
    if errors:
        return 0
    for s in snaps:
        s.reference.delete()
    return len(snaps)
