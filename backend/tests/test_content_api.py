"""Prompt 7 unit/ASGI contracts; these tests never access a real database."""
import ast
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from backend.app.core import database
from backend.app.main import create_app
from backend.app.modules.content import dependencies
from backend.app.modules.content.audit import ContentAuditWriter
from backend.app.modules.content.dependencies import get_content_service
from backend.app.modules.content.errors import ContentConflict, ContentNotFound, ContentReferenceNotFound, InvalidContentRequest
from backend.app.modules.content.models import ContentItem, ContentVersion, ReviewDecision
from backend.app.modules.content.repository import ContentRepository
from backend.app.modules.content.schemas import ContentCreate, ContentPatch, ContentReviewRequest
from backend.app.modules.content.service import ContentService
from backend.app.modules.content.state_machine import ContentState
from backend.app.modules.identity import dependencies as identity_dependencies
from backend.app.modules.identity.dependencies import get_current_user
from backend.app.modules.identity.errors import Forbidden
from backend.app.modules.identity.policy import CurrentUser, MemberRole, OrganizationContext, Permission, require_permission
from backend.app.platform.enums import ContentStatus
from backend.app.shared.errors import IdempotencyConflict

pytestmark = pytest.mark.anyio


class MemoryRepository:
    def __init__(self, context):
        self.context = context
        self.items, self.versions, self.decisions = {}, {}, []
        self.campaigns, self.connections, self.locked = {}, {}, []

    async def create_item(self, *, content_id, actor_id, campaign_id, platform, connection_id):
        now = datetime.now(UTC)
        self.items[content_id] = ContentItem(id=content_id, organization_id=self.context.organization_id,
            campaign_id=campaign_id, platform=platform, connection_id=connection_id, status=ContentStatus.DRAFT,
            current_version_no=1, approved_version_no=None, created_by=actor_id, created_at=now, updated_at=now)

    async def add_content_version(self, version):
        assert (version.content_item_id, version.version_no) not in self.versions
        self.versions[version.content_item_id, version.version_no] = version

    async def add_review_decision(self, decision):
        self.decisions.append(decision)

    async def save_content_state(self, state):
        item = self.items[state.content_id]
        item.status, item.current_version_no, item.approved_version_no = state.status, state.current_version_no, state.approved_version_no

    async def get_item(self, content_id):
        self.locked.append(content_id)
        return self.items.get(content_id)

    async def current(self, content_id):
        item = self.items.get(content_id)
        return (item, self.versions[item.id, item.current_version_no]) if item else None

    async def campaign(self, campaign_id):
        return self.campaigns.get(campaign_id)

    async def connection(self, connection_id):
        return self.connections.get(connection_id)

    async def page(self, after, limit):
        return [await self.current(k) for k in sorted(self.items) if after is None or k > after][:limit]


class MemoryIdempotency:
    """Test double; production concurrency is covered exclusively by PG tests."""
    def __init__(self):
        self.rows = {}

    async def claim(self, key, operation, digest):
        if key in self.rows:
            old = self.rows[key]
            if old[:2] != (operation, digest):
                raise IdempotencyConflict()
            return old[2]
        self.rows[key] = (operation, digest, None)

    async def complete(self, key, status, data):
        self.rows[key] = (*self.rows[key][:2], (status, data))


@pytest.fixture
def service():
    context = OrganizationContext(uuid4(), uuid4(), MemberRole.OWNER)
    return ContentService(context, MemoryRepository(context), MemoryIdempotency(), AsyncMock())


async def draft(service, **values):
    response = await service.mutate("create", uuid4(), ContentCreate(platform="facebook", body="original", **values).model_dump())
    return UUID(response[1]["id"])


