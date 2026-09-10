"""Pure/local HTTP tests. No provider or PostgreSQL connections."""
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql
from sqlalchemy import insert

from backend.app.core import database
from backend.app.main import create_app
from backend.app.modules.audit.models import AuditLog
from backend.app.modules.audit.writer import CampaignAuditWriter
from backend.app.modules.campaigns import dependencies
from backend.app.modules.campaigns.dependencies import get_campaign_service
from backend.app.modules.campaigns.errors import CampaignConflict, CampaignNotFound, IdempotencyConflict, InvalidCampaignRequest
from backend.app.modules.campaigns.models import Campaign, CampaignStatus
from backend.app.modules.campaigns.schemas import CampaignCreate, CampaignPatch
from backend.app.modules.campaigns.service import CampaignService, canonical_payload
from backend.app.modules.identity import dependencies as identity_dependencies
from backend.app.modules.identity.dependencies import get_current_user
from backend.app.modules.identity.errors import Forbidden
from backend.app.modules.identity.policy import CurrentUser, MemberRole, OrganizationContext, Permission, require_permission
from backend.app.shared.idempotency import IdempotencyStore, fingerprint
from backend.app.shared.models import IdempotencyKey

pytestmark = pytest.mark.anyio


class Repository:
    def __init__(self):
        self.rows = {}
        self.locks = []

    async def get(self, key, *, lock=False):
        self.locks.append(lock)
        return self.rows.get(key)

    async def save(self, row):
        row.created_at = row.created_at or datetime.now(UTC)
        self.rows[row.id] = row

    async def page(self, after, limit):
        return [self.rows[k] for k in sorted(self.rows) if after is None or k > after][:limit]


class MemoryKeys:
    """Test double ONLY. Production uses PostgreSQL INSERT/uniqueness."""
    def __init__(self):
        self.rows = {}

    async def claim(self, key, op, digest):
        if key in self.rows:
            row = self.rows[key]
            if row[:2] != (op, digest):
                raise IdempotencyConflict()
            return row[2]
        self.rows[key] = (op, digest, None)

    async def complete(self, key, status, body):
        self.rows[key] = (*self.rows[key][:2], (status, body))


@pytest.fixture
def delivery():
    context = OrganizationContext(uuid4(), uuid4(), MemberRole.OWNER)
    repo, keys, audit = Repository(), MemoryKeys(), AsyncMock()
    return CampaignService(context, repo, keys, audit)


@pytest.mark.parametrize("role", list(MemberRole))
@pytest.mark.parametrize("permission,allowed", [
    (Permission.CAMPAIGN_READ, set(MemberRole)),
    (Permission.CAMPAIGN_WRITE, {MemberRole.OWNER, MemberRole.MANAGER, MemberRole.EDITOR}),
    (Permission.CAMPAIGN_ARCHIVE, {MemberRole.OWNER, MemberRole.MANAGER}),
])
def test_campaign_role_matrix(role, permission, allowed):
    context = OrganizationContext(uuid4(), uuid4(), role)
    if role in allowed:
        require_permission(context, permission)
    else:
        with pytest.raises(Forbidden):
            require_permission(context, permission)


@pytest.mark.parametrize("field", ["status", "organizationId", "organization_id", "createdBy", "created_by", "id"])
@pytest.mark.parametrize("schema", [CampaignCreate, CampaignPatch])
def test_spoofed_fields_rejected(field, schema):
    with pytest.raises(ValidationError):
        schema.model_validate({"name": "Local fixture", field: str(uuid4())})


def test_patch_omitted_null_and_aware_dates():
    assert CampaignPatch().model_dump(exclude_unset=True) == {}
    assert CampaignPatch(objective=None).model_dump(exclude_unset=True) == {"objective": None}
    for payload in ({"name": None}, {"brief": None}, {"startsAt": "2026-01-01T00:00:00"}):
        with pytest.raises(ValidationError):
            CampaignPatch.model_validate(payload)


