"""Opt-in real PostgreSQL checks for tenant-local system automation authority."""

import base64
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core.database_security import (
    establish_organization_context,
    establish_system_automation_context,
    establish_user_context,
    verify_runtime_role,
)
from backend.app.core.scheduler_database import (
    set_scheduler_transaction_read_only,
    verify_scheduler_role,
)
from backend.app.modules.automation.executor import (
    FacebookPublicationExecutor,
    SQLAlchemyPublicationExecutionStore,
)
from backend.app.modules.automation.schemas import ExecutionStatus
from backend.app.modules.identity.errors import IdentityUnavailable
from backend.app.modules.identity.policy import (
    CurrentUser,
    MemberRole,
    OrganizationContext,
    SystemAutomationContext,
)
from backend.app.modules.integrations.credentials import CredentialCipher
from backend.app.modules.integrations.facebook_provider import (
    ProviderDisposition,
    ProviderResult,
    ReconciliationDisposition,
    ReconciliationResult,
)


pytestmark = pytest.mark.anyio
DATABASE = "nightclub_ai_prompt11_system_test"
USER_A = UUID("10000000-0000-0000-0000-000000000001")
REMOVED_USER = UUID("10000000-0000-0000-0000-000000000008")
ORG_A = UUID("20000000-0000-0000-0000-000000000001")
ORG_B = UUID("20000000-0000-0000-0000-000000000002")
ORG_INACTIVE = UUID("20000000-0000-0000-0000-000000000003")
CONTENT_A = UUID("96000000-0000-0000-0000-000000000001")
CONTENT_B = UUID("96000000-0000-0000-0000-000000000002")
VERSION_A = UUID("96100000-0000-0000-0000-000000000001")
VERSION_B = UUID("96100000-0000-0000-0000-000000000002")
JOB_A = UUID("96200000-0000-0000-0000-000000000001")
JOB_B = UUID("96200000-0000-0000-0000-000000000002")
ATTEMPT_A = UUID("96400000-0000-0000-0000-000000000001")
ATTEMPT_B = UUID("96400000-0000-0000-0000-000000000002")
CONNECTION_A = UUID("30000000-0000-0000-0000-000000000001")
CONNECTION_B = UUID("30000000-0000-0000-0000-000000000003")
EXECUTION_JOB = UUID("96200000-0000-0000-0000-000000000003")
INACTIVE_JOB = UUID("96200000-0000-0000-0000-000000000004")
CANCELLED_JOB = UUID("96200000-0000-0000-0000-000000000005")
NOW = datetime(2026, 9, 28, 18, 0, tzinfo=UTC)
SYNTHETIC_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")


def guarded_url(name: str, expected_user: str):
    try:
        url = make_url(os.environ.get(name, ""))
    except Exception:
        pytest.fail(f"{name}: explicit local environment required (value withheld)", pytrace=False)
    if (url.host not in {"localhost", "127.0.0.1"} or url.port != 5432
            or url.database != DATABASE or url.username != expected_user or not url.password
            or url.query or url.drivername not in {"postgresql+asyncpg", "postgresql+psycopg"}):
        pytest.fail(f"{name}: unsafe target (value withheld)", pytrace=False)
    return url


async def _factory(name: str, role: str):
    engine = create_async_engine(
        guarded_url(name, role).set(drivername="postgresql+asyncpg"),
        pool_size=1, max_overflow=0, pool_pre_ping=True, hide_parameters=True,
    )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def runtime_factory(request):
    if not request.config.getoption("--prompt11-system-postgres"):
        pytest.skip("Prompt 11 system automation PostgreSQL opt-in is not enabled")
    engine, factory = await _factory("PROMPT11_RUNTIME_URL", "nightclub_api")
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture
async def scheduler_factory(request):
    if not request.config.getoption("--prompt11-system-postgres"):
        pytest.skip("Prompt 11 system automation PostgreSQL opt-in is not enabled")
    engine, factory = await _factory("PROMPT11_SCHEDULER_URL", "nightclub_scheduler")
    try:
        yield factory
    finally:
        await engine.dispose()


@asynccontextmanager
async def system_transaction(factory, organization_id=ORG_A):
    async with factory() as session:
        async with session.begin():
            await verify_runtime_role(session, "nightclub_api")
            await establish_system_automation_context(
                session, SystemAutomationContext(organization_id),
            )
            yield session


