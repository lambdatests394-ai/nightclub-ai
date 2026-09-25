"""Opt-in real PostgreSQL tests for Prompt 10 B2-C transactional scheduling."""

from datetime import UTC, datetime, timedelta, timezone
import os
from uuid import uuid4

import anyio
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Direct service tests bypass FastAPI's identity dependency composition. Load
# the canonical registry once so autoflush sees the real request metadata.
import backend.app.platform.model_registry  # noqa: F401
from backend.app.platform.database import Base
from backend.app.modules.automation.audit import PublicationAuditWriter
from backend.app.modules.automation.repository import PublicationRepository
from backend.app.modules.automation.service import PublicationService
from backend.app.modules.content.errors import ContentConflict, ContentNotFound
from backend.app.modules.content.repository import ContentRepository
from backend.app.modules.content.schemas import ContentScheduleRequest
from backend.app.modules.identity.policy import MemberRole, OrganizationContext
from backend.app.modules.integrations.repository import FacebookConnectionRepository
from backend.app.shared.errors import IdempotencyConflict
from backend.app.shared.idempotency import IdempotencyStore
from backend.tests.test_facebook_postgres import (
    A,
    B,
    ORG_A,
    ORG_B,
    create_connection,
    create_content,
    runtime,
    sqlstate,
    tenant,
)
from backend.tests.prompt10_postgres_safety import validated_prompt10_pair

pytestmark = pytest.mark.anyio
NOW = datetime(2026, 9, 25, 18, 0, tzinfo=UTC)
AUDIT_FIELDS = {
    "organizationId", "contentId", "publicationJobId", "versionNo",
    "previousStatus", "status", "publicationJobStatus", "scheduledFor", "immediate",
}
AUDIT_FORBIDDEN = {
    "body", "title", "linkUrl", "assetIds", "credentials", "credentialsCiphertext",
    "ciphertext", "accessToken", "oauthState", "providerRequest", "providerResponse",
}


def test_prompt10_publication_postgres_bootstrap_has_complete_metadata():
    required = {
        "organizations", "profiles", "campaigns", "platform_connections",
        "content_items", "content_versions", "publication_jobs", "idempotency_keys",
    }
    assert required <= set(Base.metadata.tables)


def safe_migration_url():
    try:
        migration, _ = validated_prompt10_pair(
            os.environ.get("DATABASE_MIGRATION_URL", ""),
            os.environ.get("PROMPT10_RUNTIME_URL", ""),
        )
        return migration
    except ValueError:
        pytest.fail(
            "Prompt 10 PostgreSQL URLs: unsafe or mismatched target (values withheld)",
            pytrace=False,
        )