async def test_lifecycle_replay_archive_noop_and_locks(delivery):
    key = uuid4()
    payload = CampaignCreate(name="Sensitive free text", brief={"private": "not audit"}).model_dump()
    first = await delivery.mutate("create", key, payload)
    assert first[0] == 201 and first[1]["status"] == "draft"
    assert await delivery.mutate("create", key, payload) == first
    assert delivery.audit.write.await_count == 1
    campaign_id = next(iter(delivery.repository.rows))
    await delivery.mutate("patch", uuid4(), {"objective": None}, campaign_id)
    await delivery.mutate("archive", uuid4(), {}, campaign_id)
    calls = delivery.audit.write.await_count
    status, body = await delivery.mutate("archive", uuid4(), {}, campaign_id)
    assert status == 200 and body["status"] == "archived"
    assert delivery.audit.write.await_count == calls
    with pytest.raises(CampaignConflict):
        await delivery.mutate("patch", uuid4(), {"name": "changed"}, campaign_id)
    assert all(delivery.repository.locks)


async def test_resulting_date_range_and_partial_patch(delivery):
    starts = datetime.now(UTC)
    await delivery.mutate("create", uuid4(), CampaignCreate(name="x", starts_at=starts,
                         ends_at=starts + timedelta(days=2)).model_dump())
    key = next(iter(delivery.repository.rows))
    with pytest.raises(InvalidCampaignRequest):
        await delivery.mutate("patch", uuid4(), {"ends_at": starts - timedelta(days=1)}, key)
    await delivery.mutate("patch", uuid4(), {"starts_at": None}, key)
    assert delivery.repository.rows[key].ends_at == starts + timedelta(days=2)
    assert delivery.repository.rows[key].name == "x"


async def test_authorization_precedes_replay_and_mismatch(delivery):
    key, payload = uuid4(), CampaignCreate(name="x").model_dump()
    await delivery.mutate("create", key, payload)
    with pytest.raises(IdempotencyConflict):
        await delivery.mutate("create", key, {**payload, "name": "different"})
    delivery.context = OrganizationContext(delivery.context.organization_id, delivery.context.user_id, MemberRole.VIEWER)
    with pytest.raises(Forbidden):
        await delivery.mutate("create", key, payload)


async def test_pagination_and_not_found(delivery):
    for _ in range(3):
        await delivery.mutate("create", uuid4(), CampaignCreate(name="x").model_dump())
    first, cursor = await delivery.page(None, 2)
    last, tail = await delivery.page(cursor, 2)
    assert len(first) == 2 and len(last) == 1 and tail is None
    assert {r["id"] for r in first}.isdisjoint({r["id"] for r in last})
    with pytest.raises(CampaignNotFound):
        await delivery.read(uuid4())


@pytest.mark.parametrize("headers", [[], [("X-Organization-Id", "bad")],
    [("X-Organization-Id", str(uuid4())), ("X-Organization-Id", str(uuid4()))]])
async def test_http_invalid_organization_headers_without_db(headers):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(uuid4())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        response = await client.get("/api/v1/campaigns", headers=headers)
    assert response.status_code == 403 and response.json()["code"] == "ACCESS_DENIED"


@pytest.mark.parametrize("reason", ["inactive", "foreign", "removed"])
async def test_http_invalid_membership_before_campaign_access(monkeypatch, reason):
    @asynccontextmanager
    async def protected(user):
        yield AsyncMock()
    monkeypatch.setattr(dependencies, "protected_session", protected)
    monkeypatch.setattr(dependencies.IdentityService, "organization", AsyncMock(side_effect=Forbidden()))
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(uuid4())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        response = await client.get("/api/v1/campaigns", headers={"X-Organization-Id": str(uuid4())})
    assert response.status_code == 403


