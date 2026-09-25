"""Prompt 10 B2-C publication service tests; no database or provider I/O."""

import ast
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from backend.app.modules.automation.models import PublicationJob
from backend.app.modules.automation.repository import PublicationRepository
from backend.app.modules.automation.service import PublicationService
from backend.app.modules.content.errors import ContentConflict, ContentNotFound, InvalidContentRequest
from backend.app.modules.content.schemas import ContentScheduleRequest
from backend.app.modules.identity.errors import Forbidden
from backend.app.modules.identity.policy import MemberRole, OrganizationContext
from backend.app.platform.enums import ContentStatus
from backend.app.shared.errors import IdempotencyConflict

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 9, 24, 18, 0, tzinfo=UTC)


class MemoryIdempotency:
    def __init__(self, context):
        self.context = context
        self.rows = {}

    async def claim(self, key, operation, digest):
        owner = (self.context.organization_id, self.context.user_id)
        if key not in self.rows:
            self.rows[key] = [owner, operation, digest, None]
            return None
        row = self.rows[key]
        if row[:3] != [owner, operation, digest] or row[3] is None:
            raise IdempotencyConflict()
        return row[3]

    async def complete(self, key, status, body):
        self.rows[key][3] = (status, body)


class MemoryContent:
    def __init__(self, context):
        self.context = context
        self.item = SimpleNamespace(
            id=uuid4(), organization_id=context.organization_id,
            created_by=context.user_id, platform="facebook", connection_id=uuid4(),
            status=ContentStatus.APPROVED, current_version_no=2,
            approved_version_no=2, scheduled_for=None,
        )
        self.version_row = SimpleNamespace(
            id=uuid4(), content_item_id=self.item.id, version_no=2,
            body="private launch copy", title="private title", link_url=None,
        )
        self.attachments = []
        self.locked = []

    async def get_item(self, content_id):
        self.locked.append(content_id)
        return self.item if content_id == self.item.id else None

    async def version(self, content_id, version_no):
        if (content_id, version_no) == (self.item.id, self.version_row.version_no):
            return self.version_row
        return None

    async def asset_ids(self, version_id):
        assert version_id == self.version_row.id
        return list(self.attachments)

    async def save_publication_state(self, state, scheduled_for):
        self.item.status = state.status
        self.item.scheduled_for = scheduled_for


class MemoryPublication:
    ACTIVE = {"pending", "leased", "publishing", "retryable_failure"}

    def __init__(self):
        self.jobs = []
        self.locked = []

    async def active_for_content(self, content_id, *, lock=False):
        if lock:
            self.locked.append(content_id)
        return next((job for job in self.jobs
                     if job.content_item_id == content_id and job.status in self.ACTIVE), None)

    async def add_pending(self, job):
        if await self.active_for_content(job.content_item_id):
            raise AssertionError("service failed to reject an existing active job")
        self.jobs.append(job)

    async def cancel_pending(self, job_id):
        job = next((candidate for candidate in self.jobs if candidate.id == job_id), None)
        if job is None or job.status != "pending":
            return False
        job.status = "cancelled"
        return True


class MemoryConnections:
    def __init__(self, content):
        self.connection = SimpleNamespace(
            id=content.item.connection_id, platform="facebook", status="active",
            token_expires_at=None,
        )
        self.credential_reads = 0

    async def get_facebook_connection(self, connection_id):
        return self.connection if self.connection and self.connection.id == connection_id else None

    async def get_facebook_credentials(self, _connection_id):
        self.credential_reads += 1
        raise AssertionError("B2-C must not read or decrypt credentials")


@pytest.fixture
def publication():
    context = OrganizationContext(uuid4(), uuid4(), MemberRole.OWNER)
    content = MemoryContent(context)
    jobs = MemoryPublication()
    connections = MemoryConnections(content)
    idempotency = MemoryIdempotency(context)
    audit = AsyncMock()
    service = PublicationService(
        context, content, jobs, connections, idempotency, audit, clock=lambda: NOW,
    )
    return SimpleNamespace(
        service=service, context=context, content=content, jobs=jobs,
        connections=connections, idempotency=idempotency, audit=audit,
    )


