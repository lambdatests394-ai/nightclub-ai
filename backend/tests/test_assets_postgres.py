"""Opt-in Prompt 8 real PostgreSQL RLS. Storage traffic is always a local fake.

No grant, policy, role or FORCE bypass is used to observe append-only audit data.
"""
import asyncio
from contextlib import asynccontextmanager, contextmanager
from importlib.util import module_from_spec, spec_from_file_location
import os
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from psycopg.conninfo import make_conninfo
import pytest
from sqlalchemy import event, insert, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core import database
from backend.app.core.config import Settings
from backend.app.core.database_security import PROTECTED_TABLES
from backend.app.modules.assets.audit import AssetAuditWriter
from backend.app.modules.assets.coordinator import AssetCoordinator
from backend.app.modules.assets.errors import AssetConflict, AssetNotFound, AssetUnavailable
from backend.app.modules.assets.models import Asset
from backend.app.modules.assets.repository import AssetRepository
from backend.app.modules.assets.service import AssetService
from backend.app.modules.assets.storage import StorageUnavailable
from backend.app.modules.content.audit import ContentAuditWriter
from backend.app.modules.content.errors import ContentConflict, ContentReferenceNotFound
from backend.app.modules.content.models import ContentAsset, ContentItem, ContentVersion
from backend.app.modules.content.repository import ContentRepository
from backend.app.modules.content.schemas import ContentCreate
from backend.app.modules.content.service import ContentService
from backend.app.modules.identity.dependencies import protected_session
from backend.app.modules.identity.errors import Forbidden
from backend.app.modules.identity.policy import CurrentUser
from backend.app.modules.identity.repository import SQLAlchemyIdentityRepository
from backend.app.modules.identity.service import IdentityService
from backend.app.shared.errors import IdempotencyConflict
from backend.app.shared.idempotency import IdempotencyStore
from backend.tests.assets_fakes import FakeStorage, intent, picture

pytestmark = pytest.mark.anyio
A = UUID("10000000-0000-0000-0000-000000000001")
B = UUID("10000000-0000-0000-0000-000000000002")
ORG_A = UUID("20000000-0000-0000-0000-000000000001")
ORG_B = UUID("20000000-0000-0000-0000-000000000002")
HISTORICAL_VERSION = UUID("80000000-0000-0000-0000-000000000002")
HISTORICAL_ITEM = UUID("80000000-0000-0000-0000-000000000001")
ROOT = Path(__file__).parents[2]


def load_migration():
    spec = spec_from_file_location("prompt8_catalog", ROOT / "backend/migrations/versions/20260922_0006_asset_business_access.py")
    module = module_from_spec(spec); spec.loader.exec_module(module)
    return module


def safe_url(name, role):
    try:
        url = make_url(os.environ.get(name, ""))
    except Exception:
        pytest.fail(f"{name}: explicit local environment required (value withheld)", pytrace=False)
    if (url.host not in {"localhost", "127.0.0.1"} or url.database != "nightclub_ai_prompt8_test"
            or url.username != role or url.port != 5432 or not url.password or url.query
            or url.drivername not in {"postgresql+asyncpg", "postgresql+psycopg"}):
        pytest.fail(f"{name}: unsafe target (value withheld)", pytrace=False)
    return url


