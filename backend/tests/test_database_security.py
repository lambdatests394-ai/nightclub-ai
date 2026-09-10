"""Unit/application checks. Real transaction guarantees are tested separately."""
import asyncio
from contextlib import asynccontextmanager
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from backend.app.core import database, database_security as security
from backend.app.core.config import Settings
from backend.app.core.migration_config import migration_url
from backend.app.main import create_app
from backend.app.modules.identity import dependencies
from backend.app.modules.identity.errors import Forbidden, IdentityUnavailable
from backend.app.modules.identity.policy import CurrentUser, MemberRole, OrganizationContext
from backend.app.modules.identity.repository import Membership
from backend.app.modules.identity.service import IdentityService
from backend.app.modules.identity.models import Organization, Profile


def session_mock():
    session = AsyncMock()
    session.info = {}
    root = Mock(is_active=True)
    session.get_transaction = Mock(return_value=root)
    session.in_nested_transaction = Mock(return_value=False)
    result = Mock()
    result.one.return_value = (None, None)
    session.execute.return_value = result
    return session


@pytest.mark.anyio
async def test_context_is_parameterized_and_transaction_local():
    session = session_mock()
    user = CurrentUser(uuid4())
    context = OrganizationContext(uuid4(), user.user_id, MemberRole.OWNER)
    await security.establish_user_context(session, user)
    await security.establish_organization_context(session, context)
    calls = session.execute.await_args_list
    assert [call.args[1] for call in calls[1:]] == [
        {"value": str(user.user_id)}, {"value": ""}, {"value": str(context.organization_id)},
    ]
    for call in calls[1:]:
        sql = str(call.args[0])
        assert ":value" in sql and ", true)" in sql
        assert str(user.user_id) not in sql and str(context.organization_id) not in sql
        assert "SET ROLE" not in sql


@pytest.mark.anyio
@pytest.mark.parametrize("defect", ["no-root", "nested", "inactive", "raw-user", "malformed-user"])
async def test_user_context_rejects_untrusted_or_nonroot_input(defect):
    session = session_mock()
    user = CurrentUser(uuid4())
    if defect == "no-root": session.get_transaction.return_value = None
    if defect == "nested": session.in_nested_transaction.return_value = True
    if defect == "inactive": session.get_transaction.return_value.is_active = False
    if defect == "raw-user": user = {"user_id": str(uuid4())}
    if defect == "malformed-user": user = CurrentUser("not-a-uuid")
    with pytest.raises(IdentityUnavailable):
        await security.establish_user_context(session, user)
    session.execute.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("previous", [("prior-user", None), (None, "prior-org")])
async def test_session_contamination_invalidates_connection(previous):
    session = session_mock()
    session.execute.return_value.one.return_value = previous
    with pytest.raises(IdentityUnavailable):
        await security.establish_user_context(session, CurrentUser(uuid4()))
    session.invalidate.assert_awaited_once()
    assert session.execute.await_count == 1


@pytest.mark.anyio
@pytest.mark.parametrize("defect", ["missing-user", "wrong-user", "new-root", "raw-org", "malformed-org"])
async def test_organization_context_cannot_escape_verified_user_or_transaction(defect):
    session = session_mock()
    user = CurrentUser(uuid4())
    await security.establish_user_context(session, user)
    session.execute.reset_mock()
    context = OrganizationContext(uuid4(), user.user_id, MemberRole.VIEWER)
    if defect == "missing-user": session.info.clear()
    if defect == "wrong-user": context = OrganizationContext(uuid4(), uuid4(), MemberRole.OWNER)
    if defect == "new-root": session.get_transaction.return_value = Mock(is_active=True)
    if defect == "raw-org": context = {"organization_id": str(uuid4())}
    if defect == "malformed-org": context = OrganizationContext("not-a-uuid", user.user_id, MemberRole.VIEWER)
    with pytest.raises(IdentityUnavailable):
        await security.establish_organization_context(session, context)
    session.execute.assert_not_awaited()


def role_row():
    return dict(rolname="nightclub_api", rolcanlogin=True, rolinherit=False,
                rolsuper=False, rolcreatedb=False, rolcreaterole=False, rolreplication=False,
                rolbypassrls=False, direct_login=True, memberships=False, schema_create=False,
                table_count=20, owns_tables=False)


@pytest.mark.anyio
@pytest.mark.parametrize("defect", [None, "missing", "rolname", "rolcanlogin", "rolinherit", "rolsuper",
    "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls", "direct_login",
    "memberships", "schema_create", "table_count", "owns_tables"])