def request(*, version=2, when=None):
    return ContentScheduleRequest(versionNo=version, scheduledFor=when or NOW + timedelta(hours=2))


async def test_schedule_creates_exact_pending_job_and_structural_audit(publication):
    item = publication.content.item
    approved = item.approved_version_no
    status, body = await publication.service.schedule(item.id, uuid4(), request())

    assert status == 201
    assert set(body) == {
        "contentId", "contentStatus", "versionNo", "publicationJobId",
        "publicationJobStatus", "scheduledFor", "connectionId",
    }
    assert body["contentStatus"] == "scheduled" and body["publicationJobStatus"] == "pending"
    assert "private" not in repr(body)
    job = publication.jobs.jobs[0]
    assert job.content_version_id == publication.content.version_row.id
    assert job.content_item_id == item.id and job.created_by == publication.context.user_id
    assert job.attempt_count == 0 and job.lease_token is None and job.lease_expires_at is None
    assert job.published_external_id is None and job.next_attempt_at is None
    assert item.status == ContentStatus.SCHEDULED and item.scheduled_for == NOW + timedelta(hours=2)
    assert item.current_version_no == approved == item.approved_version_no == 2
    publication.audit.write.assert_awaited_once()
    action = publication.audit.write.await_args.args[0]
    metadata = publication.audit.write.await_args.kwargs
    assert action == "content.scheduled"
    assert set(metadata) == {
        "content_id", "job_id", "version_no", "previous_status", "status",
        "job_status", "scheduled_for", "immediate",
    }
    assert publication.connections.credential_reads == 0


async def test_non_utc_schedule_normalizes_and_replays_semantically(publication):
    key = uuid4()
    offset = timezone(timedelta(hours=-6))
    local = (NOW + timedelta(hours=2)).astimezone(offset)
    first = await publication.service.schedule(publication.content.item.id, key, request(when=local))
    second = await publication.service.schedule(
        publication.content.item.id, key, request(when=NOW + timedelta(hours=2)),
    )
    assert first == second
    assert first[1]["scheduledFor"] == "2026-09-24T20:00:00Z"
    assert len(publication.jobs.jobs) == 1


@pytest.mark.parametrize("role,allowed", [
    (MemberRole.OWNER, True), (MemberRole.MANAGER, True),
    (MemberRole.EDITOR, False), (MemberRole.REVIEWER, False),
    (MemberRole.OPERATOR, False), (MemberRole.VIEWER, False),
])
async def test_publication_rbac(publication, role, allowed):
    publication.context = OrganizationContext(
        publication.context.organization_id, publication.context.user_id, role,
    )
    publication.service.context = publication.context
    if allowed:
        assert (await publication.service.schedule(
            publication.content.item.id, uuid4(), request(),
        ))[0] == 201
    else:
        with pytest.raises(Forbidden):
            await publication.service.schedule(publication.content.item.id, uuid4(), request())
        assert publication.idempotency.rows == {} and publication.jobs.jobs == []


@pytest.mark.parametrize("field,value", [
    ("platform", "whatsapp"),
    ("status", ContentStatus.DRAFT),
    ("connection_id", None),
])
async def test_schedule_rejects_unsupported_content(publication, field, value):
    setattr(publication.content.item, field, value)
    with pytest.raises(ContentConflict):
        await publication.service.schedule(publication.content.item.id, uuid4(), request())


async def test_schedule_not_found(publication):
    with pytest.raises(ContentNotFound):
        await publication.service.schedule(uuid4(), uuid4(), request())


@pytest.mark.parametrize("requested,current,approved", [(1, 2, 2), (2, 2, 1), (3, 3, None)])
async def test_schedule_rejects_stale_or_unapproved_version(publication, requested, current, approved):
    publication.content.item.current_version_no = current
    publication.content.item.approved_version_no = approved
    with pytest.raises(ContentConflict):
        await publication.service.schedule(
            publication.content.item.id, uuid4(), request(version=requested),
        )


