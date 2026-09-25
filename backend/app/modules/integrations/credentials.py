"""Versioned AES-256-GCM envelope for minimal normalized Meta credentials."""

import base64
import binascii
import json
import os
from dataclasses import dataclass, field
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

from backend.app.modules.integrations.errors import (
    FacebookCredentialError,
    FacebookNotConfigured,
    FacebookUnknownKeyVersion,
)

_MAGIC = b"NCAIC"
_ENVELOPE_VERSION = 1
_NONCE_SIZE = 12


def decode_base64url_key(value: SecretStr | str) -> bytes:
    """Decode one canonical, unpadded base64url AES-256 key."""

    encoded = value.get_secret_value() if isinstance(value, SecretStr) else value
    if not encoded:
        raise FacebookNotConfigured() from None
    try:
        if "=" in encoded or len(encoded) != 43:
            raise ValueError()
        raw = base64.b64decode(encoded + "=", altchars=b"-_", validate=True)
        if len(raw) != 32 or base64.urlsafe_b64encode(raw).decode().rstrip("=") != encoded:
            raise ValueError()
        return raw
    except (ValueError, UnicodeError, binascii.Error):
        raise FacebookCredentialError() from None


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


@dataclass(frozen=True, slots=True)
class CredentialBinding:
    organization_id: UUID
    platform: str
    external_account_id: str
    credential_key_version: int

    def aad(self) -> bytes:
        return _canonical({
            "credentialKeyVersion": self.credential_key_version,
            "externalAccountId": self.external_account_id,
            "organizationId": str(self.organization_id),
            "platform": self.platform,
            "v": 1,
        })


@dataclass(frozen=True, slots=True)
class FacebookCredentials:
    access_token: SecretStr = field(repr=False)
    token_type: str | None = None

    def __str__(self) -> str:
        return "FacebookCredentials(access_token=**********)"


class CredentialCipher:
    """One configured writer key/version; decryption dispatch is exact."""

    def __init__(self, encoded_key: SecretStr | str, key_version: int):
        if key_version < 1:
            raise FacebookCredentialError()
        self._key = decode_base64url_key(encoded_key)
        self.key_version = key_version

    def __repr__(self) -> str:
        return f"CredentialCipher(key=**********, key_version={self.key_version})"

    def encrypt(self, credentials: FacebookCredentials, binding: CredentialBinding) -> bytes:
        if binding.credential_key_version != self.key_version:
            raise FacebookUnknownKeyVersion()
        token = credentials.access_token.get_secret_value()
        if not token:
            raise FacebookCredentialError()
        payload = {"accessToken": token}
        if credentials.token_type is not None:
            payload["tokenType"] = credentials.token_type
        nonce = os.urandom(_NONCE_SIZE)
        ciphertext = AESGCM(self._key).encrypt(nonce, _canonical(payload), binding.aad())
        return _MAGIC + bytes([_ENVELOPE_VERSION]) + nonce + ciphertext

    def decrypt(self, envelope: bytes, binding: CredentialBinding) -> FacebookCredentials:
        if binding.credential_key_version != self.key_version:
            raise FacebookUnknownKeyVersion()
        try:
            prefix = len(_MAGIC)
            if (not isinstance(envelope, bytes) or len(envelope) < prefix + 1 + _NONCE_SIZE + 17
                    or envelope[:prefix] != _MAGIC
                    or envelope[prefix] != _ENVELOPE_VERSION):
                raise ValueError()
            nonce = envelope[prefix + 1:prefix + 1 + _NONCE_SIZE]
            ciphertext = envelope[prefix + 1 + _NONCE_SIZE:]
            plaintext = AESGCM(self._key).decrypt(nonce, ciphertext, binding.aad())
            value = json.loads(plaintext)
            if (not isinstance(value, dict) or set(value) not in ({"accessToken"}, {"accessToken", "tokenType"})
                    or not isinstance(value["accessToken"], str) or not value["accessToken"]
                    or ("tokenType" in value and (not isinstance(value["tokenType"], str)
                                                   or not value["tokenType"]))):
                raise ValueError()
            return FacebookCredentials(SecretStr(value["accessToken"]), value.get("tokenType"))
        except (InvalidTag, ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
            raise FacebookCredentialError() from None
