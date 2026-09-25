"""Fully local Meta OAuth/Page discovery adapter tests."""

import json
from urllib.parse import parse_qs, urlsplit

import httpx
from pydantic import SecretStr
import pytest

from backend.app.modules.integrations.errors import (
    FacebookInvalidConnection,
    FacebookNotConfigured,
    FacebookProviderUnavailable,
)
from backend.app.modules.integrations.facebook_provider import MetaGraphOAuthAdapter
from backend.app.modules.integrations.scopes import FACEBOOK_OAUTH_SCOPES


pytestmark = pytest.mark.anyio
APP_SECRET = "synthetic-app-secret"
USER_TOKEN = "synthetic-user-token"
PAGE_TOKEN = "synthetic-page-token"


def adapter(handler):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False,
    )
    return MetaGraphOAuthAdapter(
        graph_version="v24.0", app_id="123456",
        app_secret=SecretStr(APP_SECRET),
        redirect_uri="https://app.example.test/oauth/callback", client=client,
    ), client


async def test_authorization_url_is_fixed_scoped_and_contains_no_app_secret():
    provider, client = adapter(lambda _request: httpx.Response(500))
    try:
        url = provider.authorization_url("signed-state")
    finally:
        await client.aclose()
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https" and parsed.hostname == "www.facebook.com"
    assert parsed.path == "/v24.0/dialog/oauth"
    assert query["scope"] == [",".join(FACEBOOK_OAUTH_SCOPES)]
    assert query["state"] == ["signed-state"]
    assert APP_SECRET not in url and APP_SECRET not in repr(provider)


@pytest.mark.parametrize("field,value", [
    ("app_id", "not-a-decimal-id"),
    ("redirect_uri", "http://app.example.test/oauth/callback"),
    ("redirect_uri", "https://user:pass@app.example.test/oauth/callback"),
])
async def test_direct_adapter_configuration_fails_closed(field, value):
    values = {
        "graph_version": "v24.0", "app_id": "123456",
        "app_secret": SecretStr(APP_SECRET),
        "redirect_uri": "https://app.example.test/oauth/callback",
    }
    values[field] = value
    with pytest.raises(FacebookNotConfigured):
        MetaGraphOAuthAdapter(**values)


async def test_exchange_uses_post_body_then_bearer_and_returns_normalized_page():
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/oauth/access_token"):
            return httpx.Response(200, json={
                "access_token": USER_TOKEN, "token_type": "bearer",
            })
        assert request.headers["authorization"] == f"Bearer {USER_TOKEN}"
        return httpx.Response(200, json={"data": [{
            "id": "123", "name": "Synthetic Page", "access_token": PAGE_TOKEN,
            "tasks": ["CREATE_CONTENT", "MODERATE"],
        }]})
    provider, client = adapter(handler)
    try:
        page = await provider.exchange_page(
            authorization_code=SecretStr("synthetic-code"), requested_page_id="123",
        )
    finally:
        await client.aclose()
    assert [request.method for request in requests] == ["POST", "GET"]
    assert APP_SECRET not in str(requests[0].url)
    assert PAGE_TOKEN not in repr(page) and USER_TOKEN not in repr(page)
    assert page.access_token.get_secret_value() == PAGE_TOKEN
    assert page.external_account_id == "123"
    assert page.capabilities.can_publish_posts is True


@pytest.mark.parametrize("pages", [
    [],
    [{"id":"456","name":"Wrong","access_token":"token","tasks":["CREATE_CONTENT"]}],
    [{"id":"123","name":"No capability","access_token":"token","tasks":[]}],
])
async def test_page_missing_mismatch_or_capability_failure_is_safe(pages):
    def handler(request):
        if request.url.path.endswith("/oauth/access_token"):
            return httpx.Response(200, json={"access_token": USER_TOKEN})
        return httpx.Response(200, json={"data": pages})
    provider, client = adapter(handler)
    try:
        with pytest.raises(FacebookInvalidConnection) as error:
            await provider.exchange_page(
                authorization_code=SecretStr("synthetic-code"),
                requested_page_id="123",
            )
    finally:
        await client.aclose()
    assert PAGE_TOKEN not in repr(error.value)


@pytest.mark.parametrize("response", [
    httpx.Response(400, json={"error":{"message":"raw-sensitive-provider-text"}}),
    httpx.Response(200, content=b"not-json"),
])
async def test_provider_failures_are_sanitized_and_not_retried(response):
    calls = 0
    def handler(_request):
        nonlocal calls
        calls += 1
        return response
    provider, client = adapter(handler)
    try:
        with pytest.raises(FacebookProviderUnavailable) as error:
            await provider.exchange_page(
                authorization_code=SecretStr("synthetic-code"),
                requested_page_id="123",
            )
    finally:
        await client.aclose()
    assert calls == 1
    assert "raw-sensitive-provider-text" not in repr(error.value)
    assert APP_SECRET not in repr(error.value)
