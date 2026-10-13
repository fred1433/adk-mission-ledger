"""Canonical contact identity.

Aliases are normalised per channel (lower-cased email, digits-only phone with
a leading +, lower-cased handle) and then looked up in an authoritative alias
directory. Normalisation alone never decides that two aliases are the same
person: only the directory does. An alias absent from the directory becomes
its own provisional contact and is flagged, so the reservation still protects
it, but nothing claims it was matched to anyone.
"""

from __future__ import annotations

import dataclasses
import re

ALIAS_COLLECTION = "contact_aliases"


@dataclasses.dataclass(frozen=True)
class ContactRef:
    contact_id: str
    alias_key: str
    known: bool


def normalise_alias(raw: str) -> str:
    raw = raw.strip()
    if "@" in raw and not raw.startswith("@"):
        return "email:" + raw.lower()
    if raw.startswith("@"):
        return "handle:" + raw[1:].lower()
    digits = re.sub(r"[^\d]", "", raw)
    if raw.startswith("+") and 8 <= len(digits) <= 15:
        return "phone:+" + digits
    raise ValueError(f"unrecognised alias format: {raw!r}")


def resolve(db, raw_alias: str) -> ContactRef:
    key = normalise_alias(raw_alias)
    snap = db.collection(ALIAS_COLLECTION).document(_doc_id(key)).get()
    if snap.exists:
        return ContactRef(contact_id=snap.get("contact_id"), alias_key=key, known=True)
    return ContactRef(contact_id="provisional:" + key, alias_key=key, known=False)


def register(db, contact_id: str, aliases: list[str]) -> None:
    batch = db.batch()
    for raw in aliases:
        key = normalise_alias(raw)
        batch.set(db.collection(ALIAS_COLLECTION).document(_doc_id(key)),
                  {"contact_id": contact_id, "alias_key": key})
    batch.commit()


def _doc_id(alias_key: str) -> str:
    return alias_key.replace("/", "_")
