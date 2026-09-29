"""Opt-in real PostgreSQL checks for the scheduler discovery boundary."""

from contextlib import asynccontextmanager
import os
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core.scheduler_database import (
    set_scheduler_transaction_read_only,
    verify_scheduler_role,
)


pytestmark = pytest.mark.anyio
DATABASE = "nightclub_ai_prompt11_scheduler_test"
ORG_A = UUID("20000000-0000-0000-0000-000000000001")
ORG_B = UUID("20000000-0000-0000-0000-000000000002")

CASES = {
    "pending_due": (1, ORG_A, True),
    "pending_future": (2, ORG_A, False),
    "retry_due": (3, ORG_A, True),
    "retry_future": (4, ORG_A, False),
    "retry_null": (5, ORG_A, False),
    "leased_expired": (6, ORG_A, True),
    "leased_active": (7, ORG_A, False),
    "leased_null": (8, ORG_A, False),
    "publishing_expired": (9, ORG_B, True),
    "publishing_active": (10, ORG_B, False),
    "publishing_null": (11, ORG_B, False),
    "succeeded": (12, ORG_B, False),
    "permanent_failure": (13, ORG_B, False),
    "cancelled": (14, ORG_B, False),
}


def fixture_id(prefix: int, suffix: int) -> UUID:
    return UUID(f"{prefix:08d}-0000-0000-0000-{suffix:012d}")


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


@pytest.fixture
async def scheduler_factory(request):
    if not request.config.getoption("--prompt11-scheduler-postgres"):
        pytest.skip("Prompt 11 scheduler PostgreSQL opt-in is not enabled")
    url = guarded_url("PROMPT11_SCHEDULER_URL", "nightclub_scheduler")
    engine = create_async_engine(
        url.set(drivername="postgresql+asyncpg"), pool_size=1, max_overflow=0,
        pool_pre_ping=True, hide_parameters=True,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@asynccontextmanager
async def scheduler_transaction(factory, *, read_only=False):
    async with factory() as session:
        async with session.begin():
            if read_only:
                await set_scheduler_transaction_read_only(session)
            yield session


async def test_direct_login_and_b1_role_verifier(scheduler_factory) -> None:
    async with scheduler_transaction(scheduler_factory, read_only=True) as session:
        assert (await session.execute(text(
            "SELECT current_user,session_user,current_setting('transaction_read_only')"
        ))).one() == ("nightclub_scheduler", "nightclub_scheduler", "on")
        await verify_scheduler_role(session, "nightclub_scheduler")


async def test_due_visibility_matrix_and_multiple_organizations(scheduler_factory) -> None:
    async with scheduler_transaction(scheduler_factory, read_only=True) as session:
        rows = (await session.execute(text("""SELECT pj.id,ci.organization_id
            FROM public.publication_jobs pj
            JOIN public.content_items ci ON ci.id=pj.content_item_id
            ORDER BY pj.id"""))).all()
    expected = {
        (fixture_id(91000000, suffix), organization)
        for suffix, organization, visible in CASES.values() if visible
    }
    assert set(rows) == expected
    assert {organization for _, organization in rows} == {ORG_A, ORG_B}


async def test_content_visibility_is_limited_to_due_jobs(scheduler_factory) -> None:
    async with scheduler_transaction(scheduler_factory, read_only=True) as session:
        rows = (await session.execute(text(
            "SELECT id,organization_id FROM public.content_items ORDER BY id"
        ))).all()
    expected = {
        (fixture_id(92000000, suffix), organization)
        for suffix, organization, visible in CASES.values() if visible
    }
    assert set(rows) == expected


@pytest.mark.parametrize("statement", [
    "SELECT * FROM public.publication_jobs",
    "SELECT created_by FROM public.publication_jobs",
    "SELECT credentials_ciphertext FROM public.platform_connections",
    "SELECT body FROM public.content_versions",
    "SELECT * FROM public.audit_logs",
    "SELECT * FROM public.ai_generation_requests",
    "SELECT * FROM public.whatsapp_messages",
    "SELECT * FROM public.assets",
])
async def test_forbidden_information_is_not_readable(scheduler_factory, statement) -> None:
    with pytest.raises(DBAPIError) as error:
        async with scheduler_transaction(scheduler_factory, read_only=True) as session:
            await session.execute(text(statement))
    assert error.value.orig.sqlstate == "42501"


async def test_scheduler_has_only_exact_column_privileges(scheduler_factory) -> None:
    expected = {
        "publication_jobs": {
            "id", "content_item_id", "status", "scheduled_for",
            "next_attempt_at", "lease_expires_at",
        },
        "content_items": {"id", "organization_id"},
    }
    async with scheduler_transaction(scheduler_factory, read_only=True) as session:
        assert await session.scalar(text(
            "SELECT has_schema_privilege(current_user,'public','USAGE')"
        ))
        assert not await session.scalar(text(
            "SELECT has_schema_privilege(current_user,'public','CREATE')"
        ))
        for table, columns in expected.items():
            assert not await session.scalar(text(
                "SELECT has_table_privilege(current_user,:table,'SELECT')"
            ), {"table": "public." + table})
            actual = set((await session.execute(text("""SELECT column_name
                FROM information_schema.column_privileges
                WHERE table_schema='public' AND table_name=:table
                  AND grantee=current_user AND privilege_type='SELECT'"""), {
                    "table": table,
                })).scalars())
            assert actual == columns


async def test_read_only_scheduler_transaction_rejects_write(scheduler_factory) -> None:
    with pytest.raises(DBAPIError) as error:
        async with scheduler_transaction(scheduler_factory, read_only=True) as session:
            await session.execute(text(
                "UPDATE public.publication_jobs SET updated_at=updated_at WHERE false"
            ))
    assert error.value.orig.sqlstate in {"25006", "42501"}