@contextmanager
def publication_audit_inserts(factory):
    """Observe committed executor INSERTs; nightclub_api has no audit SELECT ACL."""
    observed = []
    engine = factory.kw["bind"]

    def observe(_connection, _cursor, statement, _parameters, context, _many):
        if not statement.lstrip().upper().startswith("INSERT INTO AUDIT_LOGS"):
            return
        for values in context.compiled_parameters:
            observed.append({
                "actor_type": values.get("actor_type"),
                "actor_id": values.get("actor_id"),
                "action": values.get("action"),
                "after": values.get("after"),
            })

    event.listen(engine.sync_engine, "before_cursor_execute", observe)
    try:
        yield observed
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observe)


@asynccontextmanager
async def raw_security_context(
        factory, *, organization_id: UUID, execution_context: str,
        user_id: str):
    """Install deliberately incomplete combinations to test effective RLS."""
    async with factory() as session:
        async with session.begin():
            await verify_runtime_role(session, "nightclub_api")
            for name, value in (
                ("app.organization_id", str(organization_id)),
                ("app.user_id", user_id),
                ("app.execution_context", execution_context),
            ):
                await session.execute(text(
                    "SELECT set_config(:name,:value,true)"
                ), {"name": name, "value": value})
            yield session


async def test_system_context_is_transaction_local_and_user_id_is_empty(runtime_factory):
    async with system_transaction(runtime_factory) as session:
        assert (await session.execute(text("""SELECT
            current_user,current_setting('app.execution_context',true),
            current_setting('app.organization_id',true),
            current_setting('app.user_id',true)"""))).one() == (
                "nightclub_api", "system_automation", str(ORG_A), "",
            )
    async with runtime_factory() as session:
        async with session.begin():
            assert (await session.execute(text("""SELECT
                NULLIF(current_setting('app.execution_context',true),''),
                NULLIF(current_setting('app.organization_id',true),''),
                NULLIF(current_setting('app.user_id',true),'')"""))).one() == (
                    None, None, None,
                )


async def test_system_context_clears_after_rollback_and_pool_reuse(runtime_factory):
    with pytest.raises(RuntimeError, match="rollback sentinel"):
        async with system_transaction(runtime_factory) as session:
            assert await session.scalar(text("SELECT current_setting('app.execution_context')")) == "system_automation"
            raise RuntimeError("rollback sentinel")
    async with runtime_factory() as session:
        async with session.begin():
            assert await session.scalar(text(
                "SELECT NULLIF(current_setting('app.execution_context',true),'')"
            )) is None


async def test_contexts_are_mutually_exclusive_and_tenant_cannot_change(runtime_factory):
    async with runtime_factory() as session:
        with pytest.raises(IdentityUnavailable):
            async with session.begin():
                await establish_user_context(session, CurrentUser(USER_A))
                await establish_system_automation_context(
                    session, SystemAutomationContext(ORG_A),
                )
    async with runtime_factory() as session:
        with pytest.raises(IdentityUnavailable):
            async with session.begin():
                await establish_system_automation_context(
                    session, SystemAutomationContext(ORG_A),
                )
                await establish_system_automation_context(
                    session, SystemAutomationContext(ORG_B),
                )


async def test_system_a_visibility_is_tenant_local_across_execution_surface(runtime_factory):
    async with system_transaction(runtime_factory) as session:
        assertions = (
            ("organizations", ORG_A, ORG_B),
            ("content_items", CONTENT_A, CONTENT_B),
            ("content_versions", VERSION_A, VERSION_B),
            ("publication_jobs", JOB_A, JOB_B),
            ("platform_connections", CONNECTION_A, CONNECTION_B),
        )
        for table, own_id, foreign_id in assertions:
            visible = set((await session.execute(text(
                f"SELECT id FROM public.{table} WHERE id IN (:own,:foreign)"
            ), {"own": own_id, "foreign": foreign_id})).scalars())
            assert visible == {own_id}
        assets = set((await session.execute(text("""SELECT content_item_id
            FROM public.content_assets WHERE content_item_id IN (:own,:foreign)"""), {
                "own": CONTENT_A, "foreign": CONTENT_B,
            })).scalars())
        assert assets == {CONTENT_A}
        attempts = set((await session.execute(text("""SELECT id
            FROM public.publication_attempts WHERE id IN (:own,:foreign)"""), {
                "own": ATTEMPT_A, "foreign": ATTEMPT_B,
            })).scalars())
        assert attempts == {ATTEMPT_A}