@pytest.mark.parametrize("key", [None, "invalid"])
@pytest.mark.parametrize("method,path", [("POST", ""), ("PATCH", "/{id}"), ("POST", "/{id}/archive")])
async def test_http_idempotency_required(delivery, key, method, path):
    app = create_app()
    app.dependency_overrides[get_campaign_service] = lambda: delivery
    headers = {} if key is None else {"Idempotency-Key": key}
    payload = {} if path.endswith("archive") else {"json": {"name": "x"}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        response = await client.request(method, "/api/v1/campaigns" + path.format(id=uuid4()), headers=headers, **payload)
    assert response.status_code == 422


async def test_http_replay_keeps_new_correlation(delivery):
    app = create_app()
    app.dependency_overrides[get_campaign_service] = lambda: delivery
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        headers = {"Idempotency-Key": str(uuid4())}
        a = await client.post("/api/v1/campaigns", json={"name": "x"}, headers=headers)
        b = await client.post("/api/v1/campaigns", json={"name": "x"}, headers=headers)
    assert a.status_code == b.status_code == 201
    assert a.json()["data"] == b.json()["data"]
    assert a.json()["meta"]["correlationId"] != b.json()["meta"]["correlationId"]


@pytest.mark.parametrize("fails", [False, True])
async def test_real_function_dependency_commit_before_response(monkeypatch, delivery, fails):
    """Real FastAPI route + production root cleanup; fake session commit I/O only."""
    events, session = [], AsyncMock()
    session.info = {}
    async def commit():
        events.append("commit")
        if fails:
            raise ConnectionError("synthetic failure must not leak")
    session.commit.side_effect = commit
    monkeypatch.setattr(database, "SessionFactory", lambda: session)
    monkeypatch.setattr(identity_dependencies, "verify_runtime_role", AsyncMock())
    monkeypatch.setattr(identity_dependencies, "establish_user_context", AsyncMock())
    monkeypatch.setattr(dependencies.IdentityService, "organization", AsyncMock(return_value=(delivery.context, object())))
    monkeypatch.setattr(dependencies, "CampaignService", lambda *args: delivery)
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(delivery.context.user_id)
    original = app
    async def observed(scope, receive, send):
        async def record(message):
            if message["type"] == "http.response.start":
                events.append(message["status"])
            await send(message)
        await original(scope, receive, record)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=observed), base_url="http://local.test") as client:
        response = await client.post("/api/v1/campaigns", json={"name": "x"}, headers={
            "X-Organization-Id": str(delivery.context.organization_id), "Idempotency-Key": str(uuid4())})
    expected = 503 if fails else 201
    assert response.status_code == expected and events == ["commit", expected]
    session.rollback.assert_awaited_once()
    session.close.assert_awaited_once()
    if fails:
        assert response.headers["Retry-After"] == "30"
        assert "synthetic" not in response.text