async def test_effective_role_guard(defect):
    row = role_row()
    if defect == "rolname": row[defect] = "unexpected"
    elif defect == "table_count": row[defect] = 19
    elif defect not in (None, "missing"): row[defect] = not row[defect]
    if defect == "missing": row = None
    session = session_mock()
    session.execute.return_value.mappings.return_value.one_or_none.return_value = row
    if defect:
        with pytest.raises(IdentityUnavailable):
            await security.verify_runtime_role(session, "nightclub_api")
    else:
        await security.verify_runtime_role(session, "nightclub_api")


@pytest.mark.anyio
@pytest.mark.parametrize("outcome", ["success", "exception", "cancel", "commit-error", "rollback-error", "commit-cancel"])
async def test_root_boundary_cleanup(monkeypatch, outcome):
    session = session_mock()
    session.info["security_context"] = "test-only"
    monkeypatch.setattr(database, "SessionFactory", lambda: session)
    if outcome == "commit-error": session.commit.side_effect = RuntimeError("synthetic")
    if outcome == "commit-cancel": session.commit.side_effect = asyncio.CancelledError()
    if outcome == "rollback-error": session.rollback.side_effect = RuntimeError("synthetic")

    async def run():
        async with asynccontextmanager(database.get_db_session)():
            if outcome == "exception": raise Forbidden()
            if outcome == "cancel": raise asyncio.CancelledError()

    expected = {"exception": Forbidden, "cancel": asyncio.CancelledError,
                "commit-error": RuntimeError, "rollback-error": RuntimeError, "commit-cancel": asyncio.CancelledError}
    if outcome in expected:
        with pytest.raises(expected[outcome]): await run()
    else:
        await run()
    session.begin.assert_awaited_once()
    session.close.assert_awaited_once()
    assert "security_context" not in session.info
    if outcome in {"cancel", "commit-cancel", "rollback-error"}: session.invalidate.assert_awaited_once()
    if outcome not in {"cancel", "commit-cancel"}: session.rollback.assert_awaited_once()


@pytest.mark.anyio
async def test_cancellation_during_cleanup_waits_for_close(monkeypatch):
    session = session_mock()
    entered, finish = asyncio.Event(), asyncio.Event()

    async def rollback():
        entered.set()
        await finish.wait()

    session.rollback.side_effect = rollback
    monkeypatch.setattr(database, "SessionFactory", lambda: session)

    async def run():
        async with asynccontextmanager(database.get_db_session)(): pass

    task = asyncio.create_task(run())
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError): await task
    session.close.assert_awaited_once()


@pytest.mark.anyio
async def test_dependency_installs_user_before_repository(monkeypatch):
    session = session_mock()
    events = []

    async def boundary():
        events.append("transaction")
        yield session

    async def role(*args): events.append("role")
    async def user(*args): events.append("user")
    monkeypatch.setattr(database, "SessionFactory", object())
    monkeypatch.setattr(database, "get_db_session", boundary)
    monkeypatch.setattr(dependencies, "verify_runtime_role", role)
    monkeypatch.setattr(dependencies, "establish_user_context", user)
    async with asynccontextmanager(dependencies.get_identity_repository)(CurrentUser(uuid4())):
        assert events == ["transaction", "role", "user"]


@pytest.mark.anyio
async def test_service_installs_only_validated_organization_context():
    user = CurrentUser(uuid4())
    org = Organization(id=uuid4(), is_active=True)
    repo = AsyncMock()
    repo.get_profile.return_value = Profile(id=user.user_id, is_active=True)
    repo.get_membership.return_value = Membership(user.user_id, org, "viewer")
    installer = AsyncMock()
    service = IdentityService(repo, organization_context_installer=installer)
    context, _ = await service.organization(user, org.id)
    installer.assert_awaited_once_with(context)
    installer.reset_mock()
    with pytest.raises(Forbidden): await service.organization(user, uuid4())
    installer.assert_not_awaited()
    repo.list_memberships.return_value = [Membership(user.user_id, org, "viewer")]
    await service.active_profile(user)
    await service.organizations(user, None, 50)
    installer.assert_not_awaited()