@pytest.mark.parametrize("role", list(MemberRole))
@pytest.mark.parametrize("operation,permission,allowed", [
    ("read", Permission.CONTENT_READ, set(MemberRole)),
    ("create", Permission.CONTENT_WRITE, {MemberRole.OWNER, MemberRole.MANAGER, MemberRole.EDITOR}),
    ("review", Permission.CONTENT_REVIEW, {MemberRole.OWNER, MemberRole.MANAGER, MemberRole.REVIEWER}),
])
async def test_all_roles_http_and_policy(service, role, operation, permission, allowed):
    content_id = await draft(service)
    service.repository.items[content_id].status = ContentStatus.IN_REVIEW
    service.repository.items[content_id].created_by = uuid4()
    service.context = OrganizationContext(service.context.organization_id, service.context.user_id, role)
    app = create_app()
    app.dependency_overrides[get_content_service] = lambda: service
    headers = {"Idempotency-Key": str(uuid4())}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        if operation == "read":
            response = await client.get("/api/v1/content", headers=headers)
        elif operation == "create":
            response = await client.post("/api/v1/content", headers=headers, json={"platform": "facebook", "body": "fixture"})
        else:
            response = await client.post(f"/api/v1/content/{content_id}/review", headers=headers, json={"versionNo": 1, "decision": "approved"})
    if role in allowed:
        require_permission(service.context, permission)
        assert response.status_code == (201 if operation == "create" else 200)
    else:
        with pytest.raises(Forbidden):
            require_permission(service.context, permission)
        assert response.status_code == 403


def test_deny_unknown_permission_and_role(service):
    with pytest.raises(Forbidden):
        require_permission(service.context, "content:publish")
    with pytest.raises(Forbidden):
        require_permission(OrganizationContext(uuid4(), uuid4(), "unknown"), Permission.CONTENT_READ)


@pytest.mark.parametrize("platform", ["facebook", "whatsapp"])
async def test_create_atomic_effects_and_safe_current_projection(service, platform):
    status, response = await service.mutate("create", uuid4(), ContentCreate(platform=platform, body="private", title="private", link_url="https://example.test/private").model_dump())
    item = next(iter(service.repository.items.values()))
    version = service.repository.versions[item.id, 1]
    assert status == 201 and response["platform"] == platform
    assert item.organization_id == service.context.organization_id and item.created_by == service.context.user_id
    assert item.current_version_no == 1 and item.status == "draft" and item.approved_version_no is None
    assert version.source == "manual" and version.ai_generation_id is None
    assert item.scheduled_for is None and item.published_at is None
    assert "credentialsCiphertext" not in response and "reviewDecisions" not in response


@pytest.mark.parametrize("field", ["id", "organizationId", "createdBy", "status", "currentVersionNo", "approvedVersionNo", "source", "aiGenerationId", "scheduledFor", "publishedAt", "externalPostId", "assetIds"])
def test_create_rejects_authoritative_fields(field):
    with pytest.raises(ValidationError):
        ContentCreate.model_validate({"platform": "facebook", "body": "x", field: "untrusted"})


@pytest.mark.parametrize("field", ["campaignId", "platform", "connectionId", "organizationId", "createdBy", "status", "currentVersionNo", "approvedVersionNo", "source", "aiGenerationId", "scheduledFor", "id"])
def test_patch_rejects_authoritative_fields(field):
    with pytest.raises(ValidationError):
        ContentPatch.model_validate({"body": "new", field: "untrusted"})


def test_platform_and_review_validation():
    with pytest.raises(ValidationError):
        ContentCreate(platform="instagram", body="x")
    for payload in ({}, {"body": None}):
        with pytest.raises(ValidationError):
            ContentPatch.model_validate(payload)
    for payload in ({"versionNo": True, "decision": "approved"}, {"versionNo": 1, "decision": "rejected"}):
        with pytest.raises(ValidationError):
            ContentReviewRequest.model_validate(payload)


