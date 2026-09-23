"""Explicit tenant predicates in addition to RLS. Transaction belongs to coordinator."""
from uuid import UUID
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from backend.app.modules.assets.models import Asset
from backend.app.modules.assets.errors import AssetConflict


class AssetRepository:
    def __init__(self, session: AsyncSession, organization_id: UUID):
        self.session, self.organization_id = session, organization_id

    async def find_by_id(self, asset_id: UUID, *, lock: bool = False) -> Asset | None:
        statement = select(Asset).where(Asset.id == asset_id, Asset.organization_id == self.organization_id)
        if lock:
            locked = await self.session.scalar(statement.with_for_update().execution_options(populate_existing=True))
            if locked is not None:
                return locked
            # UPDATE USING permits only pending, so PostgreSQL may hide a visible
            # terminal row from FOR UPDATE. Read it to report the specified 409.
            # A new pending row appearing between snapshots must be retried, not
            # returned as though a row lock had been acquired.
            visible = await self.session.scalar(statement.execution_options(populate_existing=True))
            if visible is not None and visible.status == "pending":
                raise AssetConflict()
            return visible
        return await self.session.scalar(statement.execution_options(populate_existing=True))

    async def find_by_sha256(self, sha256: str) -> Asset | None:
        return await self.session.scalar(select(Asset).where(
            Asset.organization_id == self.organization_id, Asset.sha256 == sha256))

    async def create_pending(self, *, asset_id: UUID, actor_id: UUID, bucket: str, key: str,
                             filename: str, mime_type: str, byte_size: int, sha256: str) -> Asset | None:
        # Tenant hash uniqueness arbitrates concurrent intents without aborting the root.
        await self.session.execute(insert(Asset.__table__).values(
            id=asset_id, organization_id=self.organization_id, uploaded_by=actor_id,
            storage_bucket=bucket, storage_key=key, original_filename=filename,
            kind="image", mime_type=mime_type, byte_size=byte_size, sha256=sha256, status="pending",
        ).on_conflict_do_nothing(index_elements=[Asset.organization_id, Asset.sha256]))
        return await self.find_by_sha256(sha256)

    async def transition(self, asset: Asset, *, status: str, width: int | None, height: int | None) -> Asset:
        await self.session.execute(update(Asset).where(
            Asset.id == asset.id, Asset.organization_id == self.organization_id, Asset.status == "pending",
        ).values(status=status, width=width, height=height))
        return await self.find_by_id(asset.id)
