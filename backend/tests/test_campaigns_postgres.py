"""Independent, opt-in Prompt 6 runtime tests. No bypass or audit SELECT grant.

Audit INSERT counting uses SQLAlchemy execution events, not privileged reads.
Successful transaction completion plus a single INSERT is the audit evidence.
No test policy, trigger, role, or production grant is introduced by these tests.
"""
import asyncio
import os
from contextlib import asynccontextmanager
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core import database
from backend.app.core.database_security import PROTECTED_TABLES, establish_user_context, establish_organization_context, verify_runtime_role
from backend.app.main import create_app
from backend.app.modules.audit.writer import CampaignAuditWriter
from backend.app.modules.campaigns.models import Campaign
from backend.app.modules.campaigns.errors import CampaignConflict, IdempotencyConflict
from backend.app.modules.campaigns.repository import CampaignRepository
from backend.app.modules.campaigns.schemas import CampaignCreate
from backend.app.modules.campaigns.service import CampaignService
from backend.app.modules.identity.dependencies import get_current_user
from backend.app.modules.identity.policy import CurrentUser, MemberRole, OrganizationContext
from backend.app.shared.idempotency import IdempotencyStore

pytestmark = pytest.mark.anyio
A = UUID("10000000-0000-0000-0000-000000000001")
B = UUID("10000000-0000-0000-0000-000000000002")
ORG_A = UUID("20000000-0000-0000-0000-000000000001")
ORG_B = UUID("20000000-0000-0000-0000-000000000002")


@pytest.fixture
async def runtime(request, monkeypatch):
    if not request.config.getoption("--prompt6-postgres"):
        pytest.skip("Prompt 6 PostgreSQL opt-in; no local environment requested")
    try:
        url = make_url(os.environ.get("PROMPT6_RUNTIME_URL", ""))
    except Exception:
        pytest.fail("Local Prompt 6 runtime environment missing (value withheld)", pytrace=False)
    if (url.host not in {"localhost", "127.0.0.1"} or url.database != "nightclub_ai_prompt6_test"
            or url.username != "nightclub_api" or url.port != 5432 or not url.password or url.query):
        pytest.fail("Unsafe local target (value withheld)", pytrace=False)
    engine = create_async_engine(url.set(drivername="postgresql+asyncpg"), pool_size=getattr(request, "param", 1),
                                 max_overflow=0, hide_parameters=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionFactory", factory)
    statements = []
    def observe(connection, cursor, statement, parameters, context, many):
        # Store counts only: never record bound values or credentials.
        if statement.lstrip().startswith("INSERT INTO audit_logs"):
            statements.append("audit_insert")
    event.listen(engine.sync_engine, "before_cursor_execute", observe)
    try:
        async with factory() as session:
            await verify_runtime_role(session, "nightclub_api")
        yield factory, statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observe)
        await engine.dispose()


@asynccontextmanager
async def service(runtime, user=A, org=ORG_A):
    async with asynccontextmanager(database.get_db_session)() as session:
        await verify_runtime_role(session, "nightclub_api")
        await establish_user_context(session, CurrentUser(user))
        context = OrganizationContext(org, user, MemberRole.OWNER)
        await establish_organization_context(session, context)
        yield CampaignService(context, CampaignRepository(session, org), IdempotencyStore(session, context),
                              CampaignAuditWriter(session, context, uuid4())), session


async def create(runtime, key=None, user=A, org=ORG_A):
    async with service(runtime, user, org) as (business, _):
        return await business.mutate("create", key or uuid4(), CampaignCreate(name="Local fixture").model_dump())


async def test_catalog_exact_grants_policies_and_force(runtime):
    factory, _ = runtime
    path = Path(__file__).parents[1] / "migrations/versions/20260910_0004_campaign_business_access.py"
    spec = spec_from_file_location("prompt6_catalog_snapshot", path)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    async with factory() as session:
        # Independently check expected concrete policies and their expressions.
        policies = (await session.execute(text("SELECT tablename,policyname,roles,cmd,qual,with_check FROM pg_policies WHERE schemaname='public'"))).all()
        assert len(policies) == 10
        expected_names = {table + "_identity_select" for table in migration.BOOTSTRAP} | set(migration.POLICIES)
        assert {row[1] for row in policies} == expected_names
        for table, name, roles, command, using, check in policies:
            assert list(roles) == ["nightclub_api"]
            if name in migration.POLICIES:
                expected_table, expected_command, expected_using, expected_check = migration.POLICIES[name]
                assert (table, command) == (expected_table, expected_command)
                if name == "audit_campaign_insert":
                    # pg_get_expr rewrites IN to ANY(ARRAY[]); verify the exact
                    # literals and actor/tenant checks rather than textual IN.
                    assert using is None and all(value in check for value in ("campaign.created", "campaign.updated", "campaign.archived", "app.user_id", "app.organization_id"))
                else:
                    assert migration.normalized(using) == migration.normalized(expected_using)
                    assert migration.normalized(check).replace("::campaign_status", "") == migration.normalized(expected_check)
            else:
                assert check is None and migration.normalized(using) == migration.normalized(migration.BOOTSTRAP[table])
        rows = (await session.execute(text("SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity,pg_get_userbyid(c.relowner) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname=ANY(CAST(:tables AS text[]))"), {"tables": list(PROTECTED_TABLES)})).all()
        assert len(rows) == 20 and all(row[1] and row[2] and row[3] != "nightclub_api" for row in rows)
        connection = await session.connection()
        await connection.run_sync(lambda sync: migration.check_access(sync, expanded=True))
        assert not await session.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.prosecdef)"))