@pytest.mark.parametrize("status", list(ContentStatus))
async def test_patch_immutable_version_all_states(service, status):
    content_id = await draft(service)
    item = service.repository.items[content_id]
    item.status, item.approved_version_no = status, 1 if status == ContentStatus.APPROVED else None
    old = service.repository.versions[content_id, 1]
    if status in {ContentStatus.DRAFT, ContentStatus.CHANGES_REQUESTED, ContentStatus.APPROVED}:
        result = await service.mutate("patch", uuid4(), {"title": "new"}, content_id)
        assert result[1]["currentVersionNo"] == 2 and result[1]["approvedVersionNo"] is None
        assert result[1]["status"] == "draft" and old.title is None and old.body == "original"
        assert service.repository.versions[content_id, 2].body == "original"
    else:
        with pytest.raises(ContentConflict):
            await service.mutate("patch", uuid4(), {"title": "new"}, content_id)
    assert service.repository.locked[-1] == content_id


@pytest.mark.parametrize("payload", [{}, {"body": "original"}, {"title": None}])
async def test_noop_patch_rejected(service, payload):
    content_id = await draft(service)
    with pytest.raises(InvalidContentRequest):
        await service.mutate("patch", uuid4(), payload, content_id)
    assert len(service.repository.versions) == 1


@pytest.mark.parametrize("status", list(ContentStatus))
async def test_submit_state_and_replay(service, status):
    content_id = await draft(service)
    service.repository.items[content_id].status = status
    key = uuid4()
    if status == ContentStatus.DRAFT:
        first = await service.mutate("submit-review", key, {}, content_id)
        assert first[1]["status"] == "in_review"
        assert await service.mutate("submit-review", key, {}, content_id) == first
        assert service.audit.write.await_count == 2
    else:
        with pytest.raises(ContentConflict):
            await service.mutate("submit-review", key, {}, content_id)


@pytest.mark.parametrize("decision", ["approved", "changes_requested"])
@pytest.mark.parametrize("role,comment,allowed", [(MemberRole.OWNER,None,False),(MemberRole.OWNER,"  ",False),(MemberRole.OWNER,"reason",True),(MemberRole.MANAGER,"reason",False),(MemberRole.REVIEWER,"reason",False)])
async def test_self_review_owner_reason_and_decision(service, decision, role, comment, allowed):
    content_id = await draft(service)
    await service.mutate("submit-review", uuid4(), {}, content_id)
    service.context = OrganizationContext(service.context.organization_id, service.context.user_id, role)
    key, payload = uuid4(), {"version_no": 1, "decision": decision, "comment": comment}
    if not allowed:
        with pytest.raises(ContentConflict):
            await service.mutate("review", key, payload, content_id)
        assert service.repository.decisions == []
    else:
        result = await service.mutate("review", key, payload, content_id)
        assert result[1]["status"] == decision
        assert result[1]["approvedVersionNo"] == (1 if decision == "approved" else None)
        assert service.audit.write.call_args.kwargs["owner_override"] is True
        assert await service.mutate("review", key, payload, content_id) == result
        assert len(service.repository.decisions) == 1


async def test_stale_review_and_noncreator_review(service):
    content_id = await draft(service)
    await service.mutate("submit-review", uuid4(), {}, content_id)
    service.repository.items[content_id].created_by = uuid4()
    with pytest.raises(ContentConflict):
        await service.mutate("review", uuid4(), {"version_no": 2, "decision": "approved", "comment": None}, content_id)
    await service.mutate("review", uuid4(), {"version_no": 1, "decision": "approved", "comment": None}, content_id)
    assert service.audit.write.call_args.kwargs["owner_override"] is False


@pytest.mark.parametrize("kind", ["absent", "archived"])
async def test_campaign_create_checks(service, kind):
    campaign_id = uuid4()
    if kind == "archived":
        service.repository.campaigns[campaign_id] = SimpleNamespace(status="archived")
    with pytest.raises(ContentReferenceNotFound if kind == "absent" else ContentConflict):
        await draft(service, campaign_id=campaign_id)


