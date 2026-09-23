"""Explicit commit-before-capability boundary; no FastAPI yield-order assumptions."""
import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from uuid import UUID

from backend.app.modules.assets.errors import AssetUnavailable
from backend.app.modules.assets.schemas import UploadIntent
from backend.app.modules.assets.service import AssetService, mapped_storage_error
from backend.app.modules.assets.storage import StorageError, StorageProvider


class AssetCoordinator:
    def __init__(self, transaction: Callable[[], AbstractAsyncContextManager[AssetService]],
                 provider: StorageProvider, *, download_ttl: int, timeout: int):
        self._transaction, self._provider = transaction, provider
        self._download_ttl, self._timeout = download_ttl, timeout

    async def upload(self, key: UUID, body: UploadIntent) -> tuple[int, dict]:
        async with self._transaction() as service:
            status, durable, target = await service.upload_intent(key, body)
        # Exit has completed the root commit. Never move signing into the block.
        upload = None
        if target is not None:
            try:
                async with asyncio.timeout(self._timeout):
                    capability = await self._provider.create_signed_upload(target.bucket, target.key,
                        mime_type=target.mime_type, upsert=False)
            except StorageError:
                # Every signing failure is recoverable by the durable pending intent.
                raise AssetUnavailable() from None
            except TimeoutError:
                raise AssetUnavailable() from None
            upload = {"url": capability.url, "expiresAt": capability.expires_at.isoformat(),
                      "method": capability.method, "headers": capability.headers}
        return status, {**durable, "upload": upload}

    async def complete(self, asset_id: UUID, key: UUID) -> tuple[int, dict]:
        async with self._transaction() as service:
            result = await service.complete(asset_id, key, self._provider)
        return result

    async def download(self, asset_id: UUID) -> dict:
        async with self._transaction() as service:
            target = await service.download_target(asset_id)
        try:
            async with asyncio.timeout(self._timeout):
                capability = await self._provider.create_signed_download(target.bucket, target.key, expires_in=self._download_ttl)
        except StorageError as error:
            raise mapped_storage_error(error) from None
        except TimeoutError:
            raise AssetUnavailable() from None
        return {"assetId": str(asset_id), "downloadUrl": capability.url, "expiresAt": capability.expires_at.isoformat()}
