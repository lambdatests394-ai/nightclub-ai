"""Strict HMAC-authenticated, short-lived Facebook OAuth state tokens."""

import base64
import binascii
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Callable
from uuid import UUID

from pydantic import SecretStr

from backend.app.modules.integrations.errors import (
    FacebookInvalidOAuthState,
    FacebookNotConfigured,
    FacebookOAuthStateExpired,
)

STATE_VERSION = 1
DEFAULT_STATE_TTL = timedelta(minutes=10)
MAX_FUTURE_SKEW = timedelta(seconds=60)
_FIELDS = {"actorId", "expiresAt", "issuedAt", "nonce", "organizationId", "requestedPageId", "v"}


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    if not value or "=" in value:
        raise ValueError()
    raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    if _encode(raw) != value:
        raise ValueError()
    return raw


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def state_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def nonce_digest(nonce: str) -> str:
    try:
        return hashlib.sha256(_decode(nonce)).hexdigest()
    except (ValueError, binascii.Error, UnicodeError):
        raise FacebookInvalidOAuthState() from None


@dataclass(frozen=True, slots=True)
class VerifiedOAuthState:
    organization_id: UUID
    actor_id: UUID
    requested_page_id: str
    issued_at: datetime
    expires_at: datetime
    version: int
    nonce: SecretStr = field(repr=False)


@dataclass(frozen=True, slots=True)
class IssuedOAuthState:
    state: VerifiedOAuthState
    token: SecretStr = field(repr=False)
    state_digest: str
    nonce_digest: str

    def reveal_token(self) -> str:
        return self.token.get_secret_value()


class OAuthStateCodec:
    def __init__(self, key: SecretStr | str, *, ttl: timedelta = DEFAULT_STATE_TTL,
                 clock: Callable[[], datetime] | None = None):
        encoded = key.get_secret_value() if isinstance(key, SecretStr) else key
        if not encoded:
            raise FacebookNotConfigured()
        try:
            raw = _decode(encoded)
            if len(raw) != 32:
                raise ValueError()
        except (ValueError, binascii.Error, UnicodeError):
            raise FacebookNotConfigured() from None
        if ttl <= timedelta(0) or ttl > DEFAULT_STATE_TTL:
            raise FacebookNotConfigured()
        self._key = raw
        self._ttl = ttl
        self._clock = clock or (lambda: datetime.now(UTC))

    def __repr__(self) -> str:
        return "OAuthStateCodec(key=**********)"

    @staticmethod
    def _timestamp(value: datetime) -> int:
        if value.tzinfo is None or value.utcoffset() is None:
            raise FacebookInvalidOAuthState()
        return int(value.astimezone(UTC).timestamp())

    @staticmethod
    def _page_id(value: str) -> str:
        if not isinstance(value, str) or not value.isascii() or not value.isdigit() or value.startswith("0") or len(value) > 128:
            raise FacebookInvalidOAuthState()
        return value

    def _issue(
            self, organization_id: UUID, actor_id: UUID, requested_page_id: str,
            *, issued_at: datetime, nonce_bytes: bytes) -> IssuedOAuthState:
        now = issued_at.astimezone(UTC).replace(microsecond=0)
        expires = now + self._ttl
        if len(nonce_bytes) != 32:
            raise FacebookInvalidOAuthState()
        nonce = _encode(nonce_bytes)
        page_id = self._page_id(requested_page_id)
        payload = {
            "actorId": str(actor_id),
            "expiresAt": self._timestamp(expires),
            "issuedAt": self._timestamp(now),
            "nonce": nonce,
            "organizationId": str(organization_id),
            "requestedPageId": page_id,
            "v": STATE_VERSION,
        }
        encoded_payload = _encode(_canonical(payload))
        signature = _encode(hmac.digest(self._key, encoded_payload.encode("ascii"), "sha256"))
        token = encoded_payload + "." + signature
        state = VerifiedOAuthState(organization_id, actor_id, page_id, now, expires,
                                   STATE_VERSION, SecretStr(nonce))
        return IssuedOAuthState(state, SecretStr(token), state_digest(token), nonce_digest(nonce))

    def issue(self, organization_id: UUID, actor_id: UUID,
              requested_page_id: str) -> IssuedOAuthState:
        return self._issue(
            organization_id, actor_id, requested_page_id,
            issued_at=self._clock(), nonce_bytes=secrets.token_bytes(32),
        )

    def issue_idempotent(
            self, organization_id: UUID, actor_id: UUID, requested_page_id: str,
            idempotency_key: UUID, *, issued_at: datetime) -> IssuedOAuthState:
        """Reconstruct one OAuth-start token without persisting it in replay data."""

        page_id = self._page_id(requested_page_id)
        material = b"nightclub-ai:facebook-oauth-start:v1\x00" + b"\x00".join((
            organization_id.bytes, actor_id.bytes, idempotency_key.bytes,
            page_id.encode("ascii"),
        ))
        nonce_bytes = hmac.digest(self._key, material, "sha256")
        return self._issue(
            organization_id, actor_id, page_id,
            issued_at=issued_at, nonce_bytes=nonce_bytes,
        )

    def verify(self, token: str) -> VerifiedOAuthState:
        try:
            if not isinstance(token, str) or token.count(".") != 1:
                raise ValueError()
            encoded_payload, encoded_mac = token.split(".")
            supplied_mac = _decode(encoded_mac)
            expected_mac = hmac.digest(self._key, encoded_payload.encode("ascii"), "sha256")
            if len(supplied_mac) != len(expected_mac) or not hmac.compare_digest(supplied_mac, expected_mac):
                raise ValueError()
            raw_payload = _decode(encoded_payload)
            payload = json.loads(raw_payload)
            if not isinstance(payload, dict) or set(payload) != _FIELDS or _canonical(payload) != raw_payload:
                raise ValueError()
            if type(payload["v"]) is not int or payload["v"] != STATE_VERSION:
                raise ValueError()
            if type(payload["issuedAt"]) is not int or type(payload["expiresAt"]) is not int:
                raise ValueError()
            organization_id = UUID(payload["organizationId"])
            actor_id = UUID(payload["actorId"])
            if str(organization_id) != payload["organizationId"] or str(actor_id) != payload["actorId"]:
                raise ValueError()
            page_id = self._page_id(payload["requestedPageId"])
            nonce = payload["nonce"]
            if not isinstance(nonce, str) or len(_decode(nonce)) != 32:
                raise ValueError()
            issued = datetime.fromtimestamp(payload["issuedAt"], UTC)
            expires = datetime.fromtimestamp(payload["expiresAt"], UTC)
            if expires <= issued or expires - issued > self._ttl:
                raise ValueError()
            now = self._clock().astimezone(UTC)
            if issued > now + MAX_FUTURE_SKEW:
                raise ValueError()
            if now >= expires:
                raise FacebookOAuthStateExpired()
            return VerifiedOAuthState(organization_id, actor_id, page_id, issued, expires,
                                      STATE_VERSION, SecretStr(nonce))
        except FacebookOAuthStateExpired:
            raise
        except (ValueError, TypeError, KeyError, OverflowError, UnicodeError,
                binascii.Error, json.JSONDecodeError):
            raise FacebookInvalidOAuthState() from None