@pytest.fixture
async def runtime(request, monkeypatch):
    if not request.config.getoption("--prompt8-postgres"):
        pytest.skip("Prompt 8 real PostgreSQL opt-in is not enabled")
    url = safe_url("PROMPT8_RUNTIME_URL", "nightclub_api")
    engine = create_async_engine(url.set(drivername="postgresql+asyncpg"),
        pool_size=getattr(request, "param", 1), max_overflow=0, hide_parameters=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionFactory", factory)
    writes = []
    def observe(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().startswith("INSERT INTO audit_logs"):
            writes.append("audit_insert")
    event.listen(engine.sync_engine, "before_cursor_execute", observe)
    try:
        yield factory, writes
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observe)
        await engine.dispose()


@asynccontextmanager
async def business(runtime, user=A, org=ORG_A):
    async with protected_session(CurrentUser(user)) as session:
        repo = SQLAlchemyIdentityRepository(session)
        context, _ = await IdentityService(repo, organization_context_installer=repo.establish_organization_context).organization(CurrentUser(user), org)
        yield AssetService(context, AssetRepository(session, org), IdempotencyStore(session, context),
                           AssetAuditWriter(session, context, uuid4()), Settings(_env_file=None)), session


def coordinator(runtime, provider=None, user=A, org=ORG_A):
    @asynccontextmanager
    async def transaction():
        async with business(runtime, user, org) as (service, _):
            yield service
    return AssetCoordinator(transaction, provider or FakeStorage(), download_ttl=60, timeout=15)


async def new_asset(runtime, *, ready=False, user=A, org=ORG_A, digest=None):
    # Unique synthetic expected digests avoid coupling otherwise independent tests.
    body = intent().model_copy(update={"sha256": digest or uuid4().hex + uuid4().hex})
    async with business(runtime, user, org) as (service, session):
        result = await service.upload_intent(uuid4(), body)
        asset_id = UUID(result[1]["asset"]["id"])
        if ready:
            await session.execute(text("UPDATE assets SET status='ready',width=4,height=3 WHERE id=:id"), {"id": asset_id})
    return asset_id


def content_service(service, session):
    return ContentService(service.context, ContentRepository(session, service.context.organization_id),
        IdempotencyStore(session, service.context), ContentAuditWriter(session, service.context, uuid4()))


async def new_content(runtime, assets=(), *, user=A, org=ORG_A):
    async with business(runtime, user, org) as (service, session):
        _, data = await content_service(service, session).mutate("create", uuid4(),
            ContentCreate(platform="facebook", body="Prompt 8 local", asset_ids=list(assets)).model_dump())
        return UUID(data["id"])


async def attach(session, content_id, version_id, asset_id, *, organization=ORG_A, position=0):
    await session.execute(insert(ContentAsset.__table__).inline().values(
        content_item_id=content_id, content_version_id=version_id,
        organization_id=organization, asset_id=asset_id, position=position))


async def fresh_version(service, session, *, actor=A, state="draft", version_no=1):
    item_id, version_id = uuid4(), uuid4()
    repo = ContentRepository(session, service.context.organization_id)
    await repo.create_item(content_id=item_id, actor_id=service.context.user_id, campaign_id=None, platform="facebook", connection_id=None)
    await repo.add_content_version(ContentVersion(id=version_id, content_item_id=item_id,
        version_no=version_no, body="fixture", source="manual", created_by=actor))
    if state != "draft":
        await session.execute(text("UPDATE content_items SET status=CAST(:s AS content_status) WHERE id=:id"), {"s": state, "id": item_id})
    return item_id, version_id


async def test_catalog_exact_grants_24_policies_force_marker(runtime):
    migration = load_migration()
    async with runtime[0]() as session:
        conn = await session.connection()
        await conn.run_sync(migration.verify_grants)
        await conn.run_sync(lambda bind: migration.verify_historical_policies(bind, expanded=True))
        rows = (await session.execute(text("""SELECT relname,relrowsecurity,relforcerowsecurity,pg_get_userbyid(relowner)
            FROM pg_class WHERE relnamespace='public'::regnamespace AND relname=ANY(CAST(:tables AS text[]))"""), {"tables": list(PROTECTED_TABLES)})).all()
        assert len(rows) == 20 and all(rls and force and owner != "nightclub_api" for _, rls, force, owner in rows)
        assert await session.scalar(text("SELECT count(*) FROM pg_policies WHERE schemaname='public'")) == 24
        policy = await session.scalar(text("SELECT with_check FROM pg_policies WHERE policyname='content_assets_business_insert'"))
        assert "pg_current_xact_id()" in policy and "transaction_timestamp()" in policy
        assert not await session.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_proc WHERE pronamespace='public'::regnamespace AND prosecdef)"))
        # The harness checks alembic_version with its migration connection.
        # Runtime receives no extra SELECT grant on Alembic's bookkeeping table.


async def test_asset_tenant_reads_and_hidden_foreign(runtime):
    ours = await new_asset(runtime)
    other = await new_asset(runtime, user=B, org=ORG_B)
    async with business(runtime) as (service, session):
        assert await service.repository.find_by_id(ours) is not None
        assert await session.scalar(select(Asset).where(Asset.id == other)) is None
        with pytest.raises(AssetNotFound): await service.download_target(other)
        assert (await session.execute(text("UPDATE assets SET status='rejected' WHERE id=:id"), {"id": other})).rowcount == 0


