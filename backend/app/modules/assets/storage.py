"""Provider-neutral storage port. Capabilities are ephemeral, never persistence DTOs."""
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

UPLOAD_CAPABILITY_SECONDS = 7200  # Provider-defined; not an application TTL setting.


class StorageError(Exception):
    """Adapter must raise without retaining provider messages or credentials."""


class StorageMissing(StorageError): pass
class StorageTimeout(StorageError): pass
class StorageUnavailable(StorageError): pass
class StorageConflict(StorageError): pass
class StorageInvalidResponse(StorageError): pass


@dataclass(frozen=True)
class SignedUploadCapability:
    url: str = field(repr=False)
    expires_at: datetime
    method: str = "PUT"
    headers: dict[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class SignedDownloadCapability:
    url: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True)
class StoredObjectMetadata:
    bucket: str
    key: str
    byte_size: int | None = None
    content_type: str | None = None
    version_id: str | None = None


class StorageProvider(Protocol):
    async def create_signed_upload(self, bucket: str, key: str, *, mime_type: str, upsert: bool = False) -> SignedUploadCapability: ...
    async def stat_object(self, bucket: str, key: str) -> StoredObjectMetadata: ...
    def stream_object(self, bucket: str, key: str) -> AsyncIterator[bytes]: ...
    async def create_signed_download(self, bucket: str, key: str, *, expires_in: int) -> SignedDownloadCapability: ...
