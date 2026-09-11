"""Structural content audit, without body/title/link/comment values."""
from sqlalchemy import insert
from backend.app.modules.audit.models import AuditLog


class ContentAuditWriter:
    def __init__(self, session, context, correlation_id):
        self.session, self.context, self.correlation_id = session, context, correlation_id

    async def write(self, action, state, *, previous_status=None, changed_fields=(), owner_override=False):
        metadata = {
            "organizationId": str(self.context.organization_id), "contentId": str(state.content_id),
            "currentVersionNo": state.current_version_no, "previousStatus": previous_status,
            "status": str(state.status), "changedFields": sorted(changed_fields),
            "ownerOverride": bool(owner_override),
        }
        await self.session.execute(insert(AuditLog.__table__).inline().values(
            organization_id=self.context.organization_id, actor_type="user",
            actor_id=str(self.context.user_id), action=action, entity_type="content",
            entity_id=state.content_id, correlation_id=self.correlation_id, after=metadata,
        ))
