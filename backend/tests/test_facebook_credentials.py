import base64
import os
from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic import SecretStr
from pydantic import ValidationError

from backend.app.core.config import Settings

from backend.app.modules.integrations.credentials import (
    CredentialBinding,
    CredentialCipher,
    FacebookCredentials,
    decode_base64url_key,
)
from backend.app.modules.integrations.errors import (
    FacebookCredentialError,
    FacebookNotConfigured,
    FacebookUnknownKeyVersion,
)


def encoded_key(raw=None):
    return base64.urlsafe_b64encode(raw or bytes(range(32))).decode().rstrip("=")


@pytest.fixture
def cipher():
    return CredentialCipher(SecretStr(encoded_key()), 1)


@pytest.fixture
def binding():
    return CredentialBinding(uuid4(), "facebook", "123456", 1)


def test_canonical_key_is_exactly_32_bytes():
    assert decode_base64url_key(encoded_key()) == bytes(range(32))


@pytest.mark.parametrize("value", ["", "not_base64!", encoded_key(b"short"), encoded_key() + "="])
def test_invalid_or_noncanonical_key_fails_closed(value):
    error = FacebookNotConfigured if value == "" else FacebookCredentialError
    with pytest.raises(error):
        decode_base64url_key(value)


def test_aes_gcm_round_trip_and_random_nonce(cipher, binding):
    credentials = FacebookCredentials(SecretStr("synthetic-token-never-log"), "bearer")
    first = cipher.encrypt(credentials, binding)
    second = cipher.encrypt(credentials, binding)
    assert first != second
    restored = cipher.decrypt(first, binding)
    assert restored.access_token.get_secret_value() == "synthetic-token-never-log"
    assert restored.token_type == "bearer"
    assert b"synthetic-token-never-log" not in first


@pytest.mark.parametrize("changed", [
    lambda b: replace(b, organization_id=uuid4()),
    lambda b: replace(b, platform="whatsapp"),
    lambda b: replace(b, external_account_id="999"),
])
def test_aad_binding_rejects_wrong_identity(cipher, binding, changed):
    envelope = cipher.encrypt(FacebookCredentials(SecretStr("secret")), binding)
    with pytest.raises(FacebookCredentialError):
        cipher.decrypt(envelope, changed(binding))


def test_key_version_dispatch_and_aad_are_exact(cipher, binding):
    envelope = cipher.encrypt(FacebookCredentials(SecretStr("secret")), binding)
    with pytest.raises(FacebookUnknownKeyVersion):
        cipher.decrypt(envelope, replace(binding, credential_key_version=2))
    with pytest.raises(FacebookUnknownKeyVersion):
        cipher.encrypt(FacebookCredentials(SecretStr("secret")), replace(binding, credential_key_version=2))


def test_modified_ciphertext_and_malformed_envelopes_are_sanitized(cipher, binding):
    token = "sensitive-access-token"
    envelope = bytearray(cipher.encrypt(FacebookCredentials(SecretStr(token)), binding))
    envelope[-1] ^= 1
    for invalid in (bytes(envelope), b"", b"NCAIC\x02" + os.urandom(40), b"NCAIC\x01short"):
        with pytest.raises(FacebookCredentialError) as error:
            cipher.decrypt(invalid, binding)
        assert token not in str(error.value)


def test_secret_holding_repr_and_str_are_redacted(cipher):
    credentials = FacebookCredentials(SecretStr("never-visible"), "bearer")
    assert "never-visible" not in repr(credentials)
    assert "never-visible" not in str(credentials)
    assert encoded_key() not in repr(cipher)
    assert "**********" in repr(cipher)


def test_meta_configuration_is_optional_secret_safe_and_strict():
    settings = Settings(
        _env_file=None, meta_app_secret="synthetic-app-secret",
        meta_credential_encryption_key=encoded_key(), meta_oauth_state_key=encoded_key(os.urandom(32)),
        meta_oauth_redirect_uri="https://app.example.test/oauth/callback",
        meta_graph_api_version="v24.0",
    )
    rendered = repr(settings) + repr(settings.model_dump())
    assert "synthetic-app-secret" not in rendered and encoded_key() not in rendered
    assert Settings(_env_file=None).meta_app_id == ""
    for values in ({"meta_oauth_redirect_uri": "http://example.test/callback"},
                   {"meta_graph_api_version": " v24.0"},
                   {"meta_credential_key_version": 0}):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **values)