@pytest.mark.parametrize("context", ["none", "user_only", "malformed_user", "malformed_org", "foreign_org"])
async def test_missing_malformed_foreign_context_fails_closed(runtime, context):
    factory, _ = runtime
    async with factory() as session:
        await session.begin()
        if context != "none":
            await session.execute(text("SELECT set_config('app.user_id',:value,true)"), {"value": "bad" if context == "malformed_user" else str(A)})
        if context in {"malformed_user", "malformed_org", "foreign_org"}:
            await session.execute(text("SELECT set_config('app.organization_id',:value,true)"),
                                  {"value": "bad" if context == "malformed_org" else str(ORG_B)})
        try:
            rows = (await session.execute(select(Campaign))).all()
            assert rows == []
        except DBAPIError as error:
            assert context.startswith("malformed") and error.orig.sqlstate == "22P02"
        await session.rollback()


@pytest.mark.parametrize("table,operation", [("campaigns", "DELETE"), ("audit_logs", "UPDATE"), ("audit_logs", "DELETE"), ("audit_logs", "SELECT")])
async def test_runtime_forbidden_operations(runtime, table, operation):
    query = f"{operation} FROM {table}" if operation == "DELETE" else (f"SELECT * FROM {table}" if operation == "SELECT" else "UPDATE audit_logs SET action='campaign.updated'")
    with pytest.raises(DBAPIError) as error:
        async with service(runtime) as (_, session):
            await session.execute(text(query))
    assert error.value.orig.sqlstate == "42501"


@pytest.mark.parametrize("table", sorted(set(PROTECTED_TABLES) - {"profiles", "organization_members", "organizations", "campaigns", "idempotency_keys", "audit_logs"}))
async def test_unrelated_business_still_denied(runtime, table):
    with pytest.raises(DBAPIError) as error:
        async with service(runtime) as (_, session):
            await session.execute(text(f"SELECT * FROM {table}"))
    assert error.value.orig.sqlstate == "42501"


async def test_legitimate_mutations_replay_new_session_and_audit_noop(runtime):
    key = uuid4()
    a = await create(runtime, key)
    b = await create(runtime, key)
    assert a == b and a[0] == 201 and a[1]["status"] == "draft"
    campaign_id = UUID(a[1]["id"])
    assert len(runtime[1]) == 1
    async with service(runtime) as (business, session):
        result = await business.mutate("patch", uuid4(), {"objective": "local"}, campaign_id)
        assert result[1]["objective"] == "local"
        row = (await session.execute(text("SELECT expires_at-created_at FROM idempotency_keys WHERE key=:key"), {"key": key})).one()
        assert row[0].total_seconds() == 86400
    async with service(runtime) as (business, _):
        await business.mutate("archive", uuid4(), {}, campaign_id)
    async with service(runtime) as (business, _):
        await business.mutate("archive", uuid4(), {}, campaign_id)
    assert len(runtime[1]) == 3  # creation, update, archive; no replay/no-op INSERT.


@pytest.mark.parametrize("other_user,other_org", [(B, ORG_A), (B, ORG_B)])
async def test_key_collision_other_actor_or_tenant(runtime, other_user, other_org):
    key = uuid4()
    await create(runtime, key)
    with pytest.raises(IdempotencyConflict):
        await create(runtime, key, other_user, other_org)
    assert len(runtime[1]) == 1


async def test_direct_foreign_reads_writes_and_insert_checks(runtime):
    foreign = UUID((await create(runtime, user=B, org=ORG_B))[1]["id"])
    async with service(runtime) as (_, session):
        assert await session.scalar(select(Campaign).where(Campaign.id == foreign)) is None
        result = await session.execute(text("UPDATE campaigns SET name='hidden' WHERE id=:id"), {"id": foreign})
        assert result.rowcount == 0
    for organization, creator, status in ((ORG_B, A, "draft"), (ORG_A, B, "draft"), (ORG_A, A, "active")):
        with pytest.raises(DBAPIError) as error:
            async with service(runtime) as (_, session):
                await session.execute(text("INSERT INTO campaigns(organization_id,created_by,name,status) VALUES (:org,:actor,'local',CAST(:status AS campaign_status))"), {"org": organization, "actor": creator, "status": status})
        assert error.value.orig.sqlstate == "42501"


