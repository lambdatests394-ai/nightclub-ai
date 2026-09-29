"""Isolated, read-only database boundary for automation job discovery."""

import asyncio
from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from backend.app.core.config import get_settings
from backend.app.core.database import _async_database_url, create_database_engine
from backend.app.core.database_security import PROTECTED_TABLES


class SchedulerDatabaseUnavailable(RuntimeError):
    """The scheduler database boundary is absent or fails its security posture."""


SCHEDULER_ROLE_CHECK = text("""
SELECT r.rolname, r.rolcanlogin, r.rolinherit, r.rolsuper, r.rolcreatedb,
       r.rolcreaterole, r.rolreplication, r.rolbypassrls,
       current_user = session_user AS direct_login,
       EXISTS (SELECT 1 FROM pg_auth_members m WHERE m.member = r.oid) AS memberships,
       EXISTS (
           SELECT 1 FROM pg_namespace n
           WHERE n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
             AND n.nspname <> 'information_schema'
             AND has_schema_privilege(r.oid, n.oid, 'CREATE')
       ) AS schema_create,
       EXISTS (
           SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
             AND c.relname = ANY(CAST(:tables AS text[])) AND c.relowner = r.oid
       ) AS owns_tables
FROM pg_roles r WHERE r.rolname = current_user
""")


def create_scheduler_engine(database_url: str) -> AsyncEngine:
    """Create an engine that is never shared with the business runtime."""

    return create_database_engine(_async_database_url(database_url))


def create_scheduler_session_factory(
    scheduler_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(scheduler_engine, expire_on_commit=False, class_=AsyncSession)


_scheduler_url = get_settings().database_scheduler_url.get_secret_value()
SchedulerEngine: AsyncEngine | None = create_scheduler_engine(_scheduler_url) if _scheduler_url else None
SchedulerSessionFactory: async_sessionmaker[AsyncSession] | None = (
    create_scheduler_session_factory(SchedulerEngine) if SchedulerEngine else None
)


async def verify_scheduler_role(session: AsyncSession, expected_role: str) -> None:
    """Verify the connected catalog role; never trust only the URL username."""

    row = (await session.execute(
        SCHEDULER_ROLE_CHECK, {"tables": list(PROTECTED_TABLES)},
    )).mappings().one_or_none()
    if (row is None or row["rolname"] != expected_role or not row["rolcanlogin"]
            or not row["direct_login"] or any(row[key] for key in (
                "rolinherit", "rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication",
                "rolbypassrls", "memberships", "schema_create", "owns_tables",
            ))):
        raise SchedulerDatabaseUnavailable("Scheduler database unavailable")


async def set_scheduler_transaction_read_only(session: AsyncSession) -> None:
    """Set transaction-local read-only intent before discovery performs any query."""

    transaction = session.get_transaction()
    if transaction is None or not transaction.is_active or session.in_nested_transaction():
        raise SchedulerDatabaseUnavailable("Scheduler database unavailable")
    await session.execute(text("SET TRANSACTION READ ONLY"))


async def get_scheduler_session() -> AsyncIterator[AsyncSession]:
    """Yield an isolated read-only transaction with cancellation-safe cleanup."""

    if SchedulerSessionFactory is None:
        raise SchedulerDatabaseUnavailable("Scheduler database unavailable")
    session = SchedulerSessionFactory()
    cancelled = False
    try:
        await session.begin()
        await set_scheduler_transaction_read_only(session)
        yield session
        await session.commit()
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        async def cleanup() -> None:
            try:
                if cancelled:
                    await session.invalidate()
                else:
                    try:
                        await session.rollback()
                    except BaseException:
                        await session.invalidate()
                        raise
            finally:
                await session.close()

        task = asyncio.create_task(cleanup())
        interrupted = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                interrupted = True
        task.result()
        if interrupted:
            raise asyncio.CancelledError()


async def dispose_scheduler_engine() -> None:
    """Close the dedicated pool without touching the application runtime pool."""

    if SchedulerEngine is not None:
        await SchedulerEngine.dispose()