@pytest.mark.parametrize("status", ["pending", "expired", "revoked", "error"])
async def test_schedule_rejects_nonactive_connection(publication, status):
    publication.connections.connection.status = status
    with pytest.raises(ContentConflict):
        await publication.service.schedule(publication.content.item.id, uuid4(), request())


async def test_schedule_rejects_missing_foreign_or_expired_connection(publication):
    publication.connections.connection = None
    with pytest.raises(ContentConflict):
        await publication.service.schedule(publication.content.item.id, uuid4(), request())

    publication.connections = MemoryConnections(publication.content)
    publication.service.connection_repository = publication.connections
    publication.connections.connection.token_expires_at = NOW
    with pytest.raises(ContentConflict):
        await publication.service.schedule(publication.content.item.id, uuid4(), request())


@pytest.mark.parametrize("link", [
    "http://example.com/post", "https://user:pass@example.com/post",
    "https://", "https://example.com/bad\npath", "https://[invalid",
])
async def test_schedule_rejects_unsafe_links(publication, link):
    publication.content.version_row.link_url = link
    with pytest.raises(ContentConflict):
        await publication.service.schedule(publication.content.item.id, uuid4(), request())


@pytest.mark.parametrize("link", [None, "https://example.com/post?q=1"])
async def test_schedule_accepts_text_only_or_safe_https(publication, link):
    publication.content.version_row.link_url = link
    assert (await publication.service.schedule(
        publication.content.item.id, uuid4(), request(),
    ))[0] == 201


async def test_schedule_rejects_any_asset_and_existing_active_job(publication):
    publication.content.attachments = [uuid4()]
    with pytest.raises(ContentConflict):
        await publication.service.schedule(publication.content.item.id, uuid4(), request())
    publication.content.attachments = []
    publication.jobs.jobs.append(SimpleNamespace(
        id=uuid4(), content_item_id=publication.content.item.id, status="pending",
    ))
    with pytest.raises(ContentConflict):
        await publication.service.schedule(publication.content.item.id, uuid4(), request())


async def test_schedule_rejects_past(publication):
    with pytest.raises(InvalidContentRequest):
        # Pydantic permits a valid aware datetime; the service owns future-time semantics.
        await publication.service.schedule(
            publication.content.item.id, uuid4(), request(when=NOW - timedelta(seconds=1)),
        )


@pytest.mark.parametrize("payload", [
    {"versionNo": 0, "scheduledFor": NOW + timedelta(hours=1)},
    {"versionNo": True, "scheduledFor": NOW + timedelta(hours=1)},
    {"versionNo": 1, "scheduledFor": datetime(2026, 9, 24, 20, 0)},
    {"versionNo": 1, "scheduledFor": NOW + timedelta(hours=1), "extra": "forbidden"},
])
async def test_schedule_schema_is_strict(payload):
    with pytest.raises(ValidationError):
        ContentScheduleRequest.model_validate(payload)


async def test_same_key_changed_schedule_semantics_conflicts(publication):
    key = uuid4()
    await publication.service.schedule(publication.content.item.id, key, request())
    for changed in (
        request(version=1),
        request(when=NOW + timedelta(hours=3)),
    ):
        with pytest.raises(IdempotencyConflict):
            await publication.service.schedule(publication.content.item.id, key, changed)


async def test_same_key_other_actor_or_tenant_never_replays(publication):
    key = uuid4()
    await publication.service.schedule(publication.content.item.id, key, request())
    for context in (
        OrganizationContext(publication.context.organization_id, uuid4(), MemberRole.OWNER),
        OrganizationContext(uuid4(), publication.context.user_id, MemberRole.OWNER),
    ):
        publication.idempotency.context = context
        publication.service.context = context
        with pytest.raises(IdempotencyConflict):
            await publication.service.schedule(publication.content.item.id, key, request())


async def test_cancel_preserves_job_and_approval_and_replays(publication):
    item = publication.content.item
    await publication.service.schedule(item.id, uuid4(), request())
    job = publication.jobs.jobs[0]
    key = uuid4()
    first = await publication.service.cancel_schedule(item.id, key)
    second = await publication.service.cancel_schedule(item.id, key)
    assert first == second and first[0] == 200
    assert item.status == ContentStatus.APPROVED and item.scheduled_for is None
    assert item.current_version_no == item.approved_version_no == 2
    assert publication.jobs.jobs == [job] and job.status == "cancelled"
    assert publication.audit.write.await_args_list[-1].args[0] == "content.schedule_cancelled"