@pytest.mark.parametrize("kind", ["foreign_org", "forged_actor", "ready", "rejected", "deleted", "bucket", "key", "mime", "size", "digest"])
async def test_direct_asset_insert_authority_rejected(runtime, kind):
    asset_id = uuid4()
    values = dict(id=asset_id, organization_id=ORG_A, uploaded_by=A, kind="image", mime_type="image/png",
                  storage_bucket="nightclub-assets", storage_key=f"org/{ORG_A}/assets/{asset_id}/asset.png",
                  byte_size=20, sha256=uuid4().hex + uuid4().hex, status="pending")
    if kind == "foreign_org": values["organization_id"] = ORG_B
    elif kind == "forged_actor": values["uploaded_by"] = B
    elif kind in {"ready", "rejected", "deleted"}: values["status"] = kind
    elif kind == "bucket": values["storage_bucket"] = "other"
    elif kind == "key": values["storage_key"] = "arbitrary/path"
    elif kind == "mime": values["mime_type"] = "image/svg+xml"
    elif kind == "size": values["byte_size"] = 10_485_761
    else: values["sha256"] = "A" * 64
    with pytest.raises(DBAPIError) as error:
        async with business(runtime) as (_, session):
            await session.execute(insert(Asset.__table__).inline().values(**values))
    assert error.value.orig.sqlstate == "42501"


@pytest.mark.parametrize("state", ["ready", "rejected"])
async def test_asset_pending_transition_and_terminal_immutable(runtime, state):
    asset_id = await new_asset(runtime)
    async with business(runtime) as (_, session):
        result = await session.execute(text("UPDATE assets SET status=:s,width=:w,height=:h WHERE id=:id"),
            {"s": state, "w": 3 if state == "ready" else None, "h": 2 if state == "ready" else None, "id": asset_id})
        assert result.rowcount == 1
    async with business(runtime) as (_, session):
        result = await session.execute(text("UPDATE assets SET status='rejected',width=NULL,height=NULL WHERE id=:id"), {"id": asset_id})
        assert result.rowcount == 0
        assert await session.scalar(select(Asset.status).where(Asset.id == asset_id)) == state


@pytest.mark.parametrize("assignment", ["status='deleted'", "status='pending'", "status='ready'", "status='ready',width=8193,height=1", "status='ready',width=5000,height=5000", "status='rejected',width=1,height=1"])
async def test_invalid_resulting_asset_structure_denied(runtime, assignment):
    asset_id = await new_asset(runtime)
    with pytest.raises(DBAPIError) as error:
        async with business(runtime) as (_, session):
            await session.execute(text(f"UPDATE assets SET {assignment} WHERE id=:id"), {"id": asset_id})
    assert error.value.orig.sqlstate == "42501"


@pytest.mark.parametrize("statement", ["DELETE FROM assets", "DELETE FROM content_assets", "UPDATE content_assets SET position=1",
    "UPDATE assets SET organization_id=NULL", "UPDATE assets SET storage_key='other'", "UPDATE assets SET sha256='other'",
    "UPDATE assets SET uploaded_by=NULL", "UPDATE assets SET original_filename='other'", "UPDATE assets SET deleted_at=now()",
    "UPDATE content_versions SET attachment_creation_xid=pg_current_xact_id()", "UPDATE content_versions SET body='other'",
    "UPDATE audit_logs SET action='asset.ready'", "DELETE FROM audit_logs", "SELECT * FROM audit_logs"])
async def test_runtime_forbidden_grants(runtime, statement):
    with pytest.raises(DBAPIError) as error:
        async with business(runtime) as (_, session): await session.execute(text(statement))
    assert error.value.orig.sqlstate == "42501"


