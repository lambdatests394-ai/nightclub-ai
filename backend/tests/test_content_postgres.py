"""Real PostgreSQL Prompt 7 only; opt-in retained nonowner runtime, no bypass.

Append-only audit/review INSERTs are counted as executed SQL paired with root
commit success, without granting SELECT or adding privileged test policies.
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
from backend.app.core.database_security import PROTECTED_TABLES
from backend.app.main import create_app
from backend.app.modules.content.audit import ContentAuditWriter
from backend.app.modules.content.errors import ContentConflict, ContentNotFound, ContentReferenceNotFound
from backend.app.modules.content.models import ContentItem, ContentVersion
from backend.app.modules.content.repository import ContentRepository
from backend.app.modules.content.schemas import ContentCreate
from backend.app.modules.content.service import ContentService
from backend.app.modules.identity.dependencies import get_current_user, protected_session
from backend.app.modules.identity.policy import CurrentUser
from backend.app.modules.identity.repository import SQLAlchemyIdentityRepository
from backend.app.modules.identity.service import IdentityService
from backend.app.shared.errors import IdempotencyConflict
from backend.app.shared.idempotency import IdempotencyStore

pytestmark = pytest.mark.anyio
A = UUID("10000000-0000-0000-0000-000000000001")
B = UUID("10000000-0000-0000-0000-000000000002")
D = UUID("10000000-0000-0000-0000-000000000004")
ORG_A = UUID("20000000-0000-0000-0000-000000000001")
ORG_B = UUID("20000000-0000-0000-0000-000000000002")


@pytest.fixture
async def runtime(request, monkeypatch):
    if not request.config.getoption("--prompt7-postgres"):
        pytest.skip("Prompt 7 real PostgreSQL opt-in is not enabled")
    try:
        url = make_url(os.environ.get("PROMPT7_RUNTIME_URL", ""))
    except Exception:
        pytest.fail("Explicit local Prompt 7 runtime URL required (value withheld)", pytrace=False)
    if (url.host not in {"localhost", "127.0.0.1"} or url.database != "nightclub_ai_prompt7_test"
            or url.username != "nightclub_api" or url.port != 5432 or not url.password or url.query):
        pytest.fail("Refusing unsafe Prompt 7 target (value withheld)", pytrace=False)
    engine = create_async_engine(url.set(drivername="postgresql+asyncpg"),
        pool_size=getattr(request,"param",1), max_overflow=0, hide_parameters=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database,"SessionFactory",factory)
    events = []
    def observe(connection, cursor, statement, parameters, context, many):
        for table in ("content_items", "content_versions", "review_decisions", "audit_logs"):
            if statement.lstrip().startswith("INSERT INTO " + table): events.append(table)
    event.listen(engine.sync_engine,"before_cursor_execute",observe)
    try:
        yield factory, events
    finally:
        event.remove(engine.sync_engine,"before_cursor_execute",observe)
        await engine.dispose()


@asynccontextmanager
async def business(runtime, user=A, org=ORG_A):
    async with protected_session(CurrentUser(user)) as session:
        identity_repo = SQLAlchemyIdentityRepository(session)
        context, _ = await IdentityService(identity_repo,
            organization_context_installer=identity_repo.establish_organization_context).organization(CurrentUser(user), org)
        yield ContentService(context,ContentRepository(session,org),IdempotencyStore(session,context),
            ContentAuditWriter(session,context,uuid4())), session


async def create(runtime, user=A, org=ORG_A, key=None, **fields):
    async with business(runtime,user,org) as (service, _):
        return await service.mutate("create",key or uuid4(),ContentCreate(platform="facebook",body="original",**fields).model_dump())


async def mutate(runtime, content_id, action, payload, *, user=A, key=None, org=ORG_A):
    async with business(runtime,user,org) as (service, _):
        return await service.mutate(action,key or uuid4(),payload,content_id)


async def test_catalog_grants_policies_force_and_append_only(runtime):
    path = Path(__file__).parents[1] / "migrations/versions/20260910_0005_content_business_access.py"
    spec = spec_from_file_location("prompt7_catalog_snapshot",path)
    migration = module_from_spec(spec); spec.loader.exec_module(migration)
    async with runtime[0]() as session:
        connection = await session.connection()
        await connection.run_sync(migration.verify_grants)
        rows = (await session.execute(text("SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity,pg_get_userbyid(c.relowner) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname=ANY(CAST(:tables AS text[]))"),{"tables":list(PROTECTED_TABLES)})).all()
        assert len(rows)==20 and all(rls and force and owner!="nightclub_api" for _,rls,force,owner in rows)
        policies = (await session.execute(text("SELECT policyname,tablename,roles,cmd,permissive,qual,with_check FROM pg_policies WHERE schemaname='public'"))).all()
        assert len(policies)==18
        expected = dict(migration.BASE.POLICIES)
        expected.update({t+"_identity_select":(t,"SELECT",expr,None) for t,expr in migration.BASE.BOOTSTRAP.items()})
        expected.update(migration.POLICIES)
        assert {r[0] for r in policies}==set(expected)
        for name,table,roles,cmd,permissive,using,check in policies:
            et,ec,eu,ew=expected[name]
            assert (table,cmd,permissive)==(et,ec,"PERMISSIVE") and list(roles)==["nightclub_api"]
            # Rendering adds native enum casts; expression values otherwise match.
            norm=lambda x:migration.normalized(x).replace("::content_status","")
            assert norm(using)==norm(eu) and norm(check)==norm(ew)
        assert not await session.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.prosecdef)"))


@pytest.mark.parametrize("kind",["missing","user_only","bad_user","bad_org","foreign_org"])
async def test_rls_missing_invalid_context(runtime,kind):
    async with runtime[0]() as session:
        if kind!="missing":
            await session.execute(text("SELECT set_config('app.user_id',:v,true)"),{"v":"invalid" if kind=="bad_user" else str(A)})
        if kind in {"bad_user","bad_org","foreign_org"}:
            await session.execute(text("SELECT set_config('app.organization_id',:v,true)"),{"v":"invalid" if kind=="bad_org" else str(ORG_B)})
        try:
            assert list(await session.scalars(select(ContentItem)))==[]
        except DBAPIError as error:
            assert kind in {"bad_user","bad_org"} and error.orig.sqlstate=="22P02"


@pytest.mark.parametrize("query",[
    "DELETE FROM content_items", "UPDATE content_versions SET body='x'", "DELETE FROM content_versions",
    "UPDATE review_decisions SET comment='x'", "DELETE FROM review_decisions", "SELECT * FROM review_decisions",
    "SELECT credentials_ciphertext FROM platform_connections", "SELECT * FROM platform_connections",
    "UPDATE content_items SET campaign_id=NULL", "UPDATE content_items SET scheduled_for=now()",
])
async def test_forbidden_direct_runtime_operations(runtime,query):
    with pytest.raises(DBAPIError) as error:
        async with business(runtime) as (_,session): await session.execute(text(query))
    assert error.value.orig.sqlstate=="42501"


@pytest.mark.parametrize("table",["assets","content_assets","publication_jobs","publication_attempts","ai_generation_requests","webhook_events","whatsapp_conversations","whatsapp_messages","outbox_events","automation_runs"])
async def test_unrelated_tables_stay_closed(runtime,table):
    with pytest.raises(DBAPIError) as error:
        async with business(runtime) as (_,session): await session.execute(text(f"SELECT * FROM {table}"))
    assert error.value.orig.sqlstate=="42501"


async def test_create_patch_review_immutable_and_exact_decision(runtime):
    content_id=UUID((await create(runtime))[1]["id"])
    await mutate(runtime,content_id,"patch",{"title":"new title"})
    await mutate(runtime,content_id,"submit-review",{})
    key=uuid4()
    approved=await mutate(runtime,content_id,"review",{"version_no":2,"decision":"approved","comment":None},user=D,key=key)
    assert approved[1]["approvedVersionNo"]==2
    assert await mutate(runtime,content_id,"review",{"version_no":2,"decision":"approved","comment":None},user=D,key=key)==approved
    assert runtime[1].count("review_decisions")==1
    assert runtime[1].count("audit_logs")==4
    edited=await mutate(runtime,content_id,"patch",{"body":"new body"})
    assert edited[1]["status"]=="draft" and edited[1]["approvedVersionNo"] is None
    async with business(runtime) as (_,session):
        rows=(await session.execute(select(ContentVersion.version_no,ContentVersion.body,ContentVersion.title).where(ContentVersion.content_item_id==content_id).order_by(ContentVersion.version_no))).all()
        assert rows==[(1,"original",None),(2,"original","new title"),(3,"new body","new title")]


@pytest.mark.parametrize("runtime",[2],indirect=True)
async def test_concurrent_patch_versions_serialize(runtime):
    content_id=UUID((await create(runtime))[1]["id"])
    results=await asyncio.wait_for(asyncio.gather(mutate(runtime,content_id,"patch",{"body":"a"}),mutate(runtime,content_id,"patch",{"body":"b"})),15)
    assert {r[1]["currentVersionNo"] for r in results}=={2,3}
    async with business(runtime) as (_,session):
        rows=list(await session.scalars(select(ContentVersion.body).where(ContentVersion.content_item_id==content_id)))
        assert sorted(rows)==["a","b","original"]


@pytest.mark.parametrize("runtime",[2],indirect=True)
@pytest.mark.parametrize("same",[True,False])
async def test_concurrent_key_one_mutation_or_conflict(runtime,same):
    content_id=UUID((await create(runtime))[1]["id"])
    key=uuid4()
    async def attempt(body):
        try:return await mutate(runtime,content_id,"patch",{"body":body},key=key)
        except IdempotencyConflict:return "conflict"
    results=await asyncio.wait_for(asyncio.gather(attempt("a"),attempt("a" if same else "b")),15)
    if same: assert results[0]==results[1]
    else: assert results.count("conflict")==1
    assert runtime[1].count("content_versions")==2 and runtime[1].count("audit_logs")==2


@pytest.mark.parametrize("runtime",[2],indirect=True)
async def test_concurrent_reviewers_one_transition_and_no_failed_claim(runtime):
    content_id=UUID((await create(runtime))[1]["id"])
    await mutate(runtime,content_id,"submit-review",{})
    keys=[uuid4(),uuid4()]
    async def review(user,key):
        try:return await mutate(runtime,content_id,"review",{"version_no":1,"decision":"approved","comment":None},user=user,key=key)
        except ContentConflict:return "conflict"
    results=await asyncio.wait_for(asyncio.gather(review(B,keys[0]),review(D,keys[1])),15)
    assert results.count("conflict")==1 and runtime[1].count("review_decisions")==1
    for user,key,result in zip((B,D),keys,results):
        async with business(runtime,user) as (_,session):
            count=await session.scalar(text("SELECT count(*) FROM idempotency_keys WHERE key=:key"),{"key":key})
            assert count==(0 if result=="conflict" else 1)


async def test_stale_review_cannot_approve_new_version(runtime):
    content_id=UUID((await create(runtime))[1]["id"])
    await mutate(runtime,content_id,"patch",{"body":"v2"})
    await mutate(runtime,content_id,"submit-review",{})
    with pytest.raises(ContentConflict):
        await mutate(runtime,content_id,"review",{"version_no":1,"decision":"approved","comment":"reason"})


@pytest.mark.parametrize("user,org",[(B,ORG_A),(B,ORG_B)])
async def test_global_key_hidden_actor_tenant(runtime,user,org):
    key=uuid4()
    await create(runtime,key=key)
    with pytest.raises(IdempotencyConflict): await create(runtime,user=user,org=org,key=key)


async def test_foreign_content_and_reference_hidden(runtime):
    foreign=UUID((await create(runtime,user=B,org=ORG_B))[1]["id"])
    async with business(runtime) as (service,session):
        with pytest.raises(ContentNotFound): await service.read(foreign)
        assert await session.scalar(select(ContentItem).where(ContentItem.id==foreign)) is None
    with pytest.raises(ContentNotFound): await mutate(runtime,foreign,"patch",{"body":"hidden"})
    for field,value in (("campaign_id",UUID("40000000-0000-0000-0000-000000000003")),("connection_id",UUID("30000000-0000-0000-0000-000000000003"))):
        with pytest.raises(ContentReferenceNotFound): await create(runtime,**{field:value})


@pytest.mark.parametrize("suffix,expected",[(1,None),(2,ContentConflict),(4,ContentConflict)])
async def test_minimal_connection_metadata_runtime(runtime,suffix,expected):
    connection_id=UUID(f"30000000-0000-0000-0000-{suffix:012d}")
    if expected:
        with pytest.raises(expected): await create(runtime,connection_id=connection_id)
    else:
        assert (await create(runtime,connection_id=connection_id))[1]["connectionId"]==str(connection_id)


@pytest.mark.parametrize("failure",["version","audit","idempotency"])
async def test_rollback_clears_partial_rows_and_claim(runtime,monkeypatch,failure):
    key=uuid4()
    async with business(runtime) as (_,session):
        count=await session.scalar(text("SELECT count(*) FROM content_items"))
    with pytest.raises(DBAPIError):
        async with business(runtime) as (service,session):
            async def broken(*args,**kwargs): await session.execute(text("SELECT 1/0"))
            target,method={"version":(service.repository,"add_content_version"),"audit":(service.audit,"write"),"idempotency":(service.idempotency,"complete")}[failure]
            monkeypatch.setattr(target,method,broken)
            await service.mutate("create",key,ContentCreate(platform="facebook",body="rollback").model_dump())
    async with business(runtime) as (_,session):
        assert await session.scalar(text("SELECT count(*) FROM content_items"))==count
        assert await session.scalar(text("SELECT count(*) FROM idempotency_keys WHERE key=:key"),{"key":key})==0
    # A failed claim does not permanently consume its UUID.
    assert (await create(runtime,key=key))[0]==201


@pytest.mark.parametrize("ending",["commit","rollback","error","exception"])
async def test_root_pool_one_cleanup(runtime,ending):
    pid=None
    try:
        async with business(runtime) as (_,session):
            pid=await session.scalar(text("SELECT pg_backend_pid()"))
            if ending=="rollback":await session.rollback()
            if ending=="error":await session.execute(text("SELECT 1/0"))
            if ending=="exception":raise ValueError("synthetic rollback")
    except (DBAPIError,ValueError):assert ending in {"error","exception"}
    async with runtime[0]() as session:
        assert await session.scalar(text("SELECT pg_backend_pid()"))==pid
        assert (await session.execute(text("SELECT NULLIF(current_setting('app.user_id',true),''),NULLIF(current_setting('app.organization_id',true),'')"))).one()==(None,None)
        assert list(await session.scalars(select(ContentItem)))==[]


async def test_cancellation_releases_content_lock_and_no_context_leak(runtime):
    content_id=UUID((await create(runtime))[1]["id"])
    ready=asyncio.Event()
    async def pending():
        async with business(runtime) as (service,session):
            await service.repository.get_item(content_id)
            ready.set()
            await session.execute(text("SELECT pg_sleep(30)"))
    task=asyncio.create_task(pending()); await ready.wait(); await asyncio.sleep(0.05); task.cancel()
    with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(task,5)
    async with runtime[0]() as session:
        assert (await session.execute(text("SELECT NULLIF(current_setting('app.user_id',true),''),NULLIF(current_setting('app.organization_id',true),'')"))).one()==(None,None)
    assert (await asyncio.wait_for(mutate(runtime,content_id,"patch",{"body":"after cancel"}),5))[0]==200


@pytest.mark.parametrize("index,write,review",[(1,True,True),(2,True,True),(3,True,False),(4,False,True),(5,False,False),(6,False,False)])
async def test_http_real_six_role_matrix(runtime,index,write,review):
    content_id=UUID((await create(runtime))[1]["id"])
    await mutate(runtime,content_id,"submit-review",{})
    user=UUID(f"10000000-0000-0000-0000-{index:012d}")
    app=create_app(); app.dependency_overrides[get_current_user]=lambda:CurrentUser(user)
    headers={"X-Organization-Id":str(ORG_A),"Idempotency-Key":str(uuid4())}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://local.test") as client:
        assert (await client.get("/api/v1/content",headers=headers)).status_code==200
        created=await client.post("/api/v1/content",headers=headers,json={"platform":"whatsapp","body":"local"})
        assert created.status_code==(201 if write else 403)
        headers["Idempotency-Key"]=str(uuid4())
        reviewed=await client.post(f"/api/v1/content/{content_id}/review",headers=headers,json={"versionNo":1,"decision":"approved","comment":"explicit owner reason"})
        assert reviewed.status_code==(200 if review else 403)


@pytest.mark.parametrize("user,org",[(A,ORG_B),(UUID("10000000-0000-0000-0000-000000000007"),ORG_A),(UUID("10000000-0000-0000-0000-000000000008"),ORG_A),(A,UUID("20000000-0000-0000-0000-000000000003"))])
async def test_http_inactive_foreign_removed_context(runtime,user,org):
    app=create_app(); app.dependency_overrides[get_current_user]=lambda:CurrentUser(user)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://local.test") as client:
        response=await client.get("/api/v1/content",headers={"X-Organization-Id":str(org)})
    assert response.status_code==403


async def test_campaign_archive_blocks_later_content_mutation(runtime):
    # A private campaign fixture per test; no shared seed state modified.
    from backend.app.modules.campaigns.dependencies import get_campaign_service
    app=create_app(); app.dependency_overrides[get_current_user]=lambda:CurrentUser(A)
    headers={"X-Organization-Id":str(ORG_A),"Idempotency-Key":str(uuid4())}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://local.test") as client:
        response=await client.post("/api/v1/campaigns",headers=headers,json={"name":"Local content campaign"})
        assert response.status_code==201
        campaign_id=UUID(response.json()["data"]["id"])
        content_id=UUID((await create(runtime,campaign_id=campaign_id))[1]["id"])
        headers["Idempotency-Key"]=str(uuid4())
        assert (await client.post(f"/api/v1/campaigns/{campaign_id}/archive",headers=headers)).status_code==200
    async with business(runtime) as (service,_):assert (await service.read(content_id))["id"]==str(content_id)
    for action,payload in (("patch",{"body":"changed"}),("submit-review",{}),("review",{"version_no":1,"decision":"approved","comment":"reason"})):
        with pytest.raises(ContentConflict):await mutate(runtime,content_id,action,payload)


@pytest.mark.parametrize("kind",["creator","status","version","campaign","schedule"])
async def test_direct_content_insert_forgery_denied(runtime,kind):
    fields={"organization_id":ORG_A,"created_by":A,"platform":"facebook","status":"draft","current_version_no":1,"approved_version_no":None}
    if kind=="creator":fields["created_by"]=B
    if kind=="status":fields["status"]="approved"
    if kind=="version":fields["current_version_no"]=2
    if kind=="campaign":fields["campaign_id"]=UUID("40000000-0000-0000-0000-000000000003")
    if kind=="schedule":fields["scheduled_for"]=None
    from sqlalchemy import insert
    with pytest.raises(DBAPIError) as error:
        async with business(runtime) as (_,session):await session.execute(insert(ContentItem.__table__).inline().values(**fields))
    assert error.value.orig.sqlstate=="42501"
