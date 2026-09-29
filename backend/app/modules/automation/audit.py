"""Structural scheduling audit; no content, URL, asset or provider values."""

from sqlalchemy import insert

from backend.app.modules.audit.models import AuditLog
from backend.app.modules.identity.policy import OrganizationContext, SystemAutomationContext


class PublicationAuditWriter:
    def __init__(self, session, context, correlation_id):
        self.session, self.context, self.correlation_id = session, context, correlation_id

    def _actor(self):
        if isinstance(self.context, OrganizationContext):
            return "user", str(self.context.user_id)
        if isinstance(self.context, SystemAutomationContext):
            return "system", None
        raise TypeError("Unsupported publication audit authority")

    async def write(self, action, *, content_id, job_id, version_no, previous_status,
                    status, job_status, scheduled_for, immediate):
        actor_type, actor_id = self._actor()
        await self.session.execute(insert(AuditLog.__table__).inline().values(
            organization_id=self.context.organization_id, actor_type=actor_type,
            actor_id=actor_id, action=action, entity_type="content",
            entity_id=content_id, correlation_id=self.correlation_id,
            after={
                "organizationId": str(self.context.organization_id),
                "contentId": str(content_id), "publicationJobId": str(job_id),
                "versionNo": version_no, "previousStatus": str(previous_status),
                "status": str(status), "publicationJobStatus": job_status,
                "scheduledFor": scheduled_for.isoformat() if scheduled_for else None,
                "immediate": bool(immediate),
            },
        ))

    async def execution(
            self, action, *, content_id, job_id, version_no, attempt_no, status,
            outcome, reconciled, provider_http_status=None, error_code=None,
            next_attempt_at=None):
        """Persist execution structure only; provider/content values stay excluded."""
        actor_type, actor_id = self._actor()
        await self.session.execute(insert(AuditLog.__table__).inline().values(
            organization_id=self.context.organization_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            entity_type="publication",
            entity_id=job_id,
            correlation_id=self.correlation_id,
            after={
                "organizationId": str(self.context.organization_id),
                "contentId": str(content_id),
                "versionNo": version_no,
                "jobId": str(job_id),
                "attemptNo": attempt_no,
                "status": status,
                "outcome": outcome,
                "reconciled": bool(reconciled),
                "providerHttpStatus": provider_http_status,
                "errorCode": error_code,
                "nextAttemptAt": (
                    next_attempt_at.isoformat() if next_attempt_at else None
                ),
            },
        ))
