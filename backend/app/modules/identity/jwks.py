"""Injectable transport and bounded, per-process trusted signing-key cache."""
import asyncio
import json
import time
from collections.abc import Callable
from typing import Protocol
from urllib.parse import urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicKey

from backend.app.modules.identity.errors import AuthUnavailable, SecurityError

MAX_JWKS_BYTES = 256 * 1024
MAX_KEYS = 32
REFRESH_INTERVAL_SECONDS = 30


class JWKSProvider(Protocol):
    async def fetch(self) -> dict: ...


class HTTPJWKSProvider:
    def __init__(self, url: str, client: httpx.AsyncClient) -> None:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment):
            raise ValueError("A trusted HTTPS JWKS URL is required")
        self._url = url
        self._client = client

    async def fetch(self) -> dict:
        try:
            # Total deadline includes streaming; HTTPX timeouts alone are per I/O.
            async with asyncio.timeout(5):
                async with self._client.stream(
                    "GET", self._url, timeout=5, follow_redirects=False,
                ) as response:
                    if response.status_code != 200:
                        raise AuthUnavailable()
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        body.extend(chunk)
                        if len(body) > MAX_JWKS_BYTES:
                            raise AuthUnavailable()
            result = json.loads(body)
            if not isinstance(result, dict):
                raise AuthUnavailable()
            return result
        except (httpx.HTTPError, TimeoutError, ValueError, UnicodeError, RecursionError):
            raise AuthUnavailable() from None


def validated_keys(document: dict) -> dict[str, EllipticCurvePublicKey]:
    """Convert approved public verification material; never accept private keys."""
    try:
        if len(json.dumps(document).encode()) > MAX_JWKS_BYTES:
            raise ValueError()
        keys = document["keys"]
        if not isinstance(keys, list) or not 1 <= len(keys) <= MAX_KEYS:
            raise ValueError()
        result = {}
        seen = set()
        for item in keys:
            if not isinstance(item, dict):
                raise ValueError()
            kid = item.get("kid")
            if not isinstance(kid, str) or not 1 <= len(kid) <= 128 or kid in seen:
                raise ValueError()
            seen.add(kid)
            if "d" in item:
                raise ValueError()
            if item.get("kty") != "EC" or item.get("crv") != "P-256":
                continue
            if item.get("alg", "ES256") != "ES256" or item.get("use", "sig") != "sig":
                continue
            if "key_ops" in item and item["key_ops"] != ["verify"]:
                continue
            key = jwt.PyJWK.from_dict(item, algorithm="ES256").key
            if not isinstance(key, EllipticCurvePublicKey) or key.curve.name != "secp256r1":
                raise ValueError()
            result[kid] = key
        if not result:
            raise ValueError()
        return result
    except (KeyError, TypeError, ValueError, RecursionError, jwt.PyJWTError):
        raise AuthUnavailable() from None


class JWKSCache:
    def __init__(self, provider: JWKSProvider, ttl: int = 300,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if not 0 < ttl <= 600:
            raise ValueError("JWKS TTL must be between 1 and 600 seconds")
        self._provider = provider
        self._ttl = ttl
        self._clock = clock
        self._keys: dict[str, EllipticCurvePublicKey] = {}
        self._expires = 0.0
        self._last_attempt = float("-inf")
        self._last_failed = False
        self._lock = asyncio.Lock()

    async def key_for(self, kid: str) -> EllipticCurvePublicKey:
        async with self._lock:
            now = self._clock()
            if now < self._expires and kid in self._keys:
                return self._keys[kid]
            if now - self._last_attempt < REFRESH_INTERVAL_SECONDS:
                if self._last_failed or now >= self._expires:
                    raise AuthUnavailable()
                # A recent successful snapshot already established key absence.
                raise SecurityError()
            self._last_attempt = now
            self._last_failed = True
            try:
                async with asyncio.timeout(5):
                    replacement = validated_keys(await self._provider.fetch())
            except (AuthUnavailable, TimeoutError, httpx.HTTPError):
                # Keep still-valid old keys, but never extend their TTL on failure.
                raise AuthUnavailable() from None
            self._keys = replacement
            self._expires = self._clock() + self._ttl
            self._last_failed = False
            if kid not in replacement:
                raise SecurityError()
            return replacement[kid]