@pytest.mark.anyio
async def test_unsafe_runtime_identity_is_safe_http_503(monkeypatch, security_material):
    app = create_app(security_material.settings)
    app.dependency_overrides[dependencies.get_token_verifier] = lambda: security_material.verifier
    session = session_mock()
    monkeypatch.setattr(database, "SessionFactory", lambda: session)
    monkeypatch.setattr(dependencies, "verify_runtime_role", AsyncMock(side_effect=IdentityUnavailable()))
    setter = AsyncMock()
    monkeypatch.setattr(dependencies, "establish_user_context", setter)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/me", headers={"Authorization": "Bearer " + security_material.token()})
    assert response.status_code == 503 and response.headers["Retry-After"] == "30"
    assert response.json()["code"] == "IDENTITY_UNAVAILABLE"
    setter.assert_not_awaited()
    session.close.assert_awaited_once()


def test_migration_never_falls_back_to_runtime_url():
    with pytest.raises(RuntimeError, match="DATABASE_MIGRATION_URL is required"):
        migration_url(Settings(_env_file=None, database_url="postgresql://localhost/runtime",
                               database_migration_url=None))


@pytest.mark.parametrize("driver", ["postgres", "postgresql", "postgresql+asyncpg", "postgresql+psycopg"])
def test_explicit_migration_driver(driver):
    assert migration_url(Settings(_env_file=None, database_migration_url=f"{driver}://localhost/test")) == "postgresql+psycopg://localhost/test"


def test_migration_rejects_other_driver():
    with pytest.raises(RuntimeError):
        migration_url(Settings(_env_file=None, database_migration_url="sqlite:///test"))