@pytest.mark.parametrize(("execution_context", "user_id"), [
    ("", ""),
    ("some_other_value", ""),
    ("system_automation", "99999999-0000-0000-0000-000000000001"),
], ids=[
    "organization_id_only",
    "wrong_execution_context",
    "system_context_with_non_empty_nonmember_user",
])
async def test_incomplete_system_context_cannot_gain_effective_access_through_historical_policies(
        runtime_factory, execution_context, user_id):
    """Historical permissive policies must not bootstrap system authority."""
    async with raw_security_context(
            runtime_factory, organization_id=ORG_A,
            execution_context=execution_context, user_id=user_id) as session:
        checks = (
            ("organizations", "id", ORG_A),
            ("publication_jobs", "id", JOB_A),
            ("publication_attempts", "id", ATTEMPT_A),
            ("content_items", "id", CONTENT_A),
            ("content_versions", "id", VERSION_A),
            ("content_assets", "content_item_id", CONTENT_A),
            ("platform_connections", "id", CONNECTION_A),
        )
        for table, column, identifier in checks:
            assert await session.scalar(text(
                f"SELECT count(*) FROM public.{table} WHERE {column}=:identifier"
            ), {"identifier": identifier}) == 0
        job_update = await session.execute(text("""UPDATE public.publication_jobs
            SET updated_at=updated_at WHERE id=:id"""), {"id": JOB_A})
        attempt_update = await session.execute(text("""UPDATE public.publication_attempts
            SET updated_at=updated_at WHERE id=:id"""), {"id": ATTEMPT_A})
        assert job_update.rowcount == 0 and attempt_update.rowcount == 0

    with pytest.raises(DBAPIError) as attempt_error:
        async with raw_security_context(
                runtime_factory, organization_id=ORG_A,
                execution_context=execution_context, user_id=user_id) as session:
            await session.execute(text("""INSERT INTO public.publication_attempts
                (id,publication_job_id,attempt_no,started_at,request_fingerprint,outcome)
                VALUES (:id,:job,99,:started,repeat('a',64),'in_progress')"""), {
                    "id": uuid4(), "job": JOB_A, "started": NOW,
                })
    assert attempt_error.value.orig.sqlstate == "42501"

    with pytest.raises(DBAPIError) as audit_error:
        async with raw_security_context(
                runtime_factory, organization_id=ORG_A,
                execution_context=execution_context, user_id=user_id) as session:
            await session.execute(text("""INSERT INTO public.audit_logs
                (organization_id,actor_type,actor_id,action,entity_type,
                 entity_id,correlation_id,after)
                VALUES (:org,'system',NULL,'publication.started',
                        'publication',:job,:correlation,'{}')"""), {
                    "org": ORG_A, "job": JOB_A, "correlation": uuid4(),
                })
    assert audit_error.value.orig.sqlstate == "42501"


async def test_system_cannot_update_foreign_job_or_content(runtime_factory):
    async with system_transaction(runtime_factory) as session:
        job = await session.execute(text("""UPDATE public.publication_jobs
            SET updated_at=updated_at WHERE id=:id"""), {"id": JOB_B})
        content = await session.execute(text("""UPDATE public.content_items
            SET updated_at=updated_at WHERE id=:id"""), {"id": CONTENT_B})
        assert job.rowcount == 0 and content.rowcount == 0


async def test_system_cannot_create_arbitrary_publication_job(runtime_factory):
    with pytest.raises(DBAPIError) as error:
        async with system_transaction(runtime_factory) as session:
            await session.execute(text("""INSERT INTO public.publication_jobs
                (id,content_item_id,content_version_id,idempotency_key,scheduled_for,
                 status,created_by)
                VALUES (:id,:content,:version,:key,:scheduled,'pending',:actor)"""), {
                    "id": uuid4(),
                    "content": UUID("96000000-0000-0000-0000-000000000005"),
                    "version": UUID("96100000-0000-0000-0000-000000000005"),
                    "key": uuid4(),
                    "scheduled": NOW,
                    "actor": USER_A,
                })
    assert error.value.orig.sqlstate == "42501"