@pytest.mark.parametrize("status", [
    ContentStatus.DRAFT, ContentStatus.APPROVED, ContentStatus.PUBLISHING,
    ContentStatus.PUBLISHED, ContentStatus.FAILED,
])
async def test_cancel_rejects_non_scheduled_content(publication, status):
    publication.content.item.status = status
    with pytest.raises(ContentConflict):
        await publication.service.cancel_schedule(publication.content.item.id, uuid4())


@pytest.mark.parametrize("job_status", [None, "leased", "publishing", "retryable_failure"])
async def test_cancel_requires_pending_active_job(publication, job_status):
    publication.content.item.status = ContentStatus.SCHEDULED
    if job_status:
        publication.jobs.jobs.append(SimpleNamespace(
            id=uuid4(), content_item_id=publication.content.item.id, status=job_status,
            content_version_id=publication.content.version_row.id, scheduled_for=NOW,
            created_by=publication.context.user_id,
        ))
    with pytest.raises(ContentConflict):
        await publication.service.cancel_schedule(publication.content.item.id, uuid4())


async def test_publish_now_uses_one_server_instant_and_pending_job_only(publication):
    calls = 0

    def clock():
        nonlocal calls
        calls += 1
        return NOW + timedelta(seconds=calls - 1)

    publication.service.clock = clock
    first = await publication.service.publish_now(publication.content.item.id, uuid4())
    job = publication.jobs.jobs[0]
    assert first[0] == 201 and calls == 1
    assert job.scheduled_for == NOW == publication.content.item.scheduled_for
    assert first[1]["scheduledFor"] == "2026-09-24T18:00:00Z"
    assert job.status == "pending" and job.attempt_count == 0
    assert publication.audit.write.await_args.args[0] == "content.publish_requested"


async def test_publish_now_replay_keeps_original_time(publication):
    key = uuid4()
    first = await publication.service.publish_now(publication.content.item.id, key)
    publication.service.clock = lambda: NOW + timedelta(days=1)
    assert await publication.service.publish_now(publication.content.item.id, key) == first
    assert len(publication.jobs.jobs) == 1


async def test_job_insert_uses_only_certified_runtime_insert_columns(publication):
    session = AsyncMock()
    job = PublicationJob(
        id=uuid4(), content_item_id=uuid4(), content_version_id=uuid4(),
        idempotency_key=uuid4(), scheduled_for=NOW, status="pending",
        attempt_count=0, created_by=uuid4(),
    )
    assert inspect(job).transient
    await PublicationRepository(session, uuid4()).add_pending(job)
    assert inspect(job).transient
    session.add.assert_not_called()
    statement = session.execute.await_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect())).lower()
    columns = sql.split("(", 1)[1].split(")", 1)[0]
    assert set(part.strip() for part in columns.split(",")) == {
        "id", "content_item_id", "content_version_id", "idempotency_key",
        "scheduled_for", "status", "attempt_count", "next_attempt_at", "created_by",
    }
    assert "returning" not in sql


def test_b2c_core_has_no_provider_storage_credential_or_attempt_io():
    root = Path(__file__).parents[1] / "app"
    paths = (
        root / "api/v1/content.py",
        root / "modules/automation/dependencies.py",
        root / "modules/automation/repository.py",
        root / "modules/automation/service.py",
    )
    forbidden_imports = ("httpx", "supabase", "storage", "n8n")
    forbidden_calls = {
        "get_facebook_credentials", "decrypt", "create_signed_url",
        "begin_publishing", "add_publication_attempt",
    }
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = [
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        ] + [
            alias.name
            for node in ast.walk(tree) if isinstance(node, ast.Import)
            for alias in node.names
        ]
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert not any(token in module.lower() for module in imports for token in forbidden_imports)
        assert calls.isdisjoint(forbidden_calls)
