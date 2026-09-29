import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest

from backend.app.core import database, scheduler_database as scheduler


def session_mock():
    session = AsyncMock()
    root = Mock(is_active=True)
    session.get_transaction = Mock(return_value=root)
    session.in_nested_transaction = Mock(return_value=False)
    session.execute.return_value = Mock()
    return session


def safe_role_row():
    return dict(
        rolname="nightclub_scheduler", rolcanlogin=True, rolinherit=False, rolsuper=False,
        rolcreatedb=False, rolcreaterole=False, rolreplication=False, rolbypassrls=False,
        direct_login=True, memberships=False, schema_create=False, owns_tables=False,
    )


def test_scheduler_url_conversion_does_not_mutate_runtime_factory(monkeypatch) -> None:
    runtime_factory = database.SessionFactory
    scheduler_engine = Mock()
    factory = scheduler.create_scheduler_session_factory(scheduler_engine)
    assert factory is not runtime_factory
    assert database.SessionFactory is runtime_factory


def test_scheduler_engine_uses_postgresql_async_url(monkeypatch) -> None:
    creator = Mock(return_value=Mock())
    monkeypatch.setattr(scheduler, "create_database_engine", creator)
    scheduler.create_scheduler_engine("postgresql://scheduler@localhost/db")
    creator.assert_called_once_with("postgresql+asyncpg://scheduler@localhost/db")


@pytest.mark.anyio
async def test_missing_scheduler_url_fails_only_when_boundary_is_used(monkeypatch) -> None:
    monkeypatch.setattr(scheduler, "SchedulerSessionFactory", None)
    with pytest.raises(scheduler.SchedulerDatabaseUnavailable):
        async with asynccontextmanager(scheduler.get_scheduler_session)():
            pass


@pytest.mark.anyio
@pytest.mark.parametrize("defect", [
    None, "missing", "rolname", "rolcanlogin", "rolinherit", "rolsuper", "rolcreatedb",
    "rolcreaterole", "rolreplication", "rolbypassrls", "direct_login", "memberships",
    "schema_create", "owns_tables",
])
async def test_scheduler_role_posture_is_exact(defect: str | None) -> None:
    row = safe_role_row()
    if defect == "rolname":
        row[defect] = "wrong_role"
    elif defect not in (None, "missing"):
        row[defect] = not row[defect]
    session = session_mock()
    session.execute.return_value.mappings.return_value.one_or_none.return_value = (
        None if defect == "missing" else row
    )
    if defect:
        with pytest.raises(scheduler.SchedulerDatabaseUnavailable):
            await scheduler.verify_scheduler_role(session, "nightclub_scheduler")
    else:
        await scheduler.verify_scheduler_role(session, "nightclub_scheduler")
        params = session.execute.await_args.args[1]
        assert params["tables"] == list(scheduler.PROTECTED_TABLES)


@pytest.mark.anyio
@pytest.mark.parametrize("defect", ["no-root", "inactive", "nested"])
async def test_read_only_requires_active_root_transaction(defect: str) -> None:
    session = session_mock()
    if defect == "no-root":
        session.get_transaction.return_value = None
    if defect == "inactive":
        session.get_transaction.return_value.is_active = False
    if defect == "nested":
        session.in_nested_transaction.return_value = True
    with pytest.raises(scheduler.SchedulerDatabaseUnavailable):
        await scheduler.set_scheduler_transaction_read_only(session)
    session.execute.assert_not_awaited()


@pytest.mark.anyio
async def test_scheduler_boundary_sets_transaction_read_only_and_cleans_up(monkeypatch) -> None:
    session = session_mock()
    monkeypatch.setattr(scheduler, "SchedulerSessionFactory", lambda: session)
    async with asynccontextmanager(scheduler.get_scheduler_session)() as yielded:
        assert yielded is session
    session.begin.assert_awaited_once()
    assert str(session.execute.await_args.args[0]) == "SET TRANSACTION READ ONLY"
    session.commit.assert_awaited_once()
    session.rollback.assert_awaited_once()
    session.close.assert_awaited_once()


@pytest.mark.anyio
async def test_scheduler_cancellation_invalidates_connection(monkeypatch) -> None:
    session = session_mock()
    monkeypatch.setattr(scheduler, "SchedulerSessionFactory", lambda: session)

    async def run():
        async with asynccontextmanager(scheduler.get_scheduler_session)():
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await run()
    session.invalidate.assert_awaited_once()
    session.close.assert_awaited_once()
    session.rollback.assert_not_awaited()


@pytest.mark.anyio
async def test_scheduler_exception_rolls_back_before_pool_return(monkeypatch) -> None:
    session = session_mock()
    monkeypatch.setattr(scheduler, "SchedulerSessionFactory", lambda: session)

    async def run():
        async with asynccontextmanager(scheduler.get_scheduler_session)():
            raise RuntimeError("synthetic")

    with pytest.raises(RuntimeError, match="synthetic"):
        await run()
    session.rollback.assert_awaited_once()
    session.close.assert_awaited_once()