@pytest.mark.parametrize("target", ["attempt", "audit"])
async def test_system_cannot_insert_foreign_execution_rows(runtime_factory, target):
    with pytest.raises(DBAPIError) as error:
        async with system_transaction(runtime_factory) as session:
            if target == "attempt":
                await session.execute(text("""INSERT INTO public.publication_attempts
                    (id,publication_job_id,attempt_no,started_at,request_fingerprint,outcome)
                    VALUES (:id,:job,99,:started,repeat('a',64),'in_progress')"""), {
                        "id": uuid4(), "job": JOB_B, "started": NOW,
                    })
            else:
                await session.execute(text("""INSERT INTO public.audit_logs
                    (organization_id,actor_type,actor_id,action,entity_type,
                     entity_id,correlation_id,after)
                    VALUES (:org,'system',NULL,'publication.started',
                            'publication',:job,:correlation,'{}')"""), {
                        "org": ORG_B, "job": JOB_B,
                        "correlation": uuid4(),
                    })
    assert error.value.orig.sqlstate == "42501"


@pytest.mark.parametrize(("table", "column", "table_select", "column_select"), [
    ("profiles", "id", True, True),
    ("organization_members", "organization_id", True, True),
    ("campaigns", "id", True, True),
    ("assets", "id", True, True),
    ("review_decisions", "id", False, False),
    ("ai_generation_requests", "id", True, True),
    ("ai_daily_usage", "organization_id", True, True),
    ("webhook_events", "id", False, False),
    ("whatsapp_conversations", "id", False, False),
    ("whatsapp_messages", "id", False, False),
    ("outbox_events", "id", False, False),
    ("automation_runs", "id", False, False),
    ("idempotency_keys", "key", True, True),
    # Prompt 10 grants only selected OAuth columns, not table-wide SELECT.
    ("facebook_oauth_states", "id", False, True),
])
async def test_system_has_no_rls_path_to_forbidden_surfaces(
        runtime_factory, table, column, table_select, column_select):
    table_name = "public." + table
    async with system_transaction(runtime_factory) as session:
        assert await session.scalar(text(
            "SELECT has_table_privilege(current_user,:table,'SELECT')"
        ), {"table": table_name}) is table_select
        assert await session.scalar(text(
            "SELECT has_column_privilege(current_user,:table,:column,'SELECT')"
        ), {"table": table_name, "column": column}) is column_select

    if column_select:
        async with system_transaction(runtime_factory) as session:
            assert await session.scalar(text(
                f"SELECT count({column}) FROM {table_name}"
            )) == 0
    else:
        with pytest.raises(DBAPIError) as error:
            async with system_transaction(runtime_factory) as session:
                await session.execute(text(f"SELECT {column} FROM {table_name}"))
        assert error.value.orig.sqlstate == "42501"


async def test_existing_human_rls_path_remains_membership_based(runtime_factory):
    async with runtime_factory() as session:
        async with session.begin():
            await verify_runtime_role(session, "nightclub_api")
            await establish_user_context(session, CurrentUser(USER_A))
            await establish_organization_context(
                session, OrganizationContext(ORG_A, USER_A, MemberRole.OWNER),
            )
            assert await session.scalar(text(
                "SELECT NULLIF(current_setting('app.execution_context',true),'')"
            )) is None
            assert await session.scalar(text(
                "SELECT count(*) FROM public.organization_members WHERE organization_id=:id"
            ), {"id": ORG_A}) > 0
            organizations = set((await session.execute(text("""SELECT id
                FROM public.organizations WHERE id IN (:own,:foreign)"""), {
                    "own": ORG_A, "foreign": ORG_B,
                })).scalars())
            content = set((await session.execute(text("""SELECT id
                FROM public.content_items WHERE id IN (:own,:foreign)"""), {
                    "own": CONTENT_A, "foreign": CONTENT_B,
                })).scalars())
            assert organizations == {ORG_A} and content == {CONTENT_A}


async def test_scheduler_remains_discovery_only(scheduler_factory):
    async with scheduler_factory() as session:
        async with session.begin():
            await set_scheduler_transaction_read_only(session)
            await verify_scheduler_role(session, "nightclub_scheduler")
            assert not await session.scalar(text(
                "SELECT has_table_privilege(current_user,'public.publication_jobs','UPDATE')"
            ))
            assert not await session.scalar(text(
                "SELECT has_any_column_privilege(current_user,'public.platform_connections','SELECT')"
            ))


class LocalProvider:
    def __init__(self):
        self.calls = []

    async def publish_post(self, **_values):
        self.calls.append("publish")
        return ProviderResult(
            ProviderDisposition.SUCCEEDED,
            external_post_id="synthetic-system-post",
            provider_request_id="synthetic-system-request",
            http_status=200,
        )

    async def reconcile_post(self, **_values):
        self.calls.append("reconcile")
        return ReconciliationResult(ReconciliationDisposition.NOT_FOUND, http_status=200)


