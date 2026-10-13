"""Test adapters standing in for external providers. No real recipient exists.

The "provider" keeps its own state in Firestore collections prefixed
`sandbox_provider_`, as if it were a separate company's system: the worker
cannot read or roll it back inside its own transactions, which is exactly the
property that makes an external send ambiguous after a crash.

* IdempotentMailbox: the provider honours an idempotency key. A retry with
  the same key returns the original message and causes no second effect.
* PlainMailbox: no idempotency guarantee (think SMTP relay). A retry would
  deliver twice, so the dispatcher never blindly retries it.
* DraftPublisher: writes a content draft keyed by action id (create-or-return).
"""

from __future__ import annotations

import dataclasses
import uuid

from google.api_core import exceptions as gexc
from google.cloud import firestore

from .clock import wall_now

PROVIDER_MESSAGES = "sandbox_provider_messages"
PROVIDER_KEYS = "sandbox_provider_idempotency"
PROVIDER_DRAFTS = "sandbox_provider_drafts"


@dataclasses.dataclass
class Receipt:
    provider_message_id: str
    accepted_at: str
    replayed: bool = False


class IdempotentMailbox:
    name = "sandbox-mailbox-idempotent"
    supports_idempotency = True

    def __init__(self, db: firestore.Client):
        self.db = db

    def send(self, *, idempotency_key: str, to: str, body: str, thread_token: str,
             mission_id: str) -> Receipt:
        key_ref = self.db.collection(PROVIDER_KEYS).document(idempotency_key)
        db = self.db

        @firestore.transactional
        def _tx(tx):
            existing = key_ref.get(transaction=tx)
            if existing.exists:
                d = existing.to_dict()
                return Receipt(d["provider_message_id"], d["accepted_at"], replayed=True)
            mid = "sbx-" + uuid.uuid4().hex[:12]
            now = wall_now().isoformat()
            tx.set(db.collection(PROVIDER_MESSAGES).document(mid), {
                "to": to, "body": body, "thread_token": thread_token, "mission_id": mission_id,
                "idempotency_key": idempotency_key, "accepted_at": now, "adapter": self.name})
            tx.set(key_ref, {"provider_message_id": mid, "accepted_at": now})
            return Receipt(mid, now)

        return _tx(db.transaction())


class PlainMailbox:
    name = "sandbox-mailbox-plain"
    supports_idempotency = False

    def __init__(self, db: firestore.Client):
        self.db = db

    def send(self, *, idempotency_key: str, to: str, body: str, thread_token: str,
             mission_id: str) -> Receipt:
        mid = "sbx-" + uuid.uuid4().hex[:12]
        now = wall_now().isoformat()
        # The key is stored for later reconciliation only; the provider ignores it.
        self.db.collection(PROVIDER_MESSAGES).document(mid).set({
            "to": to, "body": body, "thread_token": thread_token, "mission_id": mission_id,
            "client_reference": idempotency_key, "accepted_at": now, "adapter": self.name})
        return Receipt(mid, now)


class DraftPublisher:
    name = "sandbox-draft-publisher"
    supports_idempotency = True

    def __init__(self, db: firestore.Client):
        self.db = db

    def send(self, *, idempotency_key: str, to: str, body: str, thread_token: str,
             mission_id: str) -> Receipt:
        ref = self.db.collection(PROVIDER_DRAFTS).document(idempotency_key)
        now = wall_now().isoformat()
        try:
            ref.create({"title": to, "body": body, "mission_id": mission_id, "accepted_at": now})
            return Receipt(idempotency_key, now)
        except gexc.AlreadyExists:
            return Receipt(idempotency_key, ref.get().get("accepted_at"), replayed=True)


def for_channel(db: firestore.Client, channel: str, plain: bool = False):
    if channel == "draft":
        return DraftPublisher(db)
    if channel == "email":
        return PlainMailbox(db) if plain else IdempotentMailbox(db)
    raise ValueError(f"no adapter for channel {channel!r}")
