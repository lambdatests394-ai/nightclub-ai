"""Read-only, cross-tenant discovery through the dedicated scheduler boundary."""

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import DateTime, bindparam, text

from backend.app.core.scheduler_database import (
    get_scheduler_session,
    verify_scheduler_role,
)


@dataclass(frozen=True, slots=True)
class DiscoveredPublicationJob:
    organization_id: UUID
    publication_job_id: UUID


class DueJobDiscovery(Protocol):
    async def discover(self, limit: int) -> tuple[DiscoveredPublicationJob, ...]: ...


_DUE_JOBS = text("""
SELECT ci.organization_id, pj.id AS publication_job_id
FROM public.publication_jobs pj
JOIN public.content_items ci ON ci.id = pj.content_item_id
WHERE (pj.status = 'pending' AND pj.scheduled_for <= :now)
   OR (pj.status = 'retryable_failure' AND pj.next_attempt_at IS NOT NULL
       AND pj.next_attempt_at <= :now)
   OR (pj.status IN ('leased', 'publishing') AND pj.lease_expires_at IS NOT NULL
       AND pj.lease_expires_at <= :now)
ORDER BY CASE
    WHEN pj.status = 'pending' THEN pj.scheduled_for
    WHEN pj.status = 'retryable_failure' THEN pj.next_attempt_at
    ELSE pj.lease_expires_at
END ASC, pj.id ASC
LIMIT :limit
""").bindparams(bindparam("now", type_=DateTime(timezone=True)))


class SQLAlchemyDueJobDiscovery:
    """Return only immutable IDs; the read-only transaction closes before execution."""

    def __init__(self, *, session_scope=None, expected_role="nightclub_scheduler",
                 clock=None):
        self._session_scope = session_scope or asynccontextmanager(get_scheduler_session)
        self._expected_role = expected_role
        self._clock = clock or (lambda: datetime.now(UTC))

    async def discover(self, limit: int) -> tuple[DiscoveredPublicationJob, ...]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 32:
            raise ValueError("Invalid server-owned discovery limit")
        async with self._session_scope() as session:
            # get_scheduler_session installs SET TRANSACTION READ ONLY before yielding.
            await verify_scheduler_role(session, self._expected_role)
            rows = (await session.execute(_DUE_JOBS, {
                "now": self._clock(), "limit": limit,
            })).all()
            return tuple(DiscoveredPublicationJob(row.organization_id, row.publication_job_id)
                         for row in rows)