@pytest.mark.parametrize("action,payload", [("patch", {"body": "edited"}), ("submit-review", {}), ("review", {"version_no":1,"decision":"approved","comment":"reason"})])
async def test_archived_campaign_blocks_every_mutation(service, action, payload):
    campaign_id = uuid4()
    service.repository.campaigns[campaign_id] = SimpleNamespace(status="draft")
    content_id = await draft(service, campaign_id=campaign_id)
    service.repository.campaigns[campaign_id].status = "archived"
    assert (await service.read(content_id))["id"] == str(content_id)
    with pytest.raises(ContentConflict):
        await service.mutate(action, uuid4(), payload, content_id)


@pytest.mark.parametrize("kind", ["absent", "inactive", "mismatch"])
async def test_connection_checks(service, kind):
    connection_id = uuid4()
    if kind != "absent":
        service.repository.connections[connection_id] = SimpleNamespace(platform="whatsapp" if kind == "mismatch" else "facebook", status="inactive" if kind == "inactive" else "active")
    with pytest.raises(ContentReferenceNotFound if kind == "absent" else ContentConflict):
        await draft(service, connection_id=connection_id)


async def test_connection_projection_and_append_only_inserts():
    session = AsyncMock()
    session.execute.return_value = SimpleNamespace(one_or_none=lambda: None)
    repo = ContentRepository(session, uuid4())
    await repo.connection(uuid4())
    statement = session.execute.call_args.args[0]
    assert {c.name for c in statement.selected_columns} == {"id", "organization_id", "platform", "status"}
    assert "credentials_ciphertext" not in str(statement.compile(dialect=postgresql.dialect()))
    await repo.add_review_decision(ReviewDecision(id=uuid4(), content_item_id=uuid4(), content_version_id=uuid4(), decision="approved", comment="private", decided_by=uuid4(), decided_at=datetime.now(UTC)))
    assert "RETURNING" not in str(session.execute.call_args.args[0].compile(dialect=postgresql.dialect()))


async def test_audit_never_copies_content_or_comment(service):
    session = AsyncMock()
    state = ContentState(uuid4(), service.context.user_id, ContentStatus.APPROVED)
    await ContentAuditWriter(session, service.context, uuid4()).write("content.approved", state, changed_fields=["body", "comment"], owner_override=True)
    params = session.execute.call_args.args[0].compile(dialect=postgresql.dialect()).params
    assert params["after"]["ownerOverride"] is True
    assert set(params["after"]) == {"organizationId", "contentId", "currentVersionNo", "previousStatus", "status", "changedFields", "ownerOverride"}


@pytest.mark.parametrize("headers", [[], [("X-Organization-Id","bad")], [("X-Organization-Id",str(uuid4())),("X-Organization-Id",str(uuid4()))]])
async def test_bad_organization_headers_http(headers):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(uuid4())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        response = await client.get("/api/v1/content", headers=headers)
    assert response.status_code == 403 and response.json()["code"] == "ACCESS_DENIED"


