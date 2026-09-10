"""Append-only Core INSERT without ORM identity RETURNING or broad SELECT."""
from sqlalchemy import insert
from backend.app.modules.audit.models import AuditLog


class CampaignAuditWriter:
    def __init__(self, session, context, correlation_id):
        self.session, self.context, self.correlation_id = session, context, correlation_id

    async def write(self, action, campaign, changed_fields):
        metadata = {
            "campaignId": str(campaign.id), "organizationId": str(self.context.organization_id),
            "status": str(campaign.status), "changedFields": sorted(changed_fields),
            "startsAt": campaign.starts_at.isoformat() if campaign.starts_at else None,
            "endsAt": campaign.ends_at.isoformat() if campaign.ends_at else None,
        }
        # inline disables implicit RETURNING (including the generated identity).
        await self.session.execute(insert(AuditLog.__table__).inline().values(
            organization_id=self.context.organization_id, actor_type="user",
            actor_id=str(self.context.user_id), action=action, entity_type="campaign",
            entity_id=campaign.id, correlation_id=self.correlation_id, after=metadata,
        ))