async def test_default_marker_same_transaction_ordered_and_closed_after_commit(runtime):
    a, b = await new_asset(runtime, ready=True), await new_asset(runtime, ready=True)
    async with business(runtime) as (service, session):
        item, version = await fresh_version(service, session)
        check = (await session.execute(text("SELECT attachment_creation_xid=pg_current_xact_id(),created_at=transaction_timestamp() FROM content_versions WHERE id=:id"), {"id": version})).one()
        assert check == (True, True)
        await attach(session, item, version, b, position=0)
        await attach(session, item, version, a, position=1)
        assert await ContentRepository(session, ORG_A).asset_ids(version) == [b, a]
    extra = await new_asset(runtime, ready=True)
    with pytest.raises(DBAPIError) as error:
        async with business(runtime) as (_, session): await attach(session, item, version, extra, position=2)
    assert error.value.orig.sqlstate == "42501"


async def test_historical_marker_null_and_attachment_denied(runtime):
    asset_id = await new_asset(runtime, ready=True)
    async with business(runtime) as (_, session):
        assert (await session.execute(text("SELECT attachment_creation_xid FROM content_versions WHERE id=:id"), {"id": HISTORICAL_VERSION})).one() == (None,)
    with pytest.raises(DBAPIError) as error:
        async with business(runtime) as (_, session): await attach(session, HISTORICAL_ITEM, HISTORICAL_VERSION, asset_id)
    assert error.value.orig.sqlstate == "42501"


async def test_runtime_cannot_insert_marker(runtime):
    with pytest.raises(DBAPIError) as error:
        async with business(runtime) as (_, session):
            await session.execute(text("""INSERT INTO content_versions(id,content_item_id,version_no,body,source,created_by,attachment_creation_xid)
                VALUES (:id,:item,2,'fixture','manual',:actor,pg_current_xact_id())"""), {"id": uuid4(), "item": HISTORICAL_ITEM, "actor": A})
    assert error.value.orig.sqlstate == "42501"


@pytest.mark.parametrize("case", ["foreign", "pending", "not_current", "not_draft", "position", "creator"])
async def test_attachment_policy_predicates(runtime, case):
    asset_id = await new_asset(runtime, ready=case != "pending", user=B if case == "foreign" else A, org=ORG_B if case == "foreign" else ORG_A)
    with pytest.raises(DBAPIError) as error:
        async with business(runtime) as (service, session):
            item, version = await fresh_version(service, session,
                state="approved" if case == "not_draft" else "draft", version_no=2 if case == "not_current" else 1)
            if case == "creator":
                # Change verified tenant actor within this SQL-level adversarial test only.
                await session.execute(text("SELECT set_config('app.user_id',:user,true)"), {"user": str(B)})
            await attach(session, item, version, asset_id, position=10 if case == "position" else 0)
    # The pre-existing ready trigger rejects a pending/hidden asset before RLS.
    assert error.value.orig.sqlstate in ({"42501", "P0001"} if case in {"pending", "foreign"} else {"42501"})


@contextmanager
def restore_like_default(kind):
    """Disposable DB fixture: corrupt only a default, restore it in finally.

    This simulates imported/restored metadata without granting runtime control of
    either field and without disabling RLS/FORCE or adding a privileged policy.
    No application or historical migration file is changed.
    """
    url = safe_url("DATABASE_MIGRATION_URL", "alembic_test_user")
    column, bad, good = (("attachment_creation_xid", "'0'::xid8", "pg_current_xact_id()") if kind == "xid"
                         else ("created_at", "'2000-01-01T00:00:00Z'::timestamptz", "now()"))
    info = make_conninfo(host=url.host, port=url.port, dbname=url.database, user=url.username, password=url.password, connect_timeout=5)
    with psycopg.connect(info, autocommit=True) as owner:
        owner.execute("SET lock_timeout='3s'")
        owner.execute(f"ALTER TABLE public.content_versions ALTER COLUMN {column} SET DEFAULT {bad}")
        try:
            yield
        finally:
            owner.execute(f"ALTER TABLE public.content_versions ALTER COLUMN {column} SET DEFAULT {good}")


@pytest.mark.parametrize("kind", ["xid", "timestamp"])
async def test_restored_like_marker_or_timestamp_cannot_attach(runtime, kind):
    asset_id = await new_asset(runtime, ready=True)
    with restore_like_default(kind):
        with pytest.raises(DBAPIError) as error:
            async with business(runtime) as (service, session):
                item, version = await fresh_version(service, session)
                pair = (await session.execute(text("SELECT attachment_creation_xid=pg_current_xact_id(),created_at=transaction_timestamp() FROM content_versions WHERE id=:id"), {"id": version})).one()
                assert pair == ((False, True) if kind == "xid" else (True, False))
                await attach(session, item, version, asset_id)
        assert error.value.orig.sqlstate == "42501"