def system_executor(runtime_factory, provider):
    return FacebookPublicationExecutor(
        SQLAlchemyPublicationExecutionStore(runtime_factory),
        provider,
        CredentialCipher(SYNTHETIC_KEY, 1),
        clock=lambda: NOW,
    )


async def test_revoked_creator_does_not_invalidate_existing_durable_job(
        runtime_factory):
    async with runtime_factory() as session:
        async with session.begin():
            await verify_runtime_role(session, "nightclub_api")
            await establish_user_context(session, CurrentUser(REMOVED_USER))
            assert await session.scalar(text("""SELECT count(*)
                FROM public.organization_members
                WHERE organization_id=:organization AND user_id=:user"""), {
                    "organization": ORG_A, "user": REMOVED_USER,
                }) == 0
    provider = LocalProvider()
    with publication_audit_inserts(runtime_factory) as audit:
        result = await system_executor(runtime_factory, provider).execute_system(
            ORG_A, EXECUTION_JOB,
        )
    assert result.status == ExecutionStatus.PUBLISHED
    assert provider.calls == ["publish"]
    async with system_transaction(runtime_factory) as session:
        job_status = await session.scalar(text(
            "SELECT status FROM public.publication_jobs WHERE id=:job"
        ), {"job": EXECUTION_JOB})
        creator = await session.scalar(text("""SELECT ci.created_by
            FROM public.content_items ci JOIN public.publication_jobs pj
              ON pj.content_item_id=ci.id WHERE pj.id=:job"""), {
                "job": EXECUTION_JOB,
            })
    assert creator == REMOVED_USER and job_status == "succeeded"
    assert [(row["actor_type"], row["actor_id"], row["action"]) for row in audit] == [
        ("system", None, "publication.started"),
        ("system", None, "publication.succeeded"),
    ]


async def test_inactive_organization_is_structural_failure_without_attempt_or_provider(
        runtime_factory):
    provider = LocalProvider()
    with publication_audit_inserts(runtime_factory) as audit:
        result = await system_executor(runtime_factory, provider).execute_system(
            ORG_INACTIVE, INACTIVE_JOB,
        )
    assert result.status == ExecutionStatus.PERMANENT_FAILURE
    assert result.error_code == "ORGANIZATION_INACTIVE" and provider.calls == []
    async with system_transaction(runtime_factory, ORG_INACTIVE) as session:
        job = (await session.execute(text("""SELECT status,attempt_count
            FROM public.publication_jobs WHERE id=:job"""), {
                "job": INACTIVE_JOB,
            })).one()
        content_status = await session.scalar(text("""SELECT ci.status
            FROM public.content_items ci JOIN public.publication_jobs pj
              ON pj.content_item_id=ci.id WHERE pj.id=:job"""), {
                "job": INACTIVE_JOB,
            })
        attempts = await session.scalar(text("""SELECT count(*)
            FROM public.publication_attempts WHERE publication_job_id=:job"""), {
                "job": INACTIVE_JOB,
            })
    assert job == ("permanent_failure", 0) and content_status == "failed"
    assert attempts == 0
    assert len(audit) == 1
    assert audit[0]["actor_type"] == "system" and audit[0]["actor_id"] is None
    assert audit[0]["action"] == "publication.permanent_failure"
    assert audit[0]["after"]["errorCode"] == "ORGANIZATION_INACTIVE"


async def test_explicit_cancellation_wins_without_lease_attempt_or_provider(
        runtime_factory):
    provider = LocalProvider()
    result = await system_executor(runtime_factory, provider).execute_system(
        ORG_A, CANCELLED_JOB,
    )
    assert result.status == ExecutionStatus.LEASE_UNAVAILABLE and provider.calls == []
    async with system_transaction(runtime_factory) as session:
        job = (await session.execute(text("""SELECT status,attempt_count,lease_token
            FROM public.publication_jobs WHERE id=:job"""), {
                "job": CANCELLED_JOB,
            })).one()
        attempts = await session.scalar(text("""SELECT count(*)
            FROM public.publication_attempts WHERE publication_job_id=:job"""), {
                "job": CANCELLED_JOB,
            })
    assert job == ("cancelled", 0, None) and attempts == 0