@pytest.mark.parametrize("runtime", [2], indirect=True)
async def test_concurrent_same_key_one_mutation_one_audit(runtime):
    key = uuid4()
    results = await asyncio.wait_for(asyncio.gather(create(runtime, key), create(runtime, key)), timeout=15)
    assert results[0] == results[1] and len(runtime[1]) == 1


@pytest.mark.parametrize("runtime", [2], indirect=True)
async def test_patch_archive_race_safe(runtime):
    campaign_id = UUID((await create(runtime))[1]["id"])
    async def operation(action):
        try:
            async with service(runtime) as (business, _):
                return await business.mutate(action, uuid4(), {"name": "edited"} if action == "patch" else {}, campaign_id)
        except CampaignConflict:
            return "conflict"
    results = await asyncio.wait_for(asyncio.gather(operation("patch"), operation("archive")), timeout=15)
    assert results[1][1]["status"] == "archived"
    with pytest.raises(CampaignConflict):
        async with service(runtime) as (business, _):
            assert (await business.read(campaign_id))["status"] == "archived"
            await business.mutate("patch", uuid4(), {"name": "forbidden"}, campaign_id)


@pytest.mark.parametrize("failure", ["campaign", "audit", "idempotency"])
async def test_atomic_rollback_of_all_mutation_parts(runtime, monkeypatch, failure):
    key = uuid4()
    before = None
    async with service(runtime) as (_, session):
        before = await session.scalar(text("SELECT count(*) FROM campaigns"))
    async def broken(*args, **kwargs):
        raise RuntimeError("synthetic local rollback")
    with pytest.raises(RuntimeError):
        async with service(runtime) as (business, _):
            target, method = {"campaign": (business.repository, "save"), "audit": (business.audit, "write"),
                              "idempotency": (business.idempotency, "complete")}[failure]
            monkeypatch.setattr(target, method, broken)
            await business.mutate("create", key, CampaignCreate(name="rollback fixture").model_dump())
    async with service(runtime) as (_, session):
        assert await session.scalar(text("SELECT count(*) FROM campaigns")) == before
        assert await session.scalar(text("SELECT count(*) FROM idempotency_keys WHERE key=:key"), {"key": key}) == 0


@pytest.mark.parametrize("ending", ["commit", "rollback", "error", "exception"])
async def test_context_cleanup_and_pool_one_reuse(runtime, ending):
    factory, _ = runtime
    pid = None
    try:
        async with service(runtime) as (_, session):
            pid = await session.scalar(text("SELECT pg_backend_pid()"))
            if ending == "rollback":
                await session.rollback()
            elif ending == "error":
                await session.execute(text("SELECT 1/0"))
            elif ending == "exception":
                raise ValueError("local fixture")
    except (DBAPIError, ValueError):
        assert ending in {"error", "exception"}
    async with factory() as session:
        assert await session.scalar(text("SELECT pg_backend_pid()")) == pid
        assert (await session.execute(text("SELECT NULLIF(current_setting('app.user_id',true),''),NULLIF(current_setting('app.organization_id',true),'')"))).one() == (None, None)
        assert list(await session.scalars(select(Campaign))) == []


async def test_cancellation_cleanup_no_context_leak(runtime):
    started = asyncio.Event()
    async def pending():
        async with service(runtime) as (_, session):
            started.set()
            await session.execute(text("SELECT pg_sleep(30)"))
    task = asyncio.create_task(pending())
    await started.wait()
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    async with runtime[0]() as session:
        assert (await session.execute(text("SELECT NULLIF(current_setting('app.user_id',true),''),NULLIF(current_setting('app.organization_id',true),'')"))).one() == (None, None)


@pytest.mark.parametrize("index,can_write,can_archive", [(1,True,True),(2,True,True),(3,True,False),(4,False,False),(5,False,False),(6,False,False)])
async def test_http_real_role_matrix(runtime, index, can_write, can_archive):
    user = UUID(f"10000000-0000-0000-0000-{index:012d}")
    campaign_id = (await create(runtime))[1]["id"]
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(user)
    headers = {"X-Organization-Id": str(ORG_A), "Idempotency-Key": str(uuid4())}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        assert (await client.get("/api/v1/campaigns", headers=headers)).status_code == 200
        response = await client.post("/api/v1/campaigns", headers=headers, json={"name": "HTTP fixture"})
        assert response.status_code == (201 if can_write else 403)
        headers["Idempotency-Key"] = str(uuid4())
        response = await client.post(f"/api/v1/campaigns/{campaign_id}/archive", headers=headers)
        assert response.status_code == (200 if can_archive else 403)


@pytest.mark.parametrize("user,org", [(A, ORG_B), (UUID("10000000-0000-0000-0000-000000000007"),ORG_A),
    (UUID("10000000-0000-0000-0000-000000000008"),ORG_A), (A,UUID("20000000-0000-0000-0000-000000000003"))])
async def test_http_inactive_removed_foreign_context(runtime, user, org):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(user)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        response = await client.get("/api/v1/campaigns", headers={"X-Organization-Id": str(org)})
    assert response.status_code == 403
