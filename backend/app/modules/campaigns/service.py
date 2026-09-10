"""Campaign business rules inside the request-owned transaction."""
from datetime import UTC, datetime
from uuid import UUID, uuid4

from backend.app.modules.campaigns.errors import CampaignConflict, CampaignNotFound, InvalidCampaignRequest
from backend.app.modules.campaigns.models import Campaign, CampaignStatus
from backend.app.modules.campaigns.schemas import CampaignRead
from backend.app.modules.identity.policy import Permission, require_permission
from backend.app.modules.identity.service import decode_cursor, encode_cursor
from backend.app.shared.idempotency import fingerprint


def canonical_payload(value):
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, dict):
        return {key: canonical_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [canonical_payload(item) for item in value]
    return value


class CampaignService:
    def __init__(self, context, repository, idempotency, audit):
        self.context, self.repository = context, repository
        self.idempotency, self.audit = idempotency, audit

    @staticmethod
    def serialize(campaign):
        return CampaignRead.model_validate(campaign).model_dump(mode="json", by_alias=True)

    async def read(self, campaign_id: UUID):
        require_permission(self.context, Permission.CAMPAIGN_READ)
        campaign = await self.repository.get(campaign_id)
        if campaign is None:
            raise CampaignNotFound()
        return self.serialize(campaign)

    async def page(self, cursor, limit):
        require_permission(self.context, Permission.CAMPAIGN_READ)
        if not 1 <= limit <= 100:
            raise InvalidCampaignRequest()
        rows = await self.repository.page(decode_cursor(cursor), limit + 1)
        page = rows[:limit]
        return [self.serialize(row) for row in page], encode_cursor(page[-1].id) if len(rows) > limit else None

    async def mutate(self, action, key, payload, campaign_id=None):
        permission = Permission.CAMPAIGN_ARCHIVE if action == "archive" else Permission.CAMPAIGN_WRITE
        require_permission(self.context, permission)  # MUST precede replay lookup.
        if action not in {"create", "patch", "archive"}:
            raise InvalidCampaignRequest()
        operation = f"campaign:{action}:{campaign_id or 'collection'}"
        replay = await self.idempotency.claim(key, operation, fingerprint(operation, canonical_payload(payload)))
        if replay is not None:
            return replay
        # Every mutation acquires its key first, then at most one campaign lock.
        if action == "create":
            campaign = Campaign(id=uuid4(), organization_id=self.context.organization_id,
                                created_by=self.context.user_id, status=CampaignStatus.DRAFT, **payload)
            event = "campaign.created"
        else:
            campaign = await self.repository.get(campaign_id, lock=True)
            if campaign is None:
                raise CampaignNotFound()
            if action == "patch":
                if campaign.status == CampaignStatus.ARCHIVED:
                    raise CampaignConflict()
                starts = payload.get("starts_at", campaign.starts_at)
                ends = payload.get("ends_at", campaign.ends_at)
                if starts and ends and ends < starts:
                    raise InvalidCampaignRequest()
                for field, value in payload.items():
                    setattr(campaign, field, value)
                event = "campaign.updated"
            else:
                event = None if campaign.status == CampaignStatus.ARCHIVED else "campaign.archived"
                campaign.status = CampaignStatus.ARCHIVED
        if event is not None:
            await self.repository.save(campaign)
            await self.audit.write(event, campaign, ["status"] if action == "archive" else list(payload))
        status = 201 if action == "create" else 200
        body = self.serialize(campaign)
        await self.idempotency.complete(key, status, body)
        return status, body
