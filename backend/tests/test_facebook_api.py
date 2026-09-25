"""Public Facebook connection API and OAuth transaction-boundary tests."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from backend.app.main import create_app
from backend.app.modules.integrations.dependencies import (
    FacebookConnectionCoordinator,
    get_facebook_connection_coordinator,
)


pytestmark = pytest.mark.anyio


async def call(app, method, path, *, headers=None, json=None):
    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://local.test") as client:
        return await client.request(method, path, headers=headers, json=json)


@pytest.fixture
def api():
    connection = {
        "id": str(uuid4()), "platform": "facebook",
        "externalAccountId": "123", "displayName": "Synthetic Page",
        "capabilities": {"canPublishPosts": True}, "status": "active",
    }
    coordinator = SimpleNamespace(
        list_connections=AsyncMock(return_value=[connection]),
        start=AsyncMock(return_value=(201, {
            "authorizationUrl": "https://www.facebook.com/v24.0/dialog/oauth?state=synthetic",
            "requestedPageId": "123", "scopes": ["pages_manage_posts"],
            "expiresAt": "2030-01-01T00:10:00+00:00",
        })),
        callback=AsyncMock(return_value=connection),
    )
    app = create_app()
    app.dependency_overrides[get_facebook_connection_coordinator] = lambda: coordinator
    return app, coordinator


async def test_approved_connection_routes_are_registered_and_internal_executor_route_absent(api):
    app, _ = api
    routes = {(route.path, method) for route in app.routes for method in route.methods}
    assert ("/api/v1/connections", "GET") in routes
    assert ("/api/v1/connections/facebook/oauth/start", "POST") in routes
    assert ("/api/v1/connections/facebook/oauth/callback", "GET") in routes
    assert ("/internal/automation/publish-due", "POST") not in routes


async def test_connection_list_is_redacted(api):
    app, coordinator = api
    response = await call(app, "GET", "/api/v1/connections")
    assert response.status_code == 200
    serialized = response.text.lower()
    assert not ({"access_token", "credentials_ciphertext", "credential_key_version"}
                & set(response.json()["data"][0]))
    assert "secret" not in serialized and "ciphertext" not in serialized
    coordinator.list_connections.assert_awaited_once()


async def test_oauth_start_requires_uuid_key_and_returns_only_browser_safe_data(api):
    app, coordinator = api
    key = uuid4()
    response = await call(
        app, "POST", "/api/v1/connections/facebook/oauth/start",
        headers={"Idempotency-Key": str(key)}, json={"requestedPageId": "123"},
    )
    assert response.status_code == 201
    assert set(response.json()["data"]) == {
        "authorizationUrl", "requestedPageId", "scopes", "expiresAt",
    }
    coordinator.start.assert_awaited_once_with(key, "123")


@pytest.mark.parametrize("headers", [
    {},
    {"Idempotency-Key": "not-a-uuid"},
    [("Idempotency-Key", str(uuid4())), ("Idempotency-Key", str(uuid4()))],
])
async def test_oauth_start_rejects_missing_malformed_or_duplicate_key(api, headers):
    app, coordinator = api
    response = await call(
        app, "POST", "/api/v1/connections/facebook/oauth/start",
        headers=headers, json={"requestedPageId": "123"},
    )
    assert response.status_code == 422
    coordinator.start.assert_not_awaited()


async def test_oauth_callback_requires_single_bounded_state_and_code(api):
    app, coordinator = api
    response = await call(
        app, "GET", "/api/v1/connections/facebook/oauth/callback?state=safe-state&code=safe-code",
    )
    assert response.status_code == 200
    coordinator.callback.assert_awaited_once_with("safe-state", "safe-code")
    for query in (
        "state=&code=safe", "state=safe&code=", "state=a&state=b&code=safe",
        "state=safe&code=a&code=b",
    ):
        response = await call(
            app, "GET", f"/api/v1/connections/facebook/oauth/callback?{query}",
        )
        assert response.status_code in {400, 422}


async def test_callback_commits_state_before_provider_and_persists_in_new_transaction():
    events = []
    coordinator = object.__new__(FacebookConnectionCoordinator)

    @asynccontextmanager
    async def context():
        events.append("transaction-enter")
        yield object(), object()
        events.append("transaction-commit")

    state = SimpleNamespace(requested_page_id="123")
    page = SimpleNamespace()
    service = SimpleNamespace(
        consume_facebook_oauth_state=AsyncMock(return_value=state),
        persist_verified_facebook_connection=AsyncMock(return_value={"id": "connection"}),
    )

    class Provider:
        async def exchange_page(self, **values):
            events.append("provider-outside-transaction")
            return page

    coordinator._context = context
    coordinator._service = lambda _session, _context: service
    coordinator._provider = lambda: Provider()
    result = await coordinator.callback("state", "code")
    assert result == {"id": "connection"}
    assert events == [
        "transaction-enter", "transaction-commit",
        "provider-outside-transaction",
        "transaction-enter", "transaction-commit",
    ]
    service.persist_verified_facebook_connection.assert_awaited_once_with(
        page, expected_page_id="123",
    )