async def test_content_full_snapshot_patch_review_and_rollback(runtime):
    a, b = await new_asset(runtime, ready=True), await new_asset(runtime, ready=True)
    content_id = await new_content(runtime, [a, b])
    async with business(runtime) as (service, session):
        content = content_service(service, session)
        result = await content.mutate("patch", uuid4(), {"asset_ids": [b, a]}, content_id)
        assert result[1]["assetIds"] == [str(b), str(a)] and result[1]["currentVersionNo"] == 2
        await content.mutate("submit-review", uuid4(), {}, content_id)
        await content.mutate("review", uuid4(), {"version_no": 2, "decision": "approved", "comment": "Owner review fixture"}, content_id)
    async with business(runtime) as (service, session):
        result = await content_service(service, session).mutate("patch", uuid4(), {"title": "new title"}, content_id)
        assert result[1]["status"] == "draft" and result[1]["approvedVersionNo"] is None
        assert result[1]["assetIds"] == [str(b), str(a)]
        versions = list(await session.scalars(select(ContentVersion).where(ContentVersion.content_item_id == content_id).order_by(ContentVersion.version_no)))
        assert [await ContentRepository(session, ORG_A).asset_ids(v.id) for v in versions] == [[a, b], [b, a], [b, a]]
    async with business(runtime) as (service, session):
        result = await content_service(service, session).mutate("patch", uuid4(), {"asset_ids": []}, content_id)
        assert result[1]["assetIds"] == []
    async with business(runtime) as (_, session):
        before = await session.scalar(text("SELECT count(*) FROM content_items"))
    key = uuid4()
    with pytest.raises(ContentReferenceNotFound):
        async with business(runtime) as (service, session):
            await content_service(service, session).mutate("create", key,
                ContentCreate(platform="facebook", body="rollback", asset_ids=[a, uuid4()]).model_dump())
    async with business(runtime) as (_, session):
        assert await session.scalar(text("SELECT count(*) FROM content_items")) == before
        assert await session.scalar(text("SELECT count(*) FROM idempotency_keys WHERE key=:key"), {"key": key}) == 0


@pytest.mark.parametrize("runtime", [2], indirect=True)
async def test_concurrent_same_key_one_asset_one_audit_and_fresh_capabilities(runtime):
    body = intent().model_copy(update={"sha256": uuid4().hex + uuid4().hex})
    key, provider = uuid4(), FakeStorage()
    api = coordinator(runtime, provider)
    first, second = await asyncio.wait_for(asyncio.gather(api.upload(key, body), api.upload(key, body)), 15)
    assert first[1]["asset"] == second[1]["asset"] and len(runtime[1]) == 1
    assert first[1]["upload"]["url"] != second[1]["upload"]["url"]
    async with business(runtime) as (_, session):
        durable = await session.scalar(text("SELECT response_body FROM idempotency_keys WHERE key=:key"), {"key": key})
        assert set(durable) == {"asset"}


@pytest.mark.parametrize("runtime", [2], indirect=True)
async def test_concurrent_distinct_keys_same_hash_one_winner(runtime):
    body = intent().model_copy(update={"sha256": uuid4().hex + uuid4().hex})
    api = coordinator(runtime)
    async def attempt():
        try: return await api.upload(uuid4(), body)
        except AssetConflict: return "conflict"
    results = await asyncio.wait_for(asyncio.gather(attempt(), attempt()), 15)
    assert results.count("conflict") == 1 and len(runtime[1]) == 1


