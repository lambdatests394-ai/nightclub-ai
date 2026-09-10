"""Local-only security fixtures. No remote provider, JWT or private key is stored."""
import copy
import json
from datetime import UTC, datetime
from uuid import UUID

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from backend.app.core.config import Settings
from backend.app.modules.identity.authentication import SupabaseJWTVerifier
from backend.app.modules.identity.jwks import JWKSCache


def pytest_addoption(parser):
    parser.addoption("--prompt5-postgres", action="store_true", default=False,
                     help="Run RLS tests only on nightclub_ai_prompt5_test with a disposable runtime login")
    parser.addoption("--local-postgres", action="store_true", default=False,
                     help="Run opted-in tests only on nightclub_ai_prompt4_test at loopback")


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Clock:
    value = 1000.0

    def __call__(self):
        return self.value


class StaticProvider:
    def __init__(self, document):
        self.document = document
        self.calls = 0
        self.error = None

    async def fetch(self):
        self.calls += 1
        if self.error:
            raise self.error
        return copy.deepcopy(self.document)


class SecurityMaterial:
    now = 1_800_000_000
    user_id = UUID("10000000-0000-0000-0000-000000000001")

    def __init__(self):
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.jwk = jwt.algorithms.ECAlgorithm.to_jwk(self.private_key.public_key(), as_dict=True)
        self.jwk.update(kid="local-key-1", alg="ES256", use="sig")
        self.provider = StaticProvider({"keys": [self.jwk]})
        self.clock = Clock()
        self.settings = Settings(
            _env_file=None, supabase_jwt_issuer="https://identity.example.test/auth/v1",
            supabase_jwt_audience="authenticated",
            supabase_jwks_url="https://identity.example.test/auth/v1/.well-known/jwks.json",
        )
        self.cache = JWKSCache(self.provider, clock=self.clock)
        self.verifier = SupabaseJWTVerifier(self.settings, self.cache)

    def claims(self):
        return {"iss": self.settings.supabase_jwt_issuer, "aud": "authenticated",
                "exp": self.now + 600, "iat": self.now - 300,
                "sub": str(self.user_id), "role": "authenticated"}

    def token(self, *, changes=None, omit=(), headers=None, key=None):
        claims = self.claims()
        claims.update(changes or {})
        for name in omit:
            claims.pop(name, None)
        # Sign arbitrary malformed claim fixtures without encode-time claim validation.
        return jwt.api_jws.encode(json.dumps(claims).encode(), key or self.private_key,
                                  algorithm="ES256", headers=headers or {"kid": "local-key-1"})


@pytest.fixture
def security_material(monkeypatch):
    material = SecurityMaterial()

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(material.now, UTC)

    # PyJWT has no injectable wall-clock API. Only its test clock is replaced;
    # JWKS transport/cache are injected directly and never monkeypatched globally.
    monkeypatch.setattr(jwt.api_jwt, "datetime", FixedDatetime)
    return material
