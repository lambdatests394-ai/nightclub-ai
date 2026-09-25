"""Tenant-scoped publication persistence; transaction lifetime belongs to the request."""

from sqlalchemy import insert, select, update

from backend.app.modules.automation.models import PublicationJob
from backend.app.modules.content.models import ContentItem


ACTIVE_JOB_STATUSES = ("pending", "leased", "publishing", "retryable_failure")


class PublicationRepository:
    def __init__(self, session, organization_id):
        self.session, self.organization_id = session, organization_id

    async def active_for_content(self, content_id, *, lock=False):
        statement = select(PublicationJob).join(
            ContentItem, ContentItem.id == PublicationJob.content_item_id,
        ).where(
            PublicationJob.content_item_id == content_id,
            ContentItem.organization_id == self.organization_id,
            PublicationJob.status.in_(ACTIVE_JOB_STATUSES),
        ).order_by(PublicationJob.id).limit(1)
        if lock:
            statement = statement.with_for_update(of=PublicationJob)
        return await self.session.scalar(statement)

    async def add_pending(self, job: PublicationJob) -> None:
        await self.session.execute(insert(PublicationJob.__table__).inline().values(
            id=job.id, content_item_id=job.content_item_id,
            content_version_id=job.content_version_id,
            idempotency_key=job.idempotency_key, scheduled_for=job.scheduled_for,
            status="pending", attempt_count=0, next_attempt_at=None,
            created_by=job.created_by,
        ))

    async def cancel_pending(self, job_id) -> bool:
        result = await self.session.execute(update(PublicationJob).where(
            PublicationJob.id == job_id,
            PublicationJob.status == "pending",
            PublicationJob.content_item_id.in_(select(ContentItem.id).where(
                ContentItem.organization_id == self.organization_id,
            )),
        ).values(status="cancelled"))
        return result.rowcount == 1
