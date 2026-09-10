"""Opt-in, real RLS tests. No admin/bypass/owner identity for runtime assertions."""
import asyncio
import os
from contextlib import asynccontextmanager
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core import database
from backend.app.core.database_security import (
    BOOTSTRAP_TABLES, PROTECTED_TABLES, establish_organization_context,
    establish_user_context, verify_runtime_role,
)
from backend.app.main import create_app
from backend.app.modules.identity.dependencies import get_token_verifier
from backend.app.modules.identity.errors import IdentityUnavailable
from backend.app.modules.identity.policy import CurrentUser, MemberRole, OrganizationContext

pytestmark = pytest.mark.anyio
A = UUID("10000000-0000-0000-0000-000000000001")
B = UUID("10000000-0000-0000-0000-000000000002")
INACTIVE = UUID("10000000-0000-0000-0000-000000000003")
REMOVED = UUID("10000000-0000-0000-0000-000000000004")
ORG_A = UUID("20000000-0000-0000-0000-000000000001")
ORG_B = UUID("20000000-0000-0000-0000-000000000002")
ORG_INACTIVE = UUID("20000000-0000-0000-0000-000000000003")
ORG_A2 = UUID("20000000-0000-0000-0000-000000000004")


@pytest.fixture
async def runtime(request, monkeypatch):
    if not request.config.getoption("--prompt5-postgres"):
        pytest.skip("Opt-in real RLS validation requires administrator-provisioned disposable local role")
    try:
        url = make_url(os.environ.get("PROMPT5_RUNTIME_URL", ""))
    except Exception:
        pytest.fail("Explicit disposable runtime URL required (value withheld)", pytrace=False)
    if (url.host not in {"localhost", "127.0.0.1"} or url.database != "nightclub_ai_prompt5_test"
            or url.username != "nightclub_api" or url.port != 5432 or url.query or not url.password):
        pytest.fail("Refusing non-local/non-disposable runtime target (value withheld)", pytrace=False)
    engine = create_async_engine(url.set(drivername="postgresql+asyncpg"), pool_size=1, max_overflow=0,
                                 pool_pre_ping=True, hide_parameters=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionFactory", factory)
    try:
        async with factory() as session:
            await verify_runtime_role(session, "nightclub_api")
        yield factory
    finally:
        await engine.dispose()


async def ids(session, table, column="id"):
    assert table in BOOTSTRAP_TABLES and column in {"id", "user_id"}
    # Intentionally UNSCOPED. Identifier allowlist only, no user WHERE filter.
    return set((await session.execute(text(f"SELECT {column} FROM public.{table}"))).scalars())


async def assert_empty_context(session):
    row = (await session.execute(text("SELECT NULLIF(current_setting('app.user_id', true), ''), NULLIF(current_setting('app.organization_id', true), '')"))).one()
    assert row == (None, None)
    assert await ids(session, "profiles") == set()


async def test_catalog_rls_force_policies_grants_and_ownership(runtime):
    async with runtime() as session:
        rows = (await session.execute(text("""
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, pg_get_userbyid(c.relowner)
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind = 'r'
        """))).all()
        canonical = [row for row in rows if row[0] != "alembic_version"]
        assert {r[0] for r in canonical} == set(PROTECTED_TABLES)
        assert all(r[1] and r[2] and r[3] != "nightclub_api" for r in canonical)
        policies = (await session.execute(text("SELECT tablename, roles, cmd, permissive, qual, with_check FROM pg_policies WHERE schemaname = 'public'"))).all()
        assert len(policies) == 3 and {p[0] for p in policies} == set(BOOTSTRAP_TABLES)
        assert all(list(p[1]) == ["nightclub_api"] and p[2] == "SELECT" and p[5] is None for p in policies)
        assert all(p[4] not in {"true", "(true)"} for p in policies)
        for table in PROTECTED_TABLES:
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
                allowed = await session.scalar(text("SELECT has_table_privilege(current_user, :table, :privilege)"),
                                               {"table": "public." + table, "privilege": privilege})
                assert allowed == (table in BOOTSTRAP_TABLES and privilege == "SELECT")
        assert not await session.scalar(text("SELECT has_schema_privilege(current_user, 'public', 'CREATE')"))
        assert not await session.scalar(text("SELECT has_sequence_privilege(current_user, 'public.audit_logs_id_seq', 'USAGE')"))
        # PUBLIC column/table ACLs must not silently reopen access. Local absence
        # of Supabase roles is expected; the migration never creates them.
        assert not await session.scalar(text("""
            SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace,
                LATERAL aclexplode(c.relacl) a
                WHERE n.nspname='public' AND c.relname=ANY(CAST(:tables AS text[])) AND a.grantee=0)
        """), {"tables": list(PROTECTED_TABLES)})
        for principal in ("anon", "authenticated", "service_role"):
            oid = await session.scalar(text("SELECT oid FROM pg_roles WHERE rolname=:role"), {"role": principal})
            if oid is not None:
                assert not await session.scalar(text("""
                    SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace,
                        LATERAL aclexplode(c.relacl) a
                        WHERE n.nspname='public' AND c.relname=ANY(CAST(:tables AS text[]))
                          AND a.grantee=:oid)
                """), {"tables": list(PROTECTED_TABLES), "oid": oid})


@pytest.mark.parametrize("user,organizations", [(A, {ORG_A, ORG_A2}), (B, {ORG_B}),
    (INACTIVE, set()), (REMOVED, set())])
async def test_unscoped_reads_only_permitted_identity(runtime, user, organizations):
    async with asynccontextmanager(database.get_db_session)() as session:
        await establish_user_context(session, CurrentUser(user))
        assert await ids(session, "profiles") == (set() if user == INACTIVE else {user})
        members = await ids(session, "organization_members", "user_id")
        assert members == (set() if user in {INACTIVE, REMOVED} else {user})
        assert await ids(session, "organizations") == organizations


async def test_missing_context_default_denies_bootstrap(runtime):
    async with runtime() as session:
        await assert_empty_context(session)
        assert await ids(session, "organization_members", "user_id") == set()
        assert await ids(session, "organizations") == set()


async def test_malformed_user_fails_closed(runtime):
    async with runtime() as session:
        await session.begin()
        await session.execute(text("SELECT set_config('app.user_id', :value, true)"), {"value": "invalid-uuid"})
        with pytest.raises(DBAPIError): await ids(session, "profiles")
        await session.rollback()
        await assert_empty_context(session)


@pytest.mark.parametrize("value", [None, "malformed", str(ORG_B)])
async def test_optional_org_context_never_broadens_bootstrap(runtime, value):
    async with asynccontextmanager(database.get_db_session)() as session:
        await establish_user_context(session, CurrentUser(A))
        if value is not None:
            # Adversarial direct SQL, NOT the application setter.
            await session.execute(text("SELECT set_config('app.organization_id', :value, true)"), {"value": value})
        assert await ids(session, "organizations") == {ORG_A, ORG_A2}


@pytest.mark.parametrize("table", [t for t in PROTECTED_TABLES if t not in BOOTSTRAP_TABLES])
async def test_other_tables_have_no_runtime_access(runtime, table):
    async with runtime() as session:
        await session.begin()
        await establish_user_context(session, CurrentUser(A))
        with pytest.raises(DBAPIError) as error:
            await session.execute(text(f"SELECT * FROM public.{table}"))
        assert error.value.orig.sqlstate == "42501"  # GRANT denial, not claimed as predicate proof.


@pytest.mark.parametrize("table", BOOTSTRAP_TABLES)
@pytest.mark.parametrize("operation", ["INSERT", "UPDATE", "DELETE"])
async def test_identity_writes_denied(runtime, table, operation):
    statements = {
        "INSERT": f"INSERT INTO public.{table} DEFAULT VALUES",
        "UPDATE": f"UPDATE public.{table} SET updated_at = now()",
        "DELETE": f"DELETE FROM public.{table}",
    }
    async with runtime() as session:
        await session.begin()
        await establish_user_context(session, CurrentUser(A))
        with pytest.raises(DBAPIError) as error: await session.execute(text(statements[operation]))
        assert error.value.orig.sqlstate == "42501"


@pytest.mark.parametrize("ending", ["commit", "rollback", "failed"])
async def test_root_cleanup_and_same_connection_reuse(runtime, ending):
    async with runtime() as session:
        await session.begin()
        await establish_user_context(session, CurrentUser(A))
        await establish_organization_context(session, OrganizationContext(ORG_A, A, MemberRole.VIEWER))
        pid = await session.scalar(text("SELECT pg_backend_pid()"))
        if ending == "failed":
            with pytest.raises(DBAPIError): await session.execute(text("SELECT 1/0"))
        if ending == "commit": await session.commit()
        else: await session.rollback()
    async with runtime() as session:
        await session.begin()
        assert await session.scalar(text("SELECT pg_backend_pid()")) == pid
        await assert_empty_context(session)
        await establish_user_context(session, CurrentUser(B))
        assert await ids(session, "profiles") == {B}
        assert await ids(session, "organizations") == {ORG_B}


async def test_intermediate_commit_cannot_reuse_authorization(runtime):
    async with runtime() as session:
        await session.begin()
        await establish_user_context(session, CurrentUser(A))
        await session.commit()
        assert await ids(session, "profiles") == set()
        with pytest.raises(IdentityUnavailable):
            await establish_organization_context(session, OrganizationContext(ORG_A, A, MemberRole.VIEWER))


async def test_savepoint_rollback_does_not_replace_root_cleanup(runtime):
    async with runtime() as session:
        await session.begin()
        await establish_user_context(session, CurrentUser(A))
        nested = await session.begin_nested()
        await session.execute(text("SELECT set_config('app.organization_id', :value, true)"), {"value": str(ORG_A)})
        await nested.rollback()
        assert await session.scalar(text("SELECT NULLIF(current_setting('app.organization_id', true), '')")) is None
        assert await ids(session, "profiles") == {A}
        await session.rollback()
        await assert_empty_context(session)


async def test_cancelled_query_does_not_leak_context(runtime):
    started = asyncio.Event()

    async def request():
        async with asynccontextmanager(database.get_db_session)() as session:
            await establish_user_context(session, CurrentUser(A))
            started.set()
            await session.execute(text("SELECT pg_sleep(30)"))

    task = asyncio.create_task(request())
    await started.wait()
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(task, timeout=5)
    async with runtime() as session:
        await assert_empty_context(session)


async def test_http_real_runtime_role_and_user_only_bootstrap(runtime, security_material):
    app = create_app(security_material.settings)
    app.dependency_overrides[get_token_verifier] = lambda: security_material.verifier
    headers = {"Authorization": "Bearer " + security_material.token()}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for path in ("/api/v1/me", "/api/v1/me/organizations", f"/api/v1/organizations/{ORG_A}"):
            response = await client.get(path, headers=headers)
            assert response.status_code == 200 and "Retry-After" not in response.headers
        listed = await client.get("/api/v1/me/organizations", headers=headers)
        assert {row["id"] for row in listed.json()["data"]} == {str(ORG_A), str(ORG_A2)}
        denied = await client.get(f"/api/v1/organizations/{ORG_B}", headers=headers)
        assert denied.status_code == 403 and "Retry-After" not in denied.headers
        denied = await client.get(f"/api/v1/organizations/{ORG_A}", headers={**headers, "X-Organization-Id": str(ORG_B)})
        assert denied.status_code == 403
        unauthenticated = await client.get("/api/v1/me")
        assert unauthenticated.status_code == 401 and "Retry-After" not in unauthenticated.headers