async def test_sign_failure_then_successful_completion_replay_and_audit(runtime):
    # Unique original bytes produce a real verified digest, even across harness tests.
    from PIL import PngImagePlugin
    info = PngImagePlugin.PngInfo(); info.add_text("Fixture", str(uuid4()))
    data = picture(pnginfo=info)
    provider, key = FakeStorage(data), uuid4()
    provider.sign_error = StorageUnavailable
    api = coordinator(runtime, provider)
    with pytest.raises(AssetUnavailable): await api.upload(key, intent(data))
    async with business(runtime) as (service, _):
        asset = await service.repository.find_by_sha256(intent(data).sha256)
        assert asset.status == "pending"
        asset_id = asset.id
    provider.sign_error = None
    assert UUID((await api.upload(key, intent(data)))[1]["asset"]["id"]) == asset_id
    complete_key = uuid4()
    result = await api.complete(asset_id, complete_key)
    assert result[1]["asset"]["status"] == "ready"
    assert await api.complete(asset_id, complete_key) == result
    assert len(runtime[1]) == 2
    with pytest.raises(AssetConflict): await api.complete(asset_id, uuid4())


@pytest.mark.parametrize("other_user,other_org", [(B, ORG_A), (B, ORG_B)])
async def test_global_idempotency_collision_hidden_across_actor_and_tenant(runtime, other_user, other_org):
    key, body = uuid4(), intent().model_copy(update={"sha256": uuid4().hex + uuid4().hex})
    await coordinator(runtime).upload(key, body)
    with pytest.raises(IdempotencyConflict): await coordinator(runtime, user=other_user, org=other_org).upload(key, body)


@pytest.mark.parametrize("mode", ["commit", "rollback", "cancel"])
async def test_pool_one_context_clears_and_no_failed_completion_result(runtime, mode):
    asset_id = await new_asset(runtime)
    key = uuid4()
    if mode == "commit":
        async with business(runtime): pass
    elif mode == "rollback":
        with pytest.raises(RuntimeError):
            async with business(runtime): raise RuntimeError("local fixture rollback")
    else:
        provider, started = FakeStorage(), asyncio.Event()
        async def block(*args): started.set(); await asyncio.Event().wait()
        provider.stat_object = block
        task = asyncio.create_task(coordinator(runtime, provider).complete(asset_id, key))
        await asyncio.wait_for(started.wait(), 10); task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    async with runtime[0]() as session:
        pair = (await session.execute(text("SELECT NULLIF(current_setting('app.user_id',true),''),NULLIF(current_setting('app.organization_id',true),'')"))).one()
        assert pair == (None, None)
    async with business(runtime) as (service, session):
        assert (await service.repository.find_by_id(asset_id)).status == "pending"
        assert await session.scalar(text("SELECT count(*) FROM idempotency_keys WHERE key=:key"), {"key": key}) == 0


async def test_verification_audit_failure_rolls_back_asset_and_idempotency(runtime):
    from unittest.mock import AsyncMock
    asset_id = await new_asset(runtime)
    key = uuid4()
    # Verification of these bytes rejects the synthetic expected digest. The audit
    # failure must undo even that terminal domain decision and its replay claim.
    with pytest.raises(RuntimeError):
        async with business(runtime) as (service, _):
            service.audit.write = AsyncMock(side_effect=RuntimeError("synthetic audit failure"))
            await service.complete(asset_id, key, FakeStorage())
    async with business(runtime) as (service, session):
        assert (await service.repository.find_by_id(asset_id)).status == "pending"
        assert await session.scalar(text("SELECT count(*) FROM idempotency_keys WHERE key=:key"), {"key": key}) == 0


@pytest.mark.parametrize("context", ["missing", "user_only", "foreign", "bad_user", "bad_org", "inactive_user", "removed_member", "inactive_org"])
async def test_asset_rls_requires_valid_active_context(runtime, context):
    async with runtime[0]() as session:
        if context != "missing":
            user = {"bad_user":"invalid", "inactive_user":"10000000-0000-0000-0000-000000000007",
                    "removed_member":"10000000-0000-0000-0000-000000000008"}.get(context,str(A))
            await session.execute(text("SELECT set_config('app.user_id',:value,true)"), {"value":user})
        if context not in {"missing","user_only"}:
            org = {"bad_org":"invalid", "foreign":str(ORG_B),
                   "inactive_org":"20000000-0000-0000-0000-000000000003"}.get(context,str(ORG_A))
            await session.execute(text("SELECT set_config('app.organization_id',:value,true)"), {"value":org})
        try:
            assert list(await session.scalars(select(Asset))) == []
        except DBAPIError as error:
            assert context in {"bad_user","bad_org"} and error.orig.sqlstate == "22P02"