def load_migration():
    path = Path(__file__).parents[1] / "migrations/versions/20260909_0003_identity_rls.py"
    spec = spec_from_file_location("identity_rls_test", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_scope_and_downgrade_refusal():
    migration = load_migration()
    assert migration.down_revision == "20260907_0002"
    assert set(migration.TABLES) == set(security.PROTECTED_TABLES)
    assert set(migration.POLICIES) == set(security.BOOTSTRAP_TABLES)
    assert "organizations" not in migration.POLICIES["organization_members"]
    assert all("app.organization_id" not in p for p in migration.POLICIES.values())
    with pytest.raises(RuntimeError, match="compensating"):
        migration.downgrade()


@pytest.mark.parametrize("defect", ["offline", "absent-role", "rolcanlogin", "rolsuper", "rolbypassrls",
                                   "rolinherit", "rolcreatedb", "rolcreaterole", "rolreplication", "membership"])
def test_migration_unsafe_preflight_has_no_ddl(monkeypatch, defect):
    migration = load_migration()
    row = role_row()
    row["oid"] = 123
    if defect.startswith("rol"): row[defect] = not row[defect]
    if defect == "absent-role": row = None
    bind = Mock()
    bind.execute.return_value.mappings.return_value.one_or_none.return_value = row
    bind.scalar.return_value = defect == "membership"
    execute = Mock()
    monkeypatch.setattr(migration.context, "is_offline_mode", lambda: defect == "offline")
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(migration.op, "execute", execute)
    with pytest.raises(RuntimeError): migration.upgrade()
    execute.assert_not_called()


def test_migration_ddl_plan_is_identity_only(monkeypatch):
    migration = load_migration()
    bind = Mock()
    role = {**role_row(), "oid": 123}
    result_role, result_tables = Mock(), Mock()
    result_role.mappings.return_value.one_or_none.return_value = role
    result_tables.mappings.return_value.all.return_value = [
        {"relname": table, "relowner": 456, "oid": index} for index, table in enumerate(migration.TABLES)
    ]
    bind.execute.side_effect = [result_role, result_tables]
    bind.dialect.identifier_preparer.quote.side_effect = lambda name: name

    def scalar(statement, values):
        if "has_table_privilege" in str(statement) or "has_any_column_privilege" in str(statement):
            return values["table"].split(".")[1] in migration.POLICIES and values["privilege"] == "SELECT"
        return False

    bind.scalar.side_effect = scalar
    execute = Mock()
    monkeypatch.setattr(migration.context, "is_offline_mode", lambda: False)
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(migration.op, "execute", execute)
    migration.upgrade()
    statements = [call.args[0] for call in execute.call_args_list]
    assert sum(" ENABLE ROW LEVEL SECURITY" in s for s in statements) == 20
    assert sum(" FORCE ROW LEVEL SECURITY" in s for s in statements) == 20
    assert sum(s.startswith("CREATE POLICY") for s in statements) == 3
    assert sum(s.startswith("GRANT SELECT ON TABLE") for s in statements) == 3
    assert not any("CREATE ROLE" in s or "SECURITY DEFINER" in s or "USING (true)" in s for s in statements)
    assert "REVOKE CREATE ON SCHEMA public FROM nightclub_api" in statements
    assert not any("SCHEMA public FROM PUBLIC" in s for s in statements)


def test_harness_refuses_unconfigured_url_without_exposing_value(monkeypatch):
    path = Path(__file__).parents[2] / "scripts/local-dev/validate_prompt5.py"
    spec = spec_from_file_location("rls_harness_test", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("PROMPT5_RUNTIME_URL", "invalid-synthetic-input")
    with pytest.raises(RuntimeError, match="value withheld") as error:
        module.local_url("PROMPT5_RUNTIME_URL", "nightclub_api")
    assert "invalid-synthetic-input" not in str(error.value)


@pytest.mark.parametrize("validation_fails", [False, True])
def test_harness_retains_runtime_role_message_and_validation_flow(monkeypatch, capsys, validation_fails):
    """Execute the harness with fully mocked I/O, including its finally branch.

    This is a message/flow regression, not a new PostgreSQL validation. Neither
    psycopg connections nor subprocess commands are actually executed.
    """
    path = Path(__file__).parents[2] / "scripts/local-dev/validate_prompt5.py"
    spec = spec_from_file_location("rls_harness_message_test", path)
    harness = module_from_spec(spec)
    spec.loader.exec_module(harness)
    connection = MagicMock()
    connection.__enter__.return_value = connection

    def execute(statement, parameters=None):
        result = MagicMock()
        result.__iter__.return_value = iter(())
        result.fetchall.return_value = []
        result.fetchone.return_value = None
        if "FROM pg_roles" in statement:
            if parameters == ("alembic_test_user",):
                result.fetchone.return_value = ("alembic_test_user", False, False, True, True, False, False, True)
            else:
                result.fetchone.return_value = ("nightclub_api", False, False, False, True, False, False, False)
        elif statement == "SELECT current_user":
            result.fetchone.return_value = ("nightclub_api",)
        elif "SELECT count(*)" in statement:
            result.fetchone.return_value = (0,)
        elif "SELECT version_num" in statement:
            result.fetchone.return_value = (harness.REVISION,)
        return result

    def subprocess_run(args, **kwargs):
        if "downgrade" in args:
            return SimpleNamespace(returncode=1, stdout="", stderr=
                "Unsafe security downgrade blocked; a reviewed compensating migration is required")
        failed = validation_fails and "pytest" in args
        return SimpleNamespace(returncode=int(failed), stdout="", stderr="")

    connection.execute.side_effect = execute
    monkeypatch.setattr(harness, "local_url", lambda *args: SimpleNamespace(host="127.0.0.1"))
    monkeypatch.setattr(harness, "connection_info", lambda *args: "mocked-local-connection")
    connect = Mock(return_value=connection)
    commands = Mock(side_effect=subprocess_run)
    monkeypatch.setattr(harness.psycopg, "connect", connect)
    monkeypatch.setattr(harness.subprocess, "run", commands)
    monkeypatch.setattr(harness, "seed", Mock())
    if validation_fails:
        with pytest.raises(RuntimeError, match="Validation command failed"):
            harness.run()
    else:
        harness.run()

    output = capsys.readouterr().out
    assert "LOCAL_RUNTIME_ROLE_RETAINED: nightclub_api must NOT be dropped automatically or as part of Prompt 5 validation." in output
    assert "ADMIN CLEANUP REQUIRED" not in output and "DROP ROLE" not in output
    assert "TEST_DATABASE_REMAINING: []" in output
    statements = [call.args[0] for call in connection.execute.call_args_list]
    assert statements.count("CREATE DATABASE nightclub_ai_prompt5_test OWNER alembic_test_user") == 1
    assert statements.count("DROP DATABASE nightclub_ai_prompt5_test") == 1
    assert not any(operation in sql.upper() for sql in statements
                   for operation in ("CREATE ROLE", "ALTER ROLE", "DROP ROLE"))
    executed = [call.args[0][1:] for call in commands.call_args_list]
    assert executed[:3] == [
        ["-m", "alembic", "upgrade", "20260907_0002"],
        ["-m", "alembic", "upgrade", "20260909_0003"],
        ["-m", "pytest", "backend/tests", "--prompt5-postgres", "-q", "-ra"],
    ]
    if validation_fails:
        assert len(executed) == 3
    else:
        assert executed[3] == ["-m", "alembic", "downgrade", "20260907_0002"]
        assert executed[4] == ["-m", "pytest", "backend/tests/test_rls_postgres.py", "--prompt5-postgres", "-q", "-k", "catalog"]