@pytest.mark.parametrize("reason", ["inactive", "foreign", "removed"])
async def test_context_denied_before_replay(service, monkeypatch, reason):
    session = AsyncMock()
    session.info = {}
    monkeypatch.setattr(database, "SessionFactory", lambda: session)
    monkeypatch.setattr(identity_dependencies, "verify_runtime_role", AsyncMock())
    monkeypatch.setattr(identity_dependencies, "establish_user_context", AsyncMock())
    monkeypatch.setattr(dependencies.IdentityService, "organization", AsyncMock(side_effect=Forbidden()))
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(service.context.user_id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        response = await client.get("/api/v1/content", headers={"X-Organization-Id":str(service.context.organization_id)})
    assert response.status_code == 403
    session.commit.assert_not_awaited()


@pytest.mark.parametrize("fails", [False, True])
async def test_content_commit_before_response(service, monkeypatch, fails):
    events, session = [], AsyncMock()
    session.info = {}
    async def commit():
        events.append("commit")
        if fails:
            raise ConnectionError("synthetic private failure")
    session.commit.side_effect = commit
    monkeypatch.setattr(database, "SessionFactory", lambda: session)
    monkeypatch.setattr(identity_dependencies, "verify_runtime_role", AsyncMock())
    monkeypatch.setattr(identity_dependencies, "establish_user_context", AsyncMock())
    monkeypatch.setattr(dependencies.IdentityService, "organization", AsyncMock(return_value=(service.context, object())))
    monkeypatch.setattr(dependencies, "ContentService", lambda *args: service)
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(service.context.user_id)
    async def observed(scope, receive, send):
        async def record(message):
            if message["type"] == "http.response.start": events.append(message["status"])
            await send(message)
        await app(scope, receive, record)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=observed), base_url="http://local.test") as client:
        response = await client.post("/api/v1/content", json={"platform":"facebook","body":"private"}, headers={"X-Organization-Id":str(service.context.organization_id),"Idempotency-Key":str(uuid4())})
    expected = 503 if fails else 201
    assert response.status_code == expected and events == ["commit", expected]
    session.close.assert_awaited_once()
    if fails:
        assert response.headers["Retry-After"] == "30" and "synthetic" not in response.text


@pytest.mark.parametrize("action,payload", [("create",{"platform":"facebook","body":"x"}), ("patch",{"body":"new"}), ("submit-review",None), ("review",{"versionNo":1,"decision":"approved"})])
@pytest.mark.parametrize("key", [None,"invalid"])
async def test_idempotency_headers_required_http(service, action, payload, key):
    app = create_app()
    app.dependency_overrides[get_content_service] = lambda: service
    path = "/api/v1/content" + ("" if action=="create" else f"/{uuid4()}" + ("" if action=="patch" else f"/{action}"))
    headers = {} if key is None else {"Idempotency-Key":key}
    kwargs = {} if payload is None else {"json":payload}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        response = await client.request("PATCH" if action=="patch" else "POST", path, headers=headers, **kwargs)
    assert response.status_code == 422


async def test_replay_fingerprint_authorization_and_correlation(service):
    key, payload = uuid4(), ContentCreate(platform="facebook",body="x").model_dump()
    result = await service.mutate("create", key, payload)
    assert await service.mutate("create", key, payload) == result
    with pytest.raises(IdempotencyConflict):
        await service.mutate("create", key, {**payload,"body":"different"})
    service.context = OrganizationContext(service.context.organization_id, service.context.user_id, MemberRole.VIEWER)
    with pytest.raises(Forbidden):
        await service.mutate("create", key, payload)


async def test_current_only_pagination_hidden_id_and_invalid_cursor(service):
    for _ in range(3): await draft(service)
    first, cursor = await service.page(None,2)
    last, tail = await service.page(cursor,2)
    assert len(first)==2 and len(last)==1 and tail is None
    assert {r["id"] for r in first}.isdisjoint({r["id"] for r in last})
    with pytest.raises(ContentNotFound): await service.read(uuid4())
    from backend.app.modules.identity.errors import InvalidCursor
    with pytest.raises(InvalidCursor): await service.page("bad",50)


def test_no_service_commit_and_enum_alignment():
    root = Path(__file__).parents[1] / "app/modules/content"
    for filename in ("service.py", "repository.py", "workflow_service.py"):
        tree = ast.parse((root / filename).read_text(encoding="utf-8"))
        assert not any(isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=="commit" for n in ast.walk(tree))
    assert ContentItem.__table__.c.status.type.enums == [v.value for v in ContentStatus]
    assert not ContentItem.__table__.c.status.type.create_type
    assert ContentItem.__table__.c.platform.type.enums == ["facebook","whatsapp","instagram"]
    assert not ContentItem.__table__.c.platform.type.create_type
