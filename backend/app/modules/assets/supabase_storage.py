"""httpx-only Supabase adapter; credential-bearing traffic stays on one HTTPS origin."""
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
import json
import re
from urllib.parse import parse_qs, quote, urlsplit

import httpx

from backend.app.core.config import Settings
from backend.app.modules.assets.storage import (
    UPLOAD_CAPABILITY_SECONDS, SignedDownloadCapability, SignedUploadCapability,
    StorageConflict, StorageInvalidResponse, StorageMissing, StorageTimeout,
    StorageUnavailable, StoredObjectMetadata,
)

KEY_PATTERN = re.compile(r"org/[0-9a-f-]{36}/assets/[0-9a-f-]{36}/[a-z0-9_-]{1,80}\.(?:jpg|png|webp)")


class SupabaseStorage:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self._settings, self._client = settings, client
        self._origin = settings.supabase_url

    def _target(self, bucket: str, key: str, route: str) -> str:
        if (not self._origin or not self._settings.supabase_service_role_key.get_secret_value()):
            raise StorageUnavailable()
        if bucket != self._settings.storage_bucket or not KEY_PATTERN.fullmatch(key):
            raise StorageInvalidResponse()
        return f"{self._origin}/storage/v1/{route}/{quote(bucket, safe='')}/{quote(key, safe='/')}"

    def _headers(self) -> dict[str, str]:
        credential = self._settings.supabase_service_role_key.get_secret_value()
        return {"Authorization": "Bearer " + credential, "apikey": credential, "Accept-Encoding": "identity"}

    @staticmethod
    def _status(response: httpx.Response, document: object = None) -> None:
        # Storage may wrap not-found/conflict in a 400 JSON response.
        code = document.get("code", document.get("error", "")) if isinstance(document, dict) else ""
        if not isinstance(code, str):
            raise StorageInvalidResponse()
        if response.status_code == 404 or (response.status_code == 400 and code in {"NoSuchKey", "not_found", "not-found", "Object not found"}):
            raise StorageMissing()
        if response.status_code == 409 or (response.status_code == 400 and code in {"Duplicate", "ResourceAlreadyExists"}):
            raise StorageConflict()
        if response.status_code in {408, 504}:
            raise StorageTimeout()
        if response.status_code in {401, 403, 429} or response.status_code >= 500:
            raise StorageUnavailable()
        if not 200 <= response.status_code < 300:
            raise StorageInvalidResponse()

    async def _json(self, url: str, *, payload: dict, headers: dict | None = None) -> dict:
        try:
            async with self._client.stream("POST", url, json=payload,
                    headers={**self._headers(), **(headers or {})},
                    timeout=self._settings.asset_verification_timeout_seconds, follow_redirects=False) as response:
                data = bytearray()
                async for chunk in response.aiter_bytes(4096):
                    if len(data) + len(chunk) > 65_536:
                        raise StorageInvalidResponse()
                    data.extend(chunk)
                try:
                    document = json.loads(data)
                except (ValueError, UnicodeError):
                    self._status(response)
                    raise StorageInvalidResponse() from None
                self._status(response, document)
                if not isinstance(document, dict):
                    raise StorageInvalidResponse()
                return document
        except httpx.TimeoutException:
            raise StorageTimeout() from None
        except (httpx.RemoteProtocolError, httpx.DecodingError):
            raise StorageInvalidResponse() from None
        except httpx.RequestError:
            raise StorageUnavailable() from None

    def _capability_url(self, value: object, expected: str) -> str:
        if not isinstance(value, str) or len(value) > 16384 or any(ord(c) < 33 for c in value):
            raise StorageInvalidResponse()
        # REST uses paths relative to /storage/v1; tolerate a full same-origin URL.
        url = value if value.startswith("https://") else self._origin + (
            value if value.startswith("/storage/v1/") else "/storage/v1" + value)
        try:
            parts, target = urlsplit(url), urlsplit(expected)
            query = parse_qs(parts.query, keep_blank_values=True, max_num_fields=4)
        except ValueError:
            raise StorageInvalidResponse() from None
        if (parts.scheme != target.scheme or parts.netloc != target.netloc
                or parts.path != target.path or parts.fragment
                or set(query) != {"token"} or len(query["token"]) != 1 or not query["token"][0]
                or self._settings.supabase_service_role_key.get_secret_value() in url):
            raise StorageInvalidResponse()
        return url

    async def create_signed_upload(self, bucket: str, key: str, *, mime_type: str, upsert: bool = False) -> SignedUploadCapability:
        if upsert or mime_type not in {"image/jpeg", "image/png", "image/webp"}:
            raise StorageInvalidResponse()
        target = self._target(bucket, key, "object/upload/sign")
        issued = datetime.now(UTC)
        document = await self._json(target, payload={}, headers={"x-upsert": "false"})
        return SignedUploadCapability(self._capability_url(document.get("url"), target),
            issued + timedelta(seconds=UPLOAD_CAPABILITY_SECONDS), headers={"Content-Type": mime_type})

    async def create_signed_download(self, bucket: str, key: str, *, expires_in: int) -> SignedDownloadCapability:
        if not 1 <= expires_in <= 60:
            raise StorageInvalidResponse()
        target = self._target(bucket, key, "object/sign")
        issued = datetime.now(UTC)
        document = await self._json(target, payload={"expiresIn": expires_in})
        return SignedDownloadCapability(self._capability_url(document.get("signedURL"), target),
                                        issued + timedelta(seconds=expires_in))

    async def stat_object(self, bucket: str, key: str) -> StoredObjectMetadata:
        try:
            response = await self._client.head(self._target(bucket, key, "object/authenticated"),
                headers=self._headers(), timeout=self._settings.asset_verification_timeout_seconds, follow_redirects=False)
            # Supabase Storage may report a missing canonical object as either
            # 400 or 404 for HEAD, without a useful response body.
            if response.status_code in {400, 404}:
                raise StorageMissing()
            self._status(response)
            raw = response.headers.get("content-length")
            if raw is not None and (not raw.isascii() or not raw.isdecimal()):
                raise StorageInvalidResponse()
            return StoredObjectMetadata(bucket, key, int(raw) if raw else None, response.headers.get("content-type"))
        except httpx.TimeoutException:
            raise StorageTimeout() from None
        except (httpx.RemoteProtocolError, httpx.DecodingError):
            raise StorageInvalidResponse() from None
        except httpx.RequestError:
            raise StorageUnavailable() from None

    async def stream_object(self, bucket: str, key: str) -> AsyncIterator[bytes]:
        try:
            async with self._client.stream("GET", self._target(bucket, key, "object/authenticated"),
                    headers=self._headers(), timeout=self._settings.asset_verification_timeout_seconds, follow_redirects=False) as response:
                self._status(response)
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise StorageInvalidResponse()
                async for chunk in response.aiter_bytes(65_536):
                    yield chunk
        except httpx.TimeoutException:
            raise StorageTimeout() from None
        except (httpx.RemoteProtocolError, httpx.DecodingError):
            raise StorageInvalidResponse() from None
        except httpx.RequestError:
            raise StorageUnavailable() from None
