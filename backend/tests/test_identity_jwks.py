import asyncio
import copy

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from backend.app.modules.identity.errors import AuthUnavailable, SecurityError
from backend.app.modules.identity.jwks import HTTPJWKSProvider, JWKSCache, MAX_JWKS_BYTES

pytestmark = pytest.mark.anyio


async def test_cache_hit_uses_injected_static_provider(security_material):
    material = security_material
    await material.verifier.verify(material.token())
    await material.verifier.verify(material.token())
    assert material.provider.calls == 1


async def test_unknown_kid_refreshes_once_and_rotation_succeeds(security_material):
    material = security_material
    await material.cache.key_for("local-key-1")
    material.clock.value += 30
    new_private_key = ec.generate_private_key(ec.SECP256R1())
    rotated = jwt.algorithms.ECAlgorithm.to_jwk(new_private_key.public_key(), as_dict=True)
    rotated.update(kid="rotated", alg="ES256", use="sig")
    material.provider.document = {"keys": [rotated]}
    assert await material.verifier.verify(material.token(headers={"kid": "rotated"}, key=new_private_key))
    with pytest.raises(SecurityError) as error:
        await material.verifier.verify(material.token())
    assert error.value.status == 401
    assert material.provider.calls == 2


async def test_unknown_kid_after_successful_refresh_is_401_without_loop(security_material):
    material = security_material
    with pytest.raises(SecurityError) as error:
        await material.cache.key_for("missing")
    assert error.value.status == 401
    for index in range(20):
        with pytest.raises(SecurityError) as error:
            await material.cache.key_for(f"random-{index}")
        assert error.value.status == 401
    assert material.provider.calls == 1
    material.clock.value += 30
    with pytest.raises(SecurityError) as error:
        await material.cache.key_for("missing")
    assert error.value.status == 401
    assert material.provider.calls == 2


async def test_concurrent_refresh_is_shared(security_material):
    material = security_material
    keys = await asyncio.gather(*(material.cache.key_for("local-key-1") for _ in range(30)))
    assert len(keys) == 30
    assert material.provider.calls == 1


async def test_valid_cached_key_survives_outage_and_failed_unknown_kid_refresh(security_material):
    material = security_material
    await material.cache.key_for("local-key-1")
    material.clock.value += 30
    material.provider.error = AuthUnavailable()
    with pytest.raises(AuthUnavailable):
        await material.cache.key_for("rotated")
    assert await material.verifier.verify(material.token())
    assert material.provider.calls == 2


async def test_cache_expiry_requires_refresh_and_never_extends_failed_cache(security_material):
    material = security_material
    await material.cache.key_for("local-key-1")
    material.clock.value += 300
    material.provider.error = AuthUnavailable()
    with pytest.raises(AuthUnavailable):
        await material.verifier.verify(material.token())
    with pytest.raises(AuthUnavailable):
        await material.verifier.verify(material.token())
    assert material.provider.calls == 2
    material.clock.value += 30
    material.provider.error = None
    assert await material.verifier.verify(material.token())
    assert material.provider.calls == 3


@pytest.mark.parametrize("failure", [AuthUnavailable(), TimeoutError(), httpx.ConnectError("local fixture")])
async def test_no_trusted_key_and_provider_failure_is_503(security_material, failure):
    security_material.provider.error = failure
    with pytest.raises(AuthUnavailable):
        await security_material.verifier.verify(security_material.token())
    assert security_material.provider.calls == 1


@pytest.mark.parametrize("mutation", ["empty", "not-list", "too-many", "duplicate", "private",
                                     "bad-point", "wrong-curve", "wrong-alg", "wrong-use", "wrong-ops", "oversize"])
async def test_unusable_material_never_authenticates(security_material, mutation):
    material = security_material
    key = copy.deepcopy(material.jwk)
    documents = {
        "empty": {"keys": []}, "not-list": {"keys": {}},
        "too-many": {"keys": [{**key, "kid": str(i)} for i in range(33)]},
        "duplicate": {"keys": [key, key]}, "private": {"keys": [{**key, "d": "forbidden"}]},
        "bad-point": {"keys": [{**key, "x": "invalid"}]},
        "wrong-curve": {"keys": [{**key, "crv": "P-384"}]},
        "wrong-alg": {"keys": [{**key, "alg": "HS256"}]},
        "wrong-use": {"keys": [{**key, "use": "enc"}]},
        "wrong-ops": {"keys": [{**key, "key_ops": ["sign"]}]},
        "oversize": {"keys": [key], "extra": "x" * MAX_JWKS_BYTES},
    }
    material.provider.document = documents[mutation]
    with pytest.raises(AuthUnavailable):
        await material.verifier.verify(material.token())


@pytest.mark.parametrize("status,body", [(503, b"down"), (302, b""), (200, b"not-json"),
                                        (200, b"[]"), (200, b"x" * (MAX_JWKS_BYTES + 1))],
                         ids=["unavailable", "redirect", "malformed", "array", "oversized"])
async def test_http_adapter_rejects_errors_redirects_malformed_and_oversized(status, body):
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(status, content=body, headers={"location": "https://evil.test"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = HTTPJWKSProvider("https://identity.example.test/jwks", client)
        with pytest.raises(AuthUnavailable):
            await provider.fetch()
    assert len(requests) == 1


async def test_http_adapter_success_and_timeout(security_material):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"keys": [security_material.jwk]})
    )) as client:
        result = await HTTPJWKSProvider("https://identity.example.test/jwks", client).fetch()
        assert result["keys"][0]["kid"] == "local-key-1"

    def timed_out(request):
        raise httpx.ReadTimeout("controlled timeout")
    async with httpx.AsyncClient(transport=httpx.MockTransport(timed_out)) as client:
        with pytest.raises(AuthUnavailable):
            await HTTPJWKSProvider("https://identity.example.test/jwks", client).fetch()


async def test_excessively_nested_jwks_is_unavailable_not_500():
    body = b'{"keys":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}'
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=body)
    )) as client:
        cache = JWKSCache(HTTPJWKSProvider("https://identity.example.test/jwks", client))
        with pytest.raises(AuthUnavailable):
            await cache.key_for("local-key-1")