@pytest.mark.parametrize("hidden", [True, False])
async def test_postgresql_claim_hidden_collision_or_durable_replay(hidden):
    session = AsyncMock()
    context = OrganizationContext(uuid4(), uuid4(), MemberRole.OWNER)
    row = None if hidden else SimpleNamespace(operation="op", request_hash="digest", state="completed", response_status=201, response_body={"id": "safe"})
    session.scalar.side_effect = ["read committed", None, row]
    store = IdempotencyStore(session, context)
    if hidden:
        with pytest.raises(IdempotencyConflict):
            await store.claim(uuid4(), "op", "digest")
    else:
        assert await store.claim(uuid4(), "op", "digest") == (201, {"id": "safe"})
    sql = str(session.scalar.call_args_list[1].args[0].compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (key) DO NOTHING" in sql


async def test_audit_redaction_and_no_returning(delivery):
    session = AsyncMock()
    row = Campaign(id=uuid4(), name="NEVER COPY", objective="NEVER COPY", brief={"secret": "NEVER COPY"}, status=CampaignStatus.DRAFT)
    await CampaignAuditWriter(session, delivery.context, uuid4()).write("campaign.created", row, ["name", "brief"])
    statement = session.execute.call_args.args[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    assert "RETURNING" not in str(compiled)
    assert "NEVER COPY" not in str(compiled.params)
    assert compiled.params["after"]["changedFields"] == ["brief", "name"]


def test_mapping_alignment_and_fingerprint():
    columns = Campaign.__table__.c
    assert columns.status.type.enums == ["draft", "active", "paused", "completed", "archived"]
    assert columns.status.type.create_type is False
    assert columns.starts_at.type.timezone and columns.ends_at.type.timezone
    assert IdempotencyKey.__table__.c.expires_at.type.timezone
    assert AuditLog.__table__.c.id.identity.always
    assert "RETURNING" not in str(insert(AuditLog.__table__).inline().compile(dialect=postgresql.dialect()))
    assert fingerprint("op", {"b": 1, "a": 2}) == fingerprint("op", {"a": 2, "b": 1})
    assert fingerprint("op", {}) != fingerprint("other", {})
    a = datetime.fromisoformat("2026-01-01T00:00:00+00:00")
    b = datetime.fromisoformat("2025-12-31T18:00:00-06:00")
    assert canonical_payload(a) == canonical_payload(b)


@pytest.mark.parametrize("role", list(MemberRole))
@pytest.mark.parametrize("operation", ["read", "write", "archive"])
async def test_http_six_role_matrix(delivery, role, operation):
    _, created = await delivery.mutate("create", uuid4(), CampaignCreate(name="x").model_dump())
    delivery.context = OrganizationContext(delivery.context.organization_id, delivery.context.user_id, role)
    app = create_app()
    app.dependency_overrides[get_campaign_service] = lambda: delivery
    headers = {"Idempotency-Key": str(uuid4())}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        if operation == "read":
            response = await client.get("/api/v1/campaigns", headers=headers)
            expected = 200
        elif operation == "write":
            response = await client.post("/api/v1/campaigns", json={"name": "x"}, headers=headers)
            expected = 201 if role in {MemberRole.OWNER, MemberRole.MANAGER, MemberRole.EDITOR} else 403
        else:
            response = await client.post(f"/api/v1/campaigns/{created['id']}/archive", headers=headers)
            expected = 200 if role in {MemberRole.OWNER, MemberRole.MANAGER} else 403
    assert response.status_code == expected


async def test_invalid_cursor_http_and_hidden_campaign(delivery):
    app = create_app()
    app.dependency_overrides[get_campaign_service] = lambda: delivery
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        assert (await client.get("/api/v1/campaigns?cursor=bad")).status_code == 422
        response = await client.get(f"/api/v1/campaigns/{uuid4()}")
    assert response.status_code == 404
    assert response.headers["Content-Type"].startswith("application/problem+json")


async def test_no_expired_key_reuse_and_24h_claim(delivery):
    session = AsyncMock()
    row = SimpleNamespace(operation="op", request_hash="digest", state="completed", response_status=200,
                          response_body={"id": "safe"}, expires_at=datetime.now(UTC) - timedelta(days=10))
    session.scalar.side_effect = ["read committed", None, row]
    assert await IdempotencyStore(session, delivery.context).claim(uuid4(), "op", "digest") == (200, {"id": "safe"})
    statement = session.scalar.call_args_list[1].args[0]
    values = statement.compile(dialect=postgresql.dialect()).params
    assert values["expires_at"] - values["created_at"] == timedelta(hours=24)


async def test_idempotency_requires_read_committed(delivery):
    from backend.app.modules.identity.errors import IdentityUnavailable
    session = AsyncMock()
    session.scalar.return_value = "repeatable read"
    with pytest.raises(IdentityUnavailable):
        await IdempotencyStore(session, delivery.context).claim(uuid4(), "op", "digest")
    assert session.scalar.await_count == 1
