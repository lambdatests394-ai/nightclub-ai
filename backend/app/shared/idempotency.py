"""Durable, transaction-owned claims. Lock order: key, then campaign row.

Global UUID uniqueness arbitrates even RLS-hidden collisions. DO NOTHING never
updates or exposes a hidden row. A subsequent READ COMMITTED SELECT observes the
winner after its commit; a rolled-back claim permits the waiting INSERT to win.
Never commit an in_progress row. No expiry reuse, deletion or process cache.
"""
import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert

from backend.app.shared.errors import IdempotencyConflict
from backend.app.modules.identity.errors import IdentityUnavailable
from backend.app.shared.models import IdempotencyKey


def fingerprint(operation: str, payload: dict) -> str:
    canonical = json.dumps([operation, payload], sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


class IdempotencyStore:
    def __init__(self, session, context):
        self.session, self.context = session, context

    async def claim(self, key: UUID, operation: str, request_hash: str):
        if await self.session.scalar(text("SHOW transaction_isolation")) != "read committed":
            raise IdentityUnavailable()
        now = datetime.now(UTC)
        statement = insert(IdempotencyKey).values(
            key=key, organization_id=self.context.organization_id, actor_id=self.context.user_id,
            operation=operation, request_hash=request_hash, state="in_progress",
            created_at=now, expires_at=now + timedelta(hours=24),
        ).on_conflict_do_nothing(index_elements=[IdempotencyKey.key]).returning(IdempotencyKey.key)
        if await self.session.scalar(statement) is not None:
            return None
        row = await self.session.scalar(select(IdempotencyKey).where(
            IdempotencyKey.key == key,
            IdempotencyKey.organization_id == self.context.organization_id,
            IdempotencyKey.actor_id == self.context.user_id,
        ))
        if (row is None or row.operation != operation or row.request_hash != request_hash
                or row.state != "completed" or row.response_status not in (200, 201)):
            raise IdempotencyConflict()
        return row.response_status, row.response_body

    async def complete(self, key, status, body):
        if not 200 <= status < 300:
            raise ValueError("Only successful results can be recorded")
        result = await self.session.execute(update(IdempotencyKey).where(
            IdempotencyKey.key == key, IdempotencyKey.organization_id == self.context.organization_id,
            IdempotencyKey.actor_id == self.context.user_id, IdempotencyKey.state == "in_progress",
        ).values(state="completed", response_status=status, response_body=body))
        if result.rowcount != 1:
            raise IdempotencyConflict()
