"""Transactional content workflow orchestration without HTTP or concrete DB access."""
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from backend.app.modules.automation.models import PublicationJob
from backend.app.modules.content.models import ContentVersion, ReviewDecision
from backend.app.modules.content.state_machine import ContentState, ContentStateMachine


class ContentUnitOfWork(Protocol):
    """Persistence boundary implemented later by a SQLAlchemy adapter."""

    async def add_content_version(self, version: ContentVersion) -> None: ...
    async def add_review_decision(self, decision: ReviewDecision) -> None: ...
    async def add_publication_job(self, job: PublicationJob) -> None: ...
    async def save_content_state(self, content: ContentState) -> None: ...


class ContentWorkflowService:
    """Persist child/state effects; the outer protected request owns commit."""

    def __init__(self, unit_of_work: ContentUnitOfWork) -> None:
        self._uow = unit_of_work

    async def review(self, content: ContentState, *, content_version_id: UUID, decision: str, decided_by: UUID, decided_at: datetime, comment: str | None = None, reviewer_role: str | None = None) -> ReviewDecision:
        effect = await ContentStateMachine.review(content, decision=decision, decided_by=decided_by, comment=comment, reviewer_role=reviewer_role)
        record = ReviewDecision(
            id=uuid4(), content_item_id=content.content_id, content_version_id=content_version_id,
            decision=effect.decision, comment=effect.comment, decided_by=effect.decided_by, decided_at=decided_at,
        )
        await self._uow.add_review_decision(record)
        await self._uow.save_content_state(content)
        return record

    async def edit(self, content: ContentState, *, body: str, created_by: UUID, source: str = "manual", title: str | None = None, link_url: str | None = None) -> ContentVersion:
        version_no = await ContentStateMachine.edit(content)
        version = ContentVersion(
            id=uuid4(), content_item_id=content.content_id, version_no=version_no, body=body,
            title=title, link_url=link_url, payload={}, source=source, created_by=created_by,
        )
        await self._uow.add_content_version(version)
        await self._uow.save_content_state(content)
        return version

    async def schedule(self, content: ContentState, *, content_version_id: UUID, scheduled_for: datetime, scheduled_by: UUID, idempotency_key: UUID) -> PublicationJob:
        await ContentStateMachine.schedule(content)
        job = PublicationJob(
            id=uuid4(), content_item_id=content.content_id, content_version_id=content_version_id,
            idempotency_key=idempotency_key, scheduled_for=scheduled_for, status="pending",
            attempt_count=0, created_by=scheduled_by,
        )
        await self._uow.add_publication_job(job)
        await self._uow.save_content_state(content)
        return job