@pytest.fixture
async def audit_inspector(request):
    """Migration-owner inspection exists only in this disposable certification DB."""
    if not request.config.getoption("--prompt10-postgres"):
        pytest.skip("Prompt 10 B2-C audit inspection opt-in is not enabled")
    engine = create_async_engine(
        safe_migration_url().set(drivername="postgresql+asyncpg"),
        pool_size=1,
        max_overflow=0,
        hide_parameters=True,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def inspect_audits(audit_inspector, organization_id, content_id, action):
    """Inspect committed audit rows without adding a runtime SELECT policy.

    The migration owner is deliberately NOBYPASSRLS, so FORCE RLS would hide
    every audit row from it.  The disposable certification inspector suspends
    FORCE only inside its own transaction and always rolls that transaction
    back before returning.  A fresh transaction then proves ENABLE/FORCE were
    restored.  Runtime grants and policies are never changed.
    """
    async with audit_inspector() as session:
        transaction = await session.begin()
        try:
            identity = (await session.execute(text("""SELECT current_database(),current_user,
                pg_get_userbyid(c.relowner),c.relrowsecurity,c.relforcerowsecurity
                FROM pg_class c WHERE c.oid='public.audit_logs'::regclass"""))).one()
            assert identity == (
                safe_migration_url().database,
                "alembic_test_user",
                "alembic_test_user",
                True,
                True,
            )
            await session.execute(text(
                "ALTER TABLE public.audit_logs NO FORCE ROW LEVEL SECURITY"
            ))
            rows = (await session.execute(text("""SELECT action,entity_type,entity_id,
                organization_id,after FROM public.audit_logs
                WHERE organization_id=:organization AND entity_type='content'
                  AND entity_id=:id AND action=:action ORDER BY id"""), {
                    "organization": organization_id,
                    "id": content_id,
                    "action": action,
                })).all()
        finally:
            await transaction.rollback()
    async with audit_inspector() as verifier:
        restored = (await verifier.execute(text("""SELECT relrowsecurity,relforcerowsecurity
            FROM pg_class WHERE oid='public.audit_logs'::regclass"""))).one()
        assert restored == (True, True)
    return rows


def assert_structural_audit(row, action, organization_id, content_id):
    assert row.action == action
    assert row.entity_type == "content"
    assert row.entity_id == content_id
    assert row.organization_id == organization_id
    assert set(row.after) == AUDIT_FIELDS
    assert not (AUDIT_FORBIDDEN & set(row.after))


def service(session, organization=ORG_A, actor=A, *, now=NOW):
    context = OrganizationContext(organization, actor, MemberRole.OWNER)
    return PublicationService(
        context,
        ContentRepository(session, organization),
        PublicationRepository(session, organization),
        FacebookConnectionRepository(session, organization, actor),
        IdempotencyStore(session, context),
        PublicationAuditWriter(session, context, uuid4()),
        clock=lambda: now,
    )


async def schedule(runtime, content, *, key=None, when=None, organization=ORG_A, actor=A):
    key = key or uuid4()
    async with tenant(runtime, actor, organization) as session:
        result = await service(session, organization, actor).schedule(
            content,
            key,
            ContentScheduleRequest(
                versionNo=1,
                scheduledFor=when or NOW + timedelta(hours=1),
            ),
        )
    return key, result


async def test_schedule_binds_exact_version_and_persists_one_atomic_job(runtime, audit_inspector):
    connection = await create_connection(runtime)
    content, version = await create_content(runtime, connection)
    _, (status, response) = await schedule(runtime, content)
    assert status == 201

    async with tenant(runtime) as session:
        item = (await session.execute(text("""SELECT status,scheduled_for,current_version_no,
            approved_version_no FROM content_items WHERE id=:id"""), {"id": content})).one()
        job = (await session.execute(text("""SELECT id,content_version_id,status,attempt_count,
            lease_token,lease_expires_at,published_external_id,next_attempt_at
            FROM publication_jobs WHERE content_item_id=:id"""), {"id": content})).one()
        attempts = await session.scalar(text("""SELECT count(*) FROM publication_attempts pa
            JOIN publication_jobs pj ON pj.id=pa.publication_job_id
            WHERE pj.content_item_id=:id"""), {"id": content})
    audits = await inspect_audits(audit_inspector, ORG_A, content, "content.scheduled")

    assert item.status == "scheduled" and item.scheduled_for == NOW + timedelta(hours=1)
    assert item.current_version_no == item.approved_version_no == 1
    assert job.content_version_id == version and job.status == "pending"
    assert job.attempt_count == 0 and job.lease_token is None and job.lease_expires_at is None
    assert job.published_external_id is None and job.next_attempt_at is None
    assert attempts == 0 and len(audits) == 1
    assert_structural_audit(audits[0], "content.scheduled", ORG_A, content)
    assert response["publicationJobId"] == str(job.id)


async def test_schedule_durable_replay_semantic_conflicts_and_timezone_normalization(runtime):
    connection = await create_connection(runtime)
    content, _ = await create_content(runtime, connection)
    key = uuid4()
    instant = NOW + timedelta(hours=2)
    offset = timezone(timedelta(hours=-6))
    _, first = await schedule(runtime, content, key=key, when=instant.astimezone(offset))
    _, replay = await schedule(runtime, content, key=key, when=instant)
    assert replay == first

    with pytest.raises(IdempotencyConflict):
        await schedule(runtime, content, key=key, when=instant + timedelta(minutes=1))
    with pytest.raises(IdempotencyConflict):
        async with tenant(runtime) as session:
            await service(session).schedule(
                content,
                key,
                ContentScheduleRequest(versionNo=2, scheduledFor=instant),
            )
    async with tenant(runtime) as session:
        assert await session.scalar(text(
            "SELECT count(*) FROM publication_jobs WHERE content_item_id=:id"),
            {"id": content},
        ) == 1
        row = (await session.execute(text("""SELECT state,response_status,response_body
            FROM idempotency_keys WHERE key=:key"""), {"key": key})).one()
    assert row.state == "completed" and row.response_status == 201
    assert row.response_body == first[1]


async def test_second_active_job_and_distinct_key_race_produce_one_winner(runtime, audit_inspector):
    connection = await create_connection(runtime)
    content, _ = await create_content(runtime, connection)
    outcomes = []

    async def contender(key):
        try:
            await schedule(runtime, content, key=key)
            outcomes.append("created")
        except ContentConflict:
            outcomes.append("conflict")

    async with anyio.create_task_group() as group:
        group.start_soon(contender, uuid4())
        group.start_soon(contender, uuid4())

    assert sorted(outcomes) == ["conflict", "created"]
    async with tenant(runtime) as session:
        assert await session.scalar(text(
            "SELECT count(*) FROM publication_jobs WHERE content_item_id=:id"),
            {"id": content},
        ) == 1
        assert await session.scalar(text("""SELECT count(*) FROM idempotency_keys
            WHERE operation=:operation AND state='completed'"""),
            {"operation": f"content:schedule:{content}"}) == 1
        assert await session.scalar(text("""SELECT count(*) FROM publication_attempts pa
            JOIN publication_jobs pj ON pj.id=pa.publication_job_id
            WHERE pj.content_item_id=:id"""), {"id": content}) == 0
    audits = await inspect_audits(audit_inspector, ORG_A, content, "content.scheduled")
    assert len(audits) == 1
    assert_structural_audit(audits[0], "content.scheduled", ORG_A, content)


async def test_cancel_is_non_destructive_and_preserves_version_pointers(runtime, audit_inspector):
    connection = await create_connection(runtime)
    content, _ = await create_content(runtime, connection)
    _, (_, scheduled) = await schedule(runtime, content)
    async with tenant(runtime) as session:
        status, response = await service(session).cancel_schedule(content, uuid4())
    assert status == 200 and response["scheduledFor"] is None

    async with tenant(runtime) as session:
        item = (await session.execute(text("""SELECT status,scheduled_for,current_version_no,
            approved_version_no FROM content_items WHERE id=:id"""), {"id": content})).one()
        job = (await session.execute(text(
            "SELECT id,status FROM publication_jobs WHERE content_item_id=:id"),
            {"id": content},
        )).one()
        attempts = await session.scalar(text("""SELECT count(*) FROM publication_attempts pa
            JOIN publication_jobs pj ON pj.id=pa.publication_job_id
            WHERE pj.content_item_id=:id"""), {"id": content})
    assert item.status == "approved" and item.scheduled_for is None
    assert item.current_version_no == item.approved_version_no == 1
    assert str(job.id) == scheduled["publicationJobId"] and job.status == "cancelled"
    assert attempts == 0
    audits = await inspect_audits(
        audit_inspector, ORG_A, content, "content.schedule_cancelled",
    )
    assert len(audits) == 1
    assert_structural_audit(
        audits[0], "content.schedule_cancelled", ORG_A, content,
    )


@pytest.mark.parametrize("job_status", ["leased", "publishing", "retryable_failure"])
async def test_cancel_rejects_started_execution_without_partial_mutation(
        runtime, audit_inspector, job_status):
    connection = await create_connection(runtime)
    content, _ = await create_content(runtime, connection)
    await schedule(runtime, content)
    async with tenant(runtime) as session:
        job = await session.scalar(text(
            "SELECT id FROM publication_jobs WHERE content_item_id=:id"), {"id": content})
        await session.execute(text("""UPDATE publication_jobs SET status='leased',
            lease_token=:lease,lease_expires_at=now()+interval '5 minutes' WHERE id=:id"""),
            {"id": job, "lease": uuid4()})
        if job_status in {"publishing", "retryable_failure"}:
            await session.execute(text(
                "UPDATE publication_jobs SET status='publishing' WHERE id=:id"), {"id": job})
        if job_status == "retryable_failure":
            await session.execute(text("""UPDATE publication_jobs
                SET status='retryable_failure',lease_token=NULL,lease_expires_at=NULL,
                    next_attempt_at=now()+interval '1 minute' WHERE id=:id"""), {"id": job})
    cancel_key = uuid4()
    with pytest.raises(ContentConflict):
        async with tenant(runtime) as session:
            await service(session).cancel_schedule(content, cancel_key)
    async with tenant(runtime) as session:
        item = (await session.execute(text("""SELECT status,scheduled_for,current_version_no,
            approved_version_no FROM content_items WHERE id=:id"""), {"id": content})).one()
        persisted = await session.scalar(text(
            "SELECT status FROM publication_jobs WHERE id=:id"), {"id": job})
        assert await session.scalar(text(
            "SELECT count(*) FROM idempotency_keys WHERE key=:key"), {"key": cancel_key}) == 0
    assert await inspect_audits(
        audit_inspector, ORG_A, content, "content.schedule_cancelled",
    ) == []
    assert item.status == "scheduled" and item.scheduled_for is not None
    assert item.current_version_no == item.approved_version_no == 1
    assert persisted == job_status


async def test_publish_now_creates_pending_job_without_attempt(runtime, audit_inspector):
    connection = await create_connection(runtime)
    content, version = await create_content(runtime, connection)
    async with tenant(runtime) as session:
        status, response = await service(session).publish_now(content, uuid4())
    assert status == 201 and response["scheduledFor"] == "2026-09-25T18:00:00Z"
    async with tenant(runtime) as session:
        row = (await session.execute(text("""SELECT pj.status,pj.scheduled_for,
            pj.content_version_id,(SELECT count(*) FROM publication_attempts pa
            WHERE pa.publication_job_id=pj.id) AS attempts
            FROM publication_jobs pj WHERE pj.content_item_id=:id"""), {"id": content})).one()
    assert row.status == "pending" and row.scheduled_for == NOW
    assert row.content_version_id == version and row.attempts == 0
    audits = await inspect_audits(
        audit_inspector, ORG_A, content, "content.publish_requested",
    )
    assert len(audits) == 1
    assert_structural_audit(
        audits[0], "content.publish_requested", ORG_A, content,
    )


async def test_runtime_cannot_select_audit_logs(runtime):
    with pytest.raises(DBAPIError) as error:
        async with tenant(runtime) as session:
            await session.execute(text("SELECT count(*) FROM audit_logs"))
    assert sqlstate(error) == "42501"


async def test_unsupported_transition_rolls_back_job_and_idempotency(runtime):
    connection = await create_connection(runtime)
    content, _ = await create_content(runtime, connection, status="draft")
    key = uuid4()
    with pytest.raises(ContentConflict):
        await schedule(runtime, content, key=key)
    async with tenant(runtime) as session:
        assert await session.scalar(text(
            "SELECT count(*) FROM publication_jobs WHERE content_item_id=:id"),
            {"id": content},
        ) == 0
        assert await session.scalar(text(
            "SELECT count(*) FROM idempotency_keys WHERE key=:key"), {"key": key}) == 0


async def test_cross_tenant_schedule_and_cancel_are_hidden(runtime):
    connection = await create_connection(runtime)
    content, _ = await create_content(runtime, connection)
    with pytest.raises(ContentNotFound):
        await schedule(runtime, content, organization=ORG_B, actor=B)

    await schedule(runtime, content)
    with pytest.raises(ContentNotFound):
        async with tenant(runtime, B, ORG_B) as session:
            await service(session, ORG_B, B).cancel_schedule(content, uuid4())


async def test_runtime_cannot_delete_publication_job(runtime):
    connection = await create_connection(runtime)
    content, _ = await create_content(runtime, connection)
    await schedule(runtime, content)
    with pytest.raises(DBAPIError) as error:
        async with tenant(runtime) as session:
            await session.execute(text(
                "DELETE FROM publication_jobs WHERE content_item_id=:id"), {"id": content})
    assert sqlstate(error) == "42501"
