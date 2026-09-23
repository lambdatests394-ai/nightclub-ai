"""Local asset doubles; no network or PostgreSQL and no production credentials."""
import copy
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import hashlib
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from PIL import Image

from backend.app.core.config import Settings
from backend.app.modules.assets.coordinator import AssetCoordinator
from backend.app.modules.assets.schemas import UploadIntent
from backend.app.modules.assets.service import AssetService
from backend.app.modules.assets.storage import SignedDownloadCapability, SignedUploadCapability, StoredObjectMetadata
from backend.app.modules.identity.policy import MemberRole, OrganizationContext
from backend.tests.test_content_api import MemoryIdempotency


def picture(fmt="PNG", size=(4, 3), **options):
    stream = BytesIO()
    Image.new("RGB", size, "red").save(stream, format=fmt, **options)
    return stream.getvalue()


def intent(data=None, mime="image/png", filename="flyer.png"):
    data = picture() if data is None else data
    return UploadIntent(filename=filename, mime_type=mime, byte_size=len(data), sha256=hashlib.sha256(data).hexdigest())


class MemoryAssets:
    def __init__(self, organization_id):
        self.organization_id, self.rows = organization_id, {}
        self.locks = []

    async def find_by_id(self, asset_id, *, lock=False):
        if lock:
            self.locks.append(asset_id)
        row = self.rows.get(asset_id)
        return row if row and row.organization_id == self.organization_id else None

    async def find_by_sha256(self, digest):
        return next((r for r in self.rows.values() if r.organization_id == self.organization_id and r.sha256 == digest), None)

    async def create_pending(self, *, asset_id, actor_id, bucket, key, filename, mime_type, byte_size, sha256):
        row = SimpleNamespace(id=asset_id, organization_id=self.organization_id, uploaded_by=actor_id,
            storage_bucket=bucket, storage_key=key, original_filename=filename, kind="image", mime_type=mime_type,
            byte_size=byte_size, sha256=sha256, status="pending", width=None, height=None, created_at=datetime.now(UTC))
        self.rows[asset_id] = row
        return row

    async def transition(self, asset, *, status, width, height):
        asset.status, asset.width, asset.height = status, width, height
        return asset


class FakeStorage:
    def __init__(self, data=None):
        self.data = picture() if data is None else data
        self.error = self.sign_error = None
        self.uploads, self.downloads, self.reads = [], [], 0
        self.stream_closed = False

    async def create_signed_upload(self, bucket, key, *, mime_type, upsert=False):
        assert not upsert
        self.uploads.append((bucket, key, mime_type, upsert))
        if self.sign_error:
            raise self.sign_error()
        return SignedUploadCapability(f"https://storage.example.test/upload?token=synthetic-{len(self.uploads)}",
                                      datetime.now(UTC) + timedelta(seconds=7200), headers={"Content-Type": mime_type})

    async def create_signed_download(self, bucket, key, *, expires_in):
        self.downloads.append((bucket, key, expires_in))
        return SignedDownloadCapability("https://storage.example.test/download?token=synthetic",
                                        datetime.now(UTC) + timedelta(seconds=expires_in))

    async def stat_object(self, bucket, key):
        if self.error:
            raise self.error()
        return StoredObjectMetadata(bucket, key, len(self.data), "untrusted/not-proof")

    async def stream_object(self, bucket, key):
        self.reads += 1
        try:
            for offset in range(0, len(self.data), 1024):
                yield self.data[offset:offset + 1024]
        finally:
            self.stream_closed = True


class AssetFixture:
    def __init__(self, role=MemberRole.OWNER):
        self.context = OrganizationContext(uuid4(), uuid4(), role)
        self.repository, self.idempotency = MemoryAssets(self.context.organization_id), MemoryIdempotency()
        self.audit, self.provider = AsyncMock(), FakeStorage()
        self.settings = Settings(_env_file=None)
        self.service = AssetService(self.context, self.repository, self.idempotency, self.audit, self.settings)
        self.events, self.fail_commit = [], False
        self.coordinator = AssetCoordinator(self.transaction, self.provider, download_ttl=60, timeout=15)

    @asynccontextmanager
    async def transaction(self):
        before = copy.deepcopy((self.repository.rows, self.idempotency.rows))
        try:
            yield self.service
            self.events.append("commit")
            if self.fail_commit:
                raise ConnectionError("synthetic commit failure")
        except BaseException:
            self.repository.rows, self.idempotency.rows = before
            self.events.append("rollback")
            raise
