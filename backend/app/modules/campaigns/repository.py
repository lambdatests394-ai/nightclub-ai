"""Tenant predicates supplement RLS; no commits and no DELETE operations."""
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.modules.campaigns.models import Campaign


class CampaignRepository:
    def __init__(self, session: AsyncSession, organization_id: UUID):
        self.session = session
        self.organization_id = organization_id

    async def get(self, campaign_id: UUID, *, lock=False):
        query = select(Campaign).where(Campaign.id == campaign_id,
                                       Campaign.organization_id == self.organization_id)
        if lock:
            query = query.with_for_update()
        return await self.session.scalar(query)

    async def page(self, after: UUID | None, limit: int):
        query = select(Campaign).where(Campaign.organization_id == self.organization_id)
        if after is not None:
            query = query.where(Campaign.id > after)
        return list((await self.session.scalars(query.order_by(Campaign.id).limit(limit))).all())

    async def save(self, campaign: Campaign):
        self.session.add(campaign)
        await self.session.flush()
        await self.session.refresh(campaign)
