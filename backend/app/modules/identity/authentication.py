"""ES256-only human JWT verification. No identity leaves this boundary unverified."""
import math
from typing import Protocol
from uuid import UUID

import jwt

from backend.app.core.config import Settings
from backend.app.modules.identity.errors import AuthUnavailable, SecurityError
from backend.app.modules.identity.jwks import JWKSCache
from backend.app.modules.identity.policy import CurrentUser


class TokenVerifier(Protocol):
    async def verify(self, token: str) -> CurrentUser: ...


def user_from_verified_claims(claims: dict) -> CurrentUser:
    """Only call after cryptographic AND temporal/issuer/audience verification."""
    try:
        if claims["role"] != "authenticated":
            raise ValueError()
        if "is_anonymous" in claims:
            if type(claims["is_anonymous"]) is not bool or claims["is_anonymous"]:
                raise ValueError()
        for name in ("exp", "iat", "nbf"):
            if name == "nbf" and name not in claims:
                continue
            value = claims[name]
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError()
        if claims["exp"] <= claims["iat"]:
            raise ValueError()
        if not isinstance(claims["sub"], str):
            raise ValueError()
        return CurrentUser(user_id=UUID(claims["sub"]))
    except (KeyError, TypeError, ValueError, OverflowError):
        raise SecurityError() from None


class SupabaseJWTVerifier:
    def __init__(self, settings: Settings, cache: JWKSCache) -> None:
        if not settings.supabase_jwt_issuer or not settings.supabase_jwt_audience:
            raise AuthUnavailable()
        if settings.supabase_jwt_allowed_algorithms != ["ES256"]:
            raise AuthUnavailable()
        self._issuer = settings.supabase_jwt_issuer
        self._audience = settings.supabase_jwt_audience
        self._leeway = settings.jwt_clock_skew_seconds
        self._cache = cache

    async def verify(self, token: str) -> CurrentUser:
        try:
            if not isinstance(token, str) or not 1 <= len(token) <= 16384:
                raise SecurityError()
            header = jwt.get_unverified_header(token)
            # Header is only an untrusted key selector, never configuration.
            if header.get("alg") != "ES256" or header.get("crit") or "b64" in header:
                raise SecurityError()
            kid = header.get("kid")
            if not isinstance(kid, str) or not 1 <= len(kid) <= 128:
                raise SecurityError()
            key = await self._cache.key_for(kid)
            claims = jwt.decode(
                token, key=key, algorithms=["ES256"],
                issuer=self._issuer, audience=self._audience, leeway=self._leeway,
                options={
                    "require": ["iss", "aud", "exp", "iat", "sub", "role"],
                    "verify_signature": True, "verify_exp": True, "verify_iat": True,
                    "verify_nbf": True, "verify_iss": True, "verify_aud": True,
                },
            )
            return user_from_verified_claims(claims)
        except (jwt.PyJWTError, ValueError, TypeError, OverflowError, RecursionError):
            raise SecurityError() from None
