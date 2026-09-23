"""Asset decisions in the protected root transaction. Signing is owned by coordinator."""
from dataclasses import dataclass
from uuid import UUID, uuid4

from backend.app.core.config import Settings
from backend.app.modules.assets.audit import AssetAuditWriter
from backend.app.modules.assets.errors import AssetConflict, AssetNotFound, AssetProtocolError, AssetUnavailable, InvalidAssetRequest
from backend.app.modules.assets.filenames import storage_key
from backend.app.modules.assets.repository import AssetRepository
from backend.app.modules.assets.schemas import AssetRead, UploadIntent
from backend.app.modules.assets.storage import (
    StorageConflict, StorageError, StorageInvalidResponse, StorageMissing, StorageProvider, StorageTimeout, StorageUnavailable,
)
from backend.app.modules.assets.verification import verify_object
from backend.app.modules.identity.policy import OrganizationContext, Permission, require_permission
from backend.app.shared.idempotency import IdempotencyStore, fingerprint


@dataclass(frozen=True)
class ObjectTarget:
    asset_id: UUID
    bucket: str
    key: str
    mime_type: str


def mapped_storage_error(error: StorageError):
    if isinstance(error, (StorageTimeout, StorageUnavailable)):
        return AssetUnavailable()
    if isinstance(error, StorageInvalidResponse):
        return AssetProtocolError()
    if isinstance(error, (StorageMissing, StorageConflict)):
        return AssetConflict()
    return AssetProtocolError()


class AssetService:
    def __init__(self, context: OrganizationContext, repository: AssetRepository, idempotency: IdempotencyStore,
                 audit: AssetAuditWriter, settings: Settings):
        self.context, self.repository, self.idempotency = context, repository, idempotency
        self.audit, self.settings = audit, settings

    def target(self, asset) -> ObjectTarget:
        if asset.kind != "image":
            raise AssetConflict()
        try:
            expected = storage_key(self.context.organization_id, asset.id, asset.original_filename or "", asset.mime_type)
        except (ValueError, UnicodeError):
            raise AssetProtocolError() from None
        if asset.storage_bucket != self.settings.storage_bucket or asset.storage_key != expected:
            raise AssetProtocolError()
        return ObjectTarget(asset.id, asset.storage_bucket, asset.storage_key, asset.mime_type)

    def digest(self, operation: str, payload: dict) -> str:
        return fingerprint(operation, {"organization": str(self.context.organization_id),
            "actor": str(self.context.user_id), "payload": payload})

    @staticmethod
    def serialize(asset) -> dict:
        return AssetRead.model_validate(asset).model_dump(mode="json", by_alias=True)

    async def upload_intent(self, key: UUID, body: UploadIntent) -> tuple[int, dict, ObjectTarget | None]:
        require_permission(self.context, Permission.ASSET_WRITE)
        if body.byte_size > self.settings.asset_max_image_bytes:
            raise InvalidAssetRequest()
        operation = "asset:upload-intent"
        replay = await self.idempotency.claim(key, operation, self.digest(operation, body.model_dump()))
        if replay is not None:
            status, data = replay
            asset = await self.repository.find_by_id(UUID(data["asset"]["id"]))
            if asset is None or asset.status == "deleted":
                raise AssetNotFound()
            return status, data, self.target(asset) if asset.status == "pending" else None
        asset = await self.repository.find_by_sha256(body.sha256)
        created = False
        if asset is None:
            asset_id = uuid4()
            asset = await self.repository.create_pending(asset_id=asset_id, actor_id=self.context.user_id,
                bucket=self.settings.storage_bucket, key=storage_key(self.context.organization_id, asset_id, body.filename, body.mime_type),
                filename=body.filename, mime_type=body.mime_type, byte_size=body.byte_size, sha256=body.sha256)
            created = asset is not None and asset.id == asset_id
        if not created:
            if (asset is None or asset.status != "ready" or asset.kind != "image"
                    or asset.mime_type != body.mime_type or asset.byte_size != body.byte_size):
                raise AssetConflict()
        if created:
            await self.audit.write("asset.upload_intent_created", asset)
        status, data = (201 if created else 200), {"asset": self.serialize(asset)}
        await self.idempotency.complete(key, status, data)
        return status, data, self.target(asset) if created else None

    async def complete(self, asset_id: UUID, key: UUID, provider: StorageProvider) -> tuple[int, dict]:
        require_permission(self.context, Permission.ASSET_WRITE)
        operation = f"asset:complete:{asset_id}"
        replay = await self.idempotency.claim(key, operation, self.digest(operation, {}))
        if replay is not None:
            return replay
        asset = await self.repository.find_by_id(asset_id, lock=True)
        if asset is None:
            raise AssetNotFound()
        if asset.status != "pending":
            raise AssetConflict()
        target = self.target(asset)
        try:
            result = await verify_object(provider, bucket=target.bucket, key=target.key, expected_size=asset.byte_size,
                expected_sha=asset.sha256, expected_mime=asset.mime_type, max_bytes=self.settings.asset_max_image_bytes,
                timeout=self.settings.asset_verification_timeout_seconds)
        except StorageError as error:
            raise mapped_storage_error(error) from None
        status = "rejected" if result.rejection_code else "ready"
        asset = await self.repository.transition(asset, status=status, width=result.width, height=result.height)
        await self.audit.write("asset." + status, asset, previous_status="pending", rejection_code=result.rejection_code)
        data = {"asset": self.serialize(asset), "rejectionCode": result.rejection_code}
        await self.idempotency.complete(key, 200, data)
        return 200, data

    async def download_target(self, asset_id: UUID) -> ObjectTarget:
        require_permission(self.context, Permission.ASSET_READ)
        asset = await self.repository.find_by_id(asset_id)
        if asset is None or asset.status == "deleted":
            raise AssetNotFound()
        if asset.status != "ready":
            raise AssetConflict()
        return self.target(asset)
