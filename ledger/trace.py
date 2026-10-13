"""Append-only mission trace in Firestore, read back by the scenario scripts.

Every entry records which process wrote it (Cloud Run revision and instance,
or local host and pid) so the evidence can show that a resume really ran in a
different process from the one that parked the mission.
"""

from __future__ import annotations

import functools
import os
import socket
import urllib.request

from google.cloud import firestore

from .clock import wall_now

TRACE = "mission_trace"


@functools.cache
def process_identity() -> dict:
    ident = {"pid": os.getpid(), "revision": os.environ.get("K_REVISION"),
             "service": os.environ.get("K_SERVICE")}
    if ident["revision"]:
        try:
            req = urllib.request.Request(
                "http://metadata.google.internal/computeMetadata/v1/instance/id",
                headers={"Metadata-Flavor": "Google"})
            ident["instance"] = urllib.request.urlopen(req, timeout=2).read().decode()[:24]
        except Exception:  # noqa: BLE001 - best effort only
            ident["instance"] = None
        ident["where"] = "cloud-run"
    else:
        ident["where"] = "local"
        ident["host"] = socket.gethostname().split(".")[0][:3] + "*"
    return ident


def record(db: firestore.Client, mission_id: str, kind: str, **fields) -> None:
    db.collection(TRACE).document(mission_id).collection("events").add({
        "at": wall_now(), "kind": kind, "process": process_identity(), **fields,
    })


def read(db: firestore.Client, mission_id: str) -> list[dict]:
    q = db.collection(TRACE).document(mission_id).collection("events").order_by("at")
    return [s.to_dict() for s in q.stream()]
