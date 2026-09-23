"""Asset audit projection: structural fields only; no filename, key or capability."""
from sqlalchemy import insert
from backend.app.modules.audit.models import AuditLog
from backend.app.modules.assets.models import Asset


class AssetAuditWriter:
    def __init__(self, session, context, correlation_id):
        self.session, self.context, self.correlation_id = session, context, correlation_id

    async def write(self, action: str, asset: Asset, *, previous_status: str | None = None,
                    rejection_code: str | None = None) -> None:
        await self.session.execute(insert(AuditLog.__table__).inline().values(
            organization_id=self.context.organization_id, actor_type="user", actor_id=str(self.context.user_id),
            action=action, entity_type="asset", entity_id=asset.id, correlation_id=self.correlation_id,
            after={"organizationId": str(self.context.organization_id), "assetId": str(asset.id),
                   "previousStatus": previous_status, "status": asset.status, "kind": asset.kind,
                   "mimeType": asset.mime_type, "byteSize": asset.byte_size, "width": asset.width,
                   "height": asset.height, "rejectionCode": rejection_code},
        ))
