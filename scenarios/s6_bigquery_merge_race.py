"""Scenario 6: why the contact lock is not in BigQuery.

BigQuery documents that a MERGE which only inserts rows does not conflict
with other concurrently running DML. MERGE itself is atomic; the problem is
that two insert-only MERGEs keyed on the same contact can both see "not
matched" and both insert. This scenario runs genuinely overlapping
statements and records what actually happened: query job ids, timings,
affected rows, final row counts. It does not require a duplicate; if none is
observed, the evidence says so.

Then the same race against Firestore's transactional reservation.
"""

from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import time

from common import Evidence, db, run_id
from google.cloud import bigquery

from ledger import coordination
from ledger.config import settings
from ledger.coordination import ContactReserved

N, ROUNDS = 20, 3
MERGE = """
MERGE `{t}` T
USING (SELECT @contact AS contact_id, @mission AS mission_id) S
ON T.contact_id = S.contact_id
WHEN NOT MATCHED THEN
  INSERT (contact_id, mission_id, claimed_at) VALUES (S.contact_id, S.mission_id, CURRENT_TIMESTAMP())
"""


def main() -> bool:
    ev = Evidence("s6_bigquery_merge_race", "Insert-only MERGE as a lock, vs a Firestore transaction",
                  where="local process -> BigQuery and Firestore (Google Cloud)")
    s = settings()
    bq = bigquery.Client(project=s.project)
    t = f"{s.project}.{s.bq_dataset}.naive_contact_claims"
    rid = run_id()
    rounds = []
    for r in range(ROUNDS):
        contact = f"c-{rid}-r{r}"

        def claim(i: int):
            cfg = bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("contact", "STRING", contact),
                bigquery.ScalarQueryParameter("mission", "STRING", f"m-{rid}-r{r}-{i:02d}")],
                maximum_bytes_billed=200 * 1024 * 1024)
            job = bq.query(MERGE.format(t=t), job_config=cfg)
            try:
                job.result()
                err = None
            except Exception as e:  # noqa: BLE001 - record, do not hide
                err = str(e)[:160]
            return {"job_id": job.job_id, "affected": job.num_dml_affected_rows,
                    "started": job.started.isoformat() if job.started else None,
                    "ended": job.ended.isoformat() if job.ended else None, "error": err}

        with cf.ThreadPoolExecutor(N) as pool:
            jobs = list(pool.map(claim, range(N)))
        rows = list(bq.query(f"SELECT mission_id FROM `{t}` WHERE contact_id = @c",
                             job_config=bigquery.QueryJobConfig(query_parameters=[
                                 bigquery.ScalarQueryParameter("c", "STRING", contact)])).result())
        starts = sorted(j["started"] for j in jobs if j["started"])
        ends = sorted(j["ended"] for j in jobs if j["ended"])
        inserted = sum(1 for j in jobs if j["affected"] == 1)
        # Which statements actually ran at the same time? BigQuery runs at
        # most 2 mutating DML statements per table concurrently and queues the
        # rest, so most of the 20 never overlap.
        overlapping = sorted({j["job_id"] for j in jobs for k in jobs if j is not k and j["started"]
                              and k["started"] and j["started"] < k["ended"] and k["started"] < j["ended"]})
        inserters = [j["job_id"] for j in jobs if j["affected"] == 1]
        rnd = {"round": r, "contact": contact, "statements": N, "rows_for_contact": len(rows),
               "statements_reporting_insert": inserted,
               "errors": sum(1 for j in jobs if j["error"]),
               "overlap": bool(starts and ends and starts[-1] < ends[0]),
               "jobs_overlapping_another": overlapping, "jobs_that_inserted": inserters,
               "first_start": starts[0] if starts else None, "last_start": starts[-1] if starts else None,
               "first_end": ends[0] if ends else None, "jobs": jobs}
        rounds.append(rnd)
        ev.step(f"round {r}: {N} insert-only MERGE submitted at once on one contact",
                rows_for_contact=len(rows), statements_reporting_insert=inserted,
                errors=rnd["errors"], jobs_overlapping_another=len(overlapping),
                inserters_were_overlapping=all(i in overlapping for i in inserters))
        if len(rows) > 1:
            break
        time.sleep(1)
    worst = max(r["rows_for_contact"] for r in rounds)
    ev.check("observed outcome recorded (not asserted in advance)", True,
             f"max rows for one contact across {len(rounds)} round(s): {worst}")

    d = db()
    cid = f"c-{rid}-fs"

    def reserve(i):
        try:
            coordination.reserve_contact(d, cid, f"m-{rid}-fs-{i:02d}", dt.timedelta(hours=1))
            return "won"
        except ContactReserved:
            return "refused"

    with cf.ThreadPoolExecutor(N) as pool:
        fs = list(pool.map(reserve, range(N)))
    ev.step(f"Firestore: {N} concurrent transactional reservations", results=fs)
    ev.check("Firestore: exactly one winner", fs.count("won") == 1, fs.count("won"))
    return ev.save(bigquery_rounds=rounds, bigquery_doc_quotes=[
        "A MERGE DML statement does not conflict with other concurrently running DML statements as long as "
        "the statement only inserts rows and does not delete or update any existing rows.",
        "BigQuery runs up to 2 of them concurrently, after which up to 20 are queued as PENDING."])


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
