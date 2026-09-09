from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from backend.app.core import database
from backend.app.core.config import Settings
from backend.app.main import create_app
from backend.app.modules.identity.dependencies import get_identity_repository
from backend.app.modules.identity.errors import Forbidden, IdentityUnavailable
from backend.app.modules.identity.policy import CurrentUser

pytestmark = pytest.mark.anyio


async def test_repository_dependency_preserves_exception_and_transaction_cleanup(monkeypatch):
    events = []
    session = AsyncMock()

    async def session_boundary():
        try:
            yield session
        except Forbidden:
            events.append("rollback")
            raise
        finally:
            events.append("close")

    monkeypatch.setattr(database, "SessionFactory", object())
    monkeypatch.setattr(database, "get_db_session", session_boundary)
    dependency = get_identity_repository(CurrentUser(uuid4()))
    await anext(dependency)
    with pytest.raises(Forbidden):
        await dependency.athrow(Forbidden())
    assert events == ["rollback", "close"]


async def test_unconfigured_database_is_503(monkeypatch):
    monkeypatch.setattr(database, "SessionFactory", None)
    with pytest.raises(IdentityUnavailable):
        await anext(get_identity_repository(CurrentUser(uuid4())))


async def test_lifespan_creates_process_local_cache_without_remote_fetch(security_material, monkeypatch):
    requests = AsyncMock(side_effect=AssertionError("No network request permitted"))
    monkeypatch.setattr(httpx.AsyncClient, "send", requests)
    app = create_app(security_material.settings)
    async with app.router.lifespan_context(app):
        cache = app.state.token_verifier._cache
        client = cache._provider._client
        assert cache._keys == {} and cache._ttl == 300
        assert not client.is_closed
        assert not client.follow_redirects
    assert client.is_closed and app.state.token_verifier is None
    requests.assert_not_awaited()


async def test_approved_environment_names_are_loaded(monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_ISSUER", "https://identity.example.test/auth/v1")
    monkeypatch.setenv("SUPABASE_JWT_AUDIENCE", "authenticated")
    monkeypatch.setenv("SUPABASE_JWKS_URL", "https://identity.example.test/jwks")
    monkeypatch.setenv("SUPABASE_JWT_ALLOWED_ALGORITHMS", '["ES256"]')
    monkeypatch.setenv("JWT_CLOCK_SKEW_SECONDS", "60")
    monkeypatch.setenv("SUPABASE_JWKS_CACHE_TTL_SECONDS", "300")
    settings = Settings(_env_file=None)
    assert settings.supabase_jwt_allowed_algorithms == ["ES256"]
    assert settings.jwt_clock_skew_seconds == 60
    assert settings.supabase_jwks_cache_ttl_seconds == 300
    assert settings.supabase_jwt_issuer == "https://identity.example.test/auth/v1"
    assert settings.supabase_jwt_audience == "authenticated"
    assert settings.supabase_jwks_url == "https://identity.example.test/jwks"
