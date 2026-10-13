"""Wall-clock helpers.

Leases and reservations are always checked against real time. The 48 h
reply wait in the demo is simulated only by the inbound event's declared
`simulated_received_at`; no lease is shortened and no cloud delay is faked.
"""

from __future__ import annotations

import datetime as dt


def wall_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(t: dt.datetime | None) -> str | None:
    return t.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds") if t else None
