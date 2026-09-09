import base64
import hashlib
import hmac
import json
from dataclasses import FrozenInstanceError, asdict
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import ValidationError

from backend.app.core.config import Settings
from backend.app.modules.identity.authentication import SupabaseJWTVerifier
from backend.app.modules.identity.errors import AuthUnavailable, SecurityError

pytestmark = pytest.mark.anyio


async def test_verified_current_user_contains_only_immutable_subject(security_material):
    material = security_material
    user = await material.verifier.verify(material.token())
    assert asdict(user) == {"user_id": material.user_id}
    with pytest.raises(FrozenInstanceError):
        user.user_id = uuid4()


@pytest.mark.parametrize("claim", ["iss", "aud", "exp", "iat", "sub", "role"])
async def test_missing_required_claim_is_rejected(security_material, claim):
    with pytest.raises(SecurityError) as error:
        await security_material.verifier.verify(security_material.token(omit=[claim]))
    assert error.value.status == 401


@pytest.mark.parametrize("changes", [
    {"iss": "https://identity.example.test/auth/v1/evil"}, {"iss": ["https://identity.example.test/auth/v1"]},
    {"aud": "other"}, {"aud": ["other"]}, {"aud": ["authenticated", 7]}, {"aud": None},
    {"sub": "not-uuid"}, {"sub": 12}, {"sub": ""},
    {"role": "anon"}, {"role": "service_role"}, {"role": "owner"}, {"role": None},
    {"is_anonymous": True}, {"is_anonymous": "false"},
    {"iat": "1800000000"}, {"iat": True}, {"iat": None}, {"iat": float("inf")},
    {"exp": "1800000600"}, {"exp": True}, {"exp": float("nan")},
    {"nbf": "1800000000"}, {"nbf": False},
])
async def test_invalid_human_claims_are_rejected(security_material, changes):
    with pytest.raises(SecurityError) as error:
        await security_material.verifier.verify(security_material.token(changes=changes))
    assert error.value.status == 401


@pytest.mark.parametrize("audience", ["authenticated", ["authenticated"], ["other", "authenticated"]])
async def test_standard_string_and_array_audience(security_material, audience):
    assert (await security_material.verifier.verify(
        security_material.token(changes={"aud": audience, "is_anonymous": False})
    )).user_id == security_material.user_id


@pytest.mark.parametrize("claim,offset,allowed", [
    ("exp", -61, False), ("exp", -60, False), ("exp", -59, True),
    ("nbf", 61, False), ("nbf", 60, True), ("nbf", 59, True),
    ("iat", 61, False), ("iat", 60, True), ("iat", 59, True),
])
async def test_exp_nbf_iat_explicit_leeway_boundaries(security_material, claim, offset, allowed):
    token = security_material.token(changes={claim: security_material.now + offset})
    if allowed:
        assert await security_material.verifier.verify(token)
    else:
        with pytest.raises(SecurityError):
            await security_material.verifier.verify(token)


async def test_invalid_signature_never_uses_claimed_user(security_material):
    other_key = ec.generate_private_key(ec.SECP256R1())
    with pytest.raises(SecurityError):
        await security_material.verifier.verify(security_material.token(key=other_key))


async def test_alg_none_is_rejected_before_provider_fetch(security_material):
    token = jwt.encode(security_material.claims(), key=None, algorithm="none", headers={"kid": "local-key-1"})
    with pytest.raises(SecurityError):
        await security_material.verifier.verify(token)
    assert security_material.provider.calls == 0


async def test_algorithm_confusion_public_key_as_hmac_secret_is_rejected(security_material):
    def encoded(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=")
    header = encoded({"alg": "HS256", "kid": "local-key-1"})
    message = header + b"." + encoded(security_material.claims())
    public_bytes = security_material.private_key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    signature = hmac.new(public_bytes, message, hashlib.sha256).digest()
    token = (message + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode()
    with pytest.raises(SecurityError):
        await security_material.verifier.verify(token)
    assert security_material.provider.calls == 0


@pytest.mark.parametrize("token", ["", "bad", "a.b.c", "x" * 16385])
async def test_malformed_jwt(security_material, token):
    with pytest.raises(SecurityError):
        await security_material.verifier.verify(token)


@pytest.mark.parametrize("headers", [{"kid": ""}, {"kid": "x" * 129}, {"typ": "JWT"},
                                    {"kid": "local-key-1", "crit": ["unhandled"]}])
async def test_invalid_key_selector_or_critical_header(security_material, headers):
    with pytest.raises(SecurityError):
        await security_material.verifier.verify(security_material.token(headers=headers))


async def test_token_urls_never_override_trusted_provider(security_material):
    token = security_material.token(headers={"kid": "local-key-1", "jku": "https://evil.test",
                                             "x5u": "https://evil.test/key"})
    assert await security_material.verifier.verify(token)
    assert security_material.provider.calls == 1


@pytest.mark.parametrize("algorithms", [[], ["HS256"], ["RS256"], ["none"], ["ES256", "HS256"]])
async def test_settings_cannot_expand_algorithms(algorithms):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, supabase_jwt_allowed_algorithms=algorithms)


@pytest.mark.parametrize("name,value", [
    ("supabase_jwks_cache_ttl_seconds", 0), ("supabase_jwks_cache_ttl_seconds", 601),
    ("jwt_clock_skew_seconds", -1), ("supabase_jwks_url", "http://example.test/jwks"),
    ("supabase_jwks_url", "https://user:placeholder@example.test/jwks"),
    ("supabase_jwks_url", "https://example.test/jwks?key=placeholder"),
])
async def test_invalid_security_configuration(name, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{name: value})


async def test_missing_security_configuration_fails_closed(security_material):
    with pytest.raises(AuthUnavailable):
        SupabaseJWTVerifier(Settings(_env_file=None), security_material.cache)
