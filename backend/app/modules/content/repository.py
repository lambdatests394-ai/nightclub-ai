"""Tenant-scoped content persistence; append-only children and no commits."""
from sqlalchemy import insert, select, update

from backend.app.modules.campaigns.repository import CampaignRepository
from backend.app.modules.content.models import ContentItem, ContentVersion, ReviewDecision
from backend.app.modules.integrations.models import PlatformConnection


class ContentRepository:
    def __init__(self, session, organization_id):
        self.session, self.organization_id = session, organization_id

    async def get_item(self, content_id):
        return await self.session.scalar(select(ContentItem).where(
            ContentItem.id == content_id, ContentItem.organization_id == self.organization_id,
        ).with_for_update())

    @staticmethod
    def current_query():
        return select(ContentItem, ContentVersion).join(ContentVersion, (
            (ContentVersion.content_item_id == ContentItem.id)
            & (ContentVersion.version_no == ContentItem.current_version_no)))

    async def current(self, content_id):
        return (await self.session.execute(self.current_query().where(
            ContentItem.id == content_id, ContentItem.organization_id == self.organization_id,
        ))).one_or_none()

    async def page(self, after, limit):
        query = self.current_query().where(ContentItem.organization_id == self.organization_id)
        if after is not None:
            query = query.where(ContentItem.id > after)
        return (await self.session.execute(query.order_by(ContentItem.id).limit(limit))).all()

    async def campaign(self, campaign_id):
        # Existing tenant-scoped access path. A dependent lock prevents archive
        # from committing between validation and the content transaction commit.
        # Campaign API never locks content, so there is no inverse lock order.
        return await CampaignRepository(self.session, self.organization_id).get(campaign_id, lock=True)

    async def connection(self, connection_id):
        # Projection only: never instantiate a PlatformConnection or select '*'.
        return (await self.session.execute(select(
            PlatformConnection.id, PlatformConnection.organization_id,
            PlatformConnection.platform, PlatformConnection.status,
        ).where(PlatformConnection.id == connection_id,
                PlatformConnection.organization_id == self.organization_id))).one_or_none()

    async def create_item(self, *, content_id, actor_id, campaign_id, platform, connection_id):
        await self.session.execute(insert(ContentItem.__table__).inline().values(
            id=content_id, organization_id=self.organization_id, created_by=actor_id,
            campaign_id=campaign_id, platform=platform, connection_id=connection_id,
            status="draft", current_version_no=1, approved_version_no=None,
        ))

    async def add_content_version(self, version):
        await self.session.execute(insert(ContentVersion.__table__).inline().values(
            id=version.id, content_item_id=version.content_item_id, version_no=version.version_no,
            body=version.body, title=version.title, link_url=version.link_url,
            payload={}, source="manual", created_by=version.created_by,
        ))

    async def add_review_decision(self, decision):
        # No SELECT/RETURNING required on append-only review records.
        await self.session.execute(insert(ReviewDecision.__table__).inline().values(
            id=decision.id, content_item_id=decision.content_item_id,
            content_version_id=decision.content_version_id, decision=decision.decision,
            comment=decision.comment, decided_by=decision.decided_by, decided_at=decision.decided_at,
        ))

    async def save_content_state(self, state):
        await self.session.execute(update(ContentItem).where(
            ContentItem.id == state.content_id, ContentItem.organization_id == self.organization_id,
        ).values(status=state.status, current_version_no=state.current_version_no,
                 approved_version_no=state.approved_version_no))
