"""BigQuery intake: the control plane's entry point.

Reads recent rows of `missions` that have no execution record yet in
Firestore and enqueues one named start task per mission (`start-<id>`).
Cloud Tasks refuses a second task with the same name, and the start handler
is idempotent, so overlapping intake runs cannot start a mission twice.
Scheduled every minute by Cloud Scheduler (infra/setup.sh).
"""

from __future__ import annotations

from google.cloud import bigquery, firestore

from . import tasks
from .config import settings
from .coordination import MISSIONS
from .missions_bq import table


def run(db: firestore.Client, lookback_hours: int = 48, limit: int = 200) -> dict:
    s = settings()
    bq = bigquery.Client(project=s.project)
    job = bq.query(
        f"SELECT mission_id FROM `{table('missions')}` "
        "WHERE created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @h HOUR) "
        "ORDER BY created_at LIMIT @n",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("h", "INT64", lookback_hours),
            bigquery.ScalarQueryParameter("n", "INT64", limit)],
            maximum_bytes_billed=50 * 1024 * 1024))
    ids = [r["mission_id"] for r in job.result()]
    refs = [db.collection(MISSIONS).document(i) for i in ids]
    known = {snap.id for snap in db.get_all(refs) if snap.exists} if refs else set()
    started = []
    for mid in ids:
        if mid in known:
            continue
        tasks.enqueue("/tasks/start", {"mission_id": mid}, name=f"start-{mid}")
        started.append(mid)
    return {"bigquery_job": job.job_id, "rows_seen": len(ids), "enqueued": started}
