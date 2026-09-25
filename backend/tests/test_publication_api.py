"""Prompt 10 B2-C ASGI contracts; publication service is dependency-injected."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from backend.app.main import create_app
from backend.app.core import database
from backend.app.modules.automation import dependencies
from backend.app.modules.automation.dependencies import get_publication_service
from backend.app.modules.content.errors import ContentConflict
from backend.app.modules.identity import dependencies as identity_dependencies
from backend.app.modules.identity.dependencies import get_current_user
from backend.app.modules.identity.policy import CurrentUser, MemberRole, OrganizationContext

pytestmark = pytest.mark.anyio


@pytest.fixture
def publication_api():
    service = SimpleNamespace(
        schedule=AsyncMock(return_value=(201, {"contentId": str(uuid4())})),
        cancel_schedule=AsyncMock(return_value=(200, {"contentId": str(uuid4())})),
        publish_now=AsyncMock(return_value=(201, {"contentId": str(uuid4())})),
    )
    app = create_app()
    app.dependency_overrides[get_publication_service] = lambda: service
    return app, service


async def call(app, method, path, *, headers=None, json=None, content=None):
    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        return await client.request(method, path, headers=headers, json=json, content=content)


async def test_schedule_route_contract(publication_api):
    app, service = publication_api
    content_id, key = uuid4(), uuid4()
    response = await call(
        app, "POST", f"/api/v1/content/{content_id}/schedule",
        headers={"Idempotency-Key": str(key)},
        json={"versionNo": 3, "scheduledFor": "2026-09-25T12:30:00-06:00"},
    )
    assert response.status_code == 201
    request = service.schedule.await_args.args[2]
    assert request.version_no == 3
    assert request.scheduled_for == datetime(2026, 9, 25, 18, 30, tzinfo=UTC)
    assert service.schedule.await_args.args[:2] == (content_id, key)
    assert set(response.json()) == {"data", "meta"}


@pytest.mark.parametrize("suffix,method,status", [
    ("cancel-schedule", "cancel_schedule", 200),
    ("publish-now", "publish_now", 201),
])
async def test_empty_body_routes(publication_api, suffix, method, status):
    app, service = publication_api
    content_id, key = uuid4(), uuid4()
    response = await call(
        app, "POST", f"/api/v1/content/{content_id}/{suffix}",
        headers={"Idempotency-Key": str(key)}, content=b"",
    )
    assert response.status_code == status
    getattr(service, method).assert_awaited_once_with(content_id, key)


@pytest.mark.parametrize("suffix", ["cancel-schedule", "publish-now"])
async def test_body_forbidden_on_empty_body_routes(publication_api, suffix):
    app, service = publication_api
    response = await call(
        app, "POST", f"/api/v1/content/{uuid4()}/{suffix}",
        headers={"Idempotency-Key": str(uuid4())}, json={},
    )
    assert response.status_code == 422 and response.json()["code"] == "INVALID_REQUEST"
    service.cancel_schedule.assert_not_awaited()
    service.publish_now.assert_not_awaited()


@pytest.mark.parametrize("suffix,payload", [
    ("schedule", {"versionNo": 1, "scheduledFor": "2026-09-25T12:00:00Z"}),
    ("cancel-schedule", None), ("publish-now", None),
])
@pytest.mark.parametrize("header", [None, "not-a-uuid"])
async def test_idempotency_key_required(publication_api, suffix, payload, header):
    app, _ = publication_api
    headers = {} if header is None else {"Idempotency-Key": header}
    response = await call(
        app, "POST", f"/api/v1/content/{uuid4()}/{suffix}",
        headers=headers, json=payload,
    )
    assert response.status_code == 422


async def test_duplicate_idempotency_header_rejected(publication_api):
    app, _ = publication_api
    response = await call(
        app, "POST", f"/api/v1/content/{uuid4()}/publish-now",
        headers=[("Idempotency-Key", str(uuid4())), ("Idempotency-Key", str(uuid4()))],
        content=b"",
    )
    assert response.status_code == 422


@pytest.mark.parametrize("payload", [
    {"versionNo": 0, "scheduledFor": "2026-09-25T12:00:00Z"},
    {"versionNo": True, "scheduledFor": "2026-09-25T12:00:00Z"},
    {"versionNo": 1, "scheduledFor": "2026-09-25T12:00:00"},
    {"versionNo": 1, "scheduledFor": "2026-09-25T12:00:00Z", "extra": 1},
])
async def test_schedule_schema_rejected_at_http_boundary(publication_api, payload):
    app, service = publication_api
    response = await call(
        app, "POST", f"/api/v1/content/{uuid4()}/schedule",
        headers={"Idempotency-Key": str(uuid4())}, json=payload,
    )
    assert response.status_code == 422
    service.schedule.assert_not_awaited()


async def test_sanitized_conflict_response(publication_api):
    app, service = publication_api
    service.publish_now.side_effect = ContentConflict()
    response = await call(
        app, "POST", f"/api/v1/content/{uuid4()}/publish-now",
        headers={"Idempotency-Key": str(uuid4())}, content=b"",
    )
    assert response.status_code == 409
    assert response.json()["code"] == "CONTENT_CONFLICT"
    assert "constraint" not in response.text.lower()


@pytest.mark.parametrize("headers", [
    {},
    {"X-Organization-Id": "invalid"},
    [("X-Organization-Id", str(uuid4())), ("X-Organization-Id", str(uuid4()))],
])
async def test_publication_dependency_requires_exactly_one_organization_header(headers):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(uuid4())
    request_headers = list(headers.items()) if isinstance(headers, dict) else headers
    request_headers.append(("Idempotency-Key", str(uuid4())))
    response = await call(
        app, "POST", f"/api/v1/content/{uuid4()}/publish-now",
        headers=request_headers, content=b"",
    )
    assert response.status_code == 403 and response.json()["code"] == "ACCESS_DENIED"


@pytest.mark.parametrize("fails", [False, True])
async def test_publication_commit_finishes_before_http_success(monkeypatch, fails):
    context = OrganizationContext(uuid4(), uuid4(), MemberRole.OWNER)
    result = {"contentId": str(uuid4())}
    service = SimpleNamespace(publish_now=AsyncMock(return_value=(201, result)))
    events = []
    session = AsyncMock()
    session.info = {}

    async def commit():
        events.append("commit")
        if fails:
            raise ConnectionError("synthetic private commit failure")

    session.commit.side_effect = commit
    monkeypatch.setattr(database, "SessionFactory", lambda: session)
    monkeypatch.setattr(identity_dependencies, "verify_runtime_role", AsyncMock())
    monkeypatch.setattr(identity_dependencies, "establish_user_context", AsyncMock())
    monkeypatch.setattr(
        dependencies.IdentityService, "organization",
        AsyncMock(return_value=(context, object())),
    )
    monkeypatch.setattr(dependencies, "PublicationService", lambda *args: service)
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(context.user_id)

    async def observed(scope, receive, send):
        async def record(message):
            if message["type"] == "http.response.start":
                events.append(message["status"])
            await send(message)
        await app(scope, receive, record)

    response = await call(
        observed, "POST", f"/api/v1/content/{uuid4()}/publish-now",
        headers={
            "X-Organization-Id": str(context.organization_id),
            "Idempotency-Key": str(uuid4()),
        },
        content=b"",
    )
    expected = 503 if fails else 201
    assert response.status_code == expected and events == ["commit", expected]
    if fails:
        assert response.headers["Retry-After"] == "30"
        assert "synthetic" not in response.text
