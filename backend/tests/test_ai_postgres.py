"""Opt-in Prompt 9 real PostgreSQL tests; provider results are deterministic local values."""
import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
import os
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core import database
from backend.app.core.config import Settings
from backend.app.core.database_security import PROTECTED_TABLES
from backend.app.modules.ai.audit import AIAuditWriter
from backend.app.modules.ai.errors import AIBudgetExceeded, AIGenerationAlreadyApplied, AIGenerationNotFound
from backend.app.modules.ai.models import AIDailyUsage, AIGenerationRequest
from backend.app.modules.ai.provider import GenerationResult
from backend.app.modules.ai.repository import AIRepository
from backend.app.modules.ai.schemas import AIGenerationCreate, ProviderOutput
from backend.app.modules.ai.service import AIService
from backend.app.modules.content.audit import ContentAuditWriter
from backend.app.modules.content.models import ContentVersion
from backend.app.modules.content.repository import ContentRepository
from backend.app.modules.content.schemas import ContentCreate
from backend.app.modules.content.service import ContentService
from backend.app.modules.identity.dependencies import protected_session
from backend.app.modules.identity.policy import CurrentUser
from backend.app.modules.identity.repository import SQLAlchemyIdentityRepository
from backend.app.modules.identity.service import IdentityService
from backend.app.shared.idempotency import IdempotencyStore

pytestmark = pytest.mark.anyio
A = UUID("10000000-0000-0000-0000-000000000001")
B = UUID("10000000-0000-0000-0000-000000000002")
ORG_A = UUID("20000000-0000-0000-0000-000000000001")
ORG_B = UUID("20000000-0000-0000-0000-000000000002")
ROOT = Path(__file__).parents[2]


def safe_url():
    try:
        url = make_url(os.environ.get("PROMPT9_RUNTIME_URL", ""))
    except Exception:
        pytest.fail("PROMPT9_RUNTIME_URL: explicit local environment required (value withheld)", pytrace=False)
    if (url.host not in {"localhost", "127.0.0.1"} or url.database != "nightclub_ai_prompt9_test"
            or url.username != "nightclub_api" or url.port != 5432 or not url.password or url.query
            or url.drivername not in {"postgresql+asyncpg", "postgresql+psycopg"}):
        pytest.fail("PROMPT9_RUNTIME_URL: unsafe target (value withheld)", pytrace=False)
    return url


@pytest.fixture
async def runtime(request, monkeypatch):
    if not request.config.getoption("--prompt9-postgres"):
        pytest.skip("Prompt 9 real PostgreSQL opt-in is not enabled")
    engine = create_async_engine(safe_url().set(drivername="postgresql+asyncpg"),
        pool_size=getattr(request, "param", 1), max_overflow=0, hide_parameters=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionFactory", factory)
    try:
        yield factory
    finally:
        await engine.dispose()


def settings(budget="100"):
    return Settings(_env_file=None, openai_api_key="synthetic-local-only", openai_model="fixture-model",
                    ai_daily_budget_usd=Decimal(budget))


@asynccontextmanager
async def business(runtime, user=A, org=ORG_A, budget="100"):
    async with protected_session(CurrentUser(user)) as session:
        identity = SQLAlchemyIdentityRepository(session)
        context, _ = await IdentityService(identity,
            organization_context_installer=identity.establish_organization_context).organization(CurrentUser(user), org)
        yield AIService(context, AIRepository(session, org, user), ContentRepository(session, org),
            IdempotencyStore(session, context), AIAuditWriter(session, context, uuid4()),
            ContentAuditWriter(session, context, uuid4()), settings(budget)), session


async def new_content(runtime, user=A, org=ORG_A):
    async with protected_session(CurrentUser(user)) as session:
        identity = SQLAlchemyIdentityRepository(session)
        context, _ = await IdentityService(identity,
            organization_context_installer=identity.establish_organization_context).organization(CurrentUser(user), org)
        service = ContentService(context, ContentRepository(session, org), IdempotencyStore(session, context),
                                 ContentAuditWriter(session, context, uuid4()))
        _, result = await service.mutate("create", uuid4(), ContentCreate(
            platform="facebook", body="Prompt 9 local human draft",
            title="Human title", link_url="https://example.test/event").model_dump())
        return UUID(result["id"])


def request(content_id):
    return AIGenerationCreate.model_validate({"contentId": content_id, "templateKey": "facebook_event_v1",
        "brief": {"eventName": "Local fixture", "tone": "energetic", "language": "es-MX"}})


def result(cost="0.500000"):
    return GenerationResult(ProviderOutput.model_validate({
        "variants": [{"body": "Local variant one"}, {"body": "Local variant two"},
                     {"body": "Local variant three"}], "safetyFlags": []}),
        "local-provider-request", 10, 20, Decimal(cost))


async def accept_and_claim(runtime, content_id, user=A, org=ORG_A):
    async with business(runtime, user, org) as (service, _):
        generation_id = (await service.accept(uuid4(), request(content_id))).generation_id
    async with business(runtime, user, org) as (service, _):
        assert await service.claim_execution(generation_id) is not None
    return generation_id


async def succeed(runtime, generation_id, value=None, user=A, org=ORG_A):
    async with business(runtime, user, org) as (service, _):
        assert await service.succeed(generation_id, value or result()) is True


async def current_spend(runtime, user=A, org=ORG_A):
    async with business(runtime, user, org) as (_, session):
        return await session.scalar(select(AIDailyUsage.estimated_cost_usd)) or Decimal("0")


async def test_prompt9_catalog_21_force_tables_30_policies_and_narrow_grants(runtime):
    from importlib.util import module_from_spec, spec_from_file_location
    spec = spec_from_file_location("prompt9_catalog", ROOT / "backend/migrations/versions/20260923_0007_ai_generation_business_access.py")
    migration = module_from_spec(spec); spec.loader.exec_module(migration)
    async with runtime() as session:
        connection = await session.connection()
        await connection.run_sync(migration.verify_grants)
        await connection.run_sync(migration.verify_policies)
        rows = (await session.execute(text("""SELECT relname,relrowsecurity,relforcerowsecurity,pg_get_userbyid(relowner)
            FROM pg_class WHERE relnamespace='public'::regnamespace
            AND relname=ANY(CAST(:tables AS text[]))"""), {"tables": list(PROTECTED_TABLES)})).all()
        assert len(rows) == 21 and all(rls and force and owner != "nightclub_api" for _, rls, force, owner in rows)
        assert await session.scalar(text("SELECT count(*) FROM pg_policies WHERE schemaname='public'")) == 30
        assert not await session.scalar(text("SELECT has_table_privilege('nightclub_api','ai_daily_usage','DELETE')"))
        assert not await session.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_proc WHERE pronamespace='public'::regnamespace AND prosecdef)"))


async def test_initial_generation_output_is_sql_null_not_jsonb_null(runtime):
    content_id = await new_content(runtime)
    async with business(runtime) as (service, session):
        generation_id = (await service.accept(uuid4(), request(content_id))).generation_id
        row = (await session.execute(text("""
            SELECT output IS NULL,
                   output IS NOT DISTINCT FROM 'null'::jsonb,
                   provider_request_id IS NULL,
                   input_tokens IS NULL,
                   output_tokens IS NULL,
                   estimated_cost_usd IS NULL,
                   error_code IS NULL
            FROM ai_generation_requests
            WHERE id = :generation_id
        """), {"generation_id": generation_id})).one()
        assert row == (True, False, True, True, True, True, True)


async def test_creator_scoped_generation_and_organization_budget_ledger(runtime):
    before = await current_spend(runtime)
    content_a = await new_content(runtime)
    generation_id = await accept_and_claim(runtime, content_a)
    await succeed(runtime, generation_id, result("1.000000"))
    content_b = await new_content(runtime, user=B)
    async with business(runtime, B, ORG_A) as (service, session):
        with pytest.raises(AIGenerationNotFound):
            await service.read(generation_id)
        spend = await session.scalar(select(AIDailyUsage.estimated_cost_usd))
        assert spend == before + Decimal("1.000000")
        service.settings.ai_daily_budget_usd = spend
        with pytest.raises(AIBudgetExceeded):
            await service.accept(uuid4(), request(content_b))


async def test_foreign_ledger_hidden_and_current_tenant_visible(runtime):
    before = await current_spend(runtime, B, ORG_B)
    foreign_content = await new_content(runtime, user=B, org=ORG_B)
    foreign_generation = await accept_and_claim(runtime, foreign_content, B, ORG_B)
    await succeed(runtime, foreign_generation, result("0.250000"), B, ORG_B)
    async with business(runtime) as (_, session):
        rows = list(await session.scalars(select(AIDailyUsage)))
        assert all(row.organization_id == ORG_A for row in rows)
    async with business(runtime, B, ORG_B) as (_, session):
        assert await session.scalar(select(AIDailyUsage.estimated_cost_usd)) == before + Decimal("0.250000")


@pytest.mark.parametrize("statement,sqlstate", [
    ("DELETE FROM ai_daily_usage", "42501"),
    ("UPDATE ai_daily_usage SET organization_id='20000000-0000-0000-0000-000000000002'", "42501"),
    ("UPDATE ai_daily_usage SET usage_date=usage_date-1", "42501"),
    ("UPDATE ai_daily_usage SET estimated_cost_usd=estimated_cost_usd-0.1", "23514"),
])
async def test_ledger_cannot_delete_rekey_or_decrease(runtime, statement, sqlstate):
    content_id = await new_content(runtime)
    generation_id = await accept_and_claim(runtime, content_id)
    await succeed(runtime, generation_id)
    with pytest.raises(DBAPIError) as error:
        async with business(runtime) as (_, session):
            await session.execute(text(statement))
    assert error.value.orig.sqlstate == sqlstate


@pytest.mark.parametrize("target", ["succeeded", "failed", "queued", "cancelled"])
async def test_invalid_direct_generation_transitions_rejected(runtime, target):
    content_id = await new_content(runtime)
    with pytest.raises(DBAPIError) as error:
        async with business(runtime) as (service, session):
            generation_id = (await service.accept(uuid4(), request(content_id))).generation_id
            assignment = ("status=:status,output='{\"variants\":[]}'::jsonb,provider_request_id='x',"
                          "input_tokens=0,output_tokens=0,estimated_cost_usd=0") if target == "succeeded" else "status=:status"
            await session.execute(text(f"UPDATE ai_generation_requests SET {assignment} WHERE id=:id"),
                                  {"status": target, "id": generation_id})
    assert error.value.orig.sqlstate in {"23514", "42501"}


async def test_failed_generation_never_creates_ledger(runtime):
    before = await current_spend(runtime)
    content_id = await new_content(runtime)
    generation_id = await accept_and_claim(runtime, content_id)
    async with business(runtime) as (service, session):
        assert await service.fail(generation_id, "AI_PROVIDER_TIMEOUT") is True
    async with business(runtime) as (_, session):
        row = (await session.execute(text("""
            SELECT status,
                   output IS NULL,
                   output IS NOT DISTINCT FROM 'null'::jsonb,
                   error_code
            FROM ai_generation_requests
            WHERE id = :generation_id
        """), {"generation_id": generation_id})).one()
        assert row == ("failed", True, False, "AI_PROVIDER_TIMEOUT")
        assert (await session.scalar(select(AIDailyUsage.estimated_cost_usd)) or Decimal("0")) == before


async def test_success_charged_once_and_terminal_replay_no_double_charge(runtime):
    before = await current_spend(runtime)
    content_id = await new_content(runtime)
    generation_id = await accept_and_claim(runtime, content_id)
    await succeed(runtime, generation_id, result("0.750000"))
    async with business(runtime) as (service, session):
        assert await service.succeed(generation_id, result("0.750000")) is False
        assert await session.scalar(select(AIDailyUsage.estimated_cost_usd)) == before + Decimal("0.750000")


async def test_success_and_ledger_rollback_together_on_audit_failure(runtime):
    before = await current_spend(runtime)
    content_id = await new_content(runtime)
    generation_id = await accept_and_claim(runtime, content_id)
    with pytest.raises(RuntimeError):
        async with business(runtime) as (service, _):
            service.audit.write = AsyncMock(side_effect=RuntimeError("synthetic audit rollback"))
            await service.succeed(generation_id, result("0.900000"))
    async with business(runtime) as (_, session):
        assert await session.scalar(select(AIGenerationRequest.status).where(AIGenerationRequest.id == generation_id)) == "running"
        assert (await session.scalar(select(AIDailyUsage.estimated_cost_usd)) or Decimal("0")) == before


@pytest.mark.parametrize("runtime", [2], indirect=True)
async def test_concurrent_success_increments_do_not_lose_spend(runtime):
    before = await current_spend(runtime)
    first = await accept_and_claim(runtime, await new_content(runtime))
    second = await accept_and_claim(runtime, await new_content(runtime))
    await asyncio.wait_for(asyncio.gather(
        succeed(runtime, first, result("0.400000")),
        succeed(runtime, second, result("0.600000")),
    ), 15)
    async with business(runtime) as (_, session):
        assert await session.scalar(select(AIDailyUsage.estimated_cost_usd)) == before + Decimal("1.000000")


async def test_apply_real_ai_version_selected_body_and_single_use(runtime):
    content_id = await new_content(runtime)
    generation_id = await accept_and_claim(runtime, content_id)
    await succeed(runtime, generation_id)
    async with business(runtime) as (service, session):
        status, body = await service.apply(generation_id, uuid4(), 1)
        version = await session.scalar(select(ContentVersion).where(ContentVersion.ai_generation_id == generation_id))
        assert status == 200 and body["status"] == "draft"
        assert version.body == "Local variant two" and version.source == "ai"
        assert version.title == "Human title" and version.link_url == "https://example.test/event"
    async with business(runtime) as (service, session):
        with pytest.raises(AIGenerationAlreadyApplied):
            await service.apply(generation_id, uuid4(), 1)
        assert await session.scalar(text("SELECT count(*) FROM content_versions WHERE ai_generation_id=:id"), {"id": generation_id}) == 1


async def test_arbitrary_ai_body_is_rejected_by_rls(runtime):
    content_id = await new_content(runtime)
    generation_id = await accept_and_claim(runtime, content_id)
    await succeed(runtime, generation_id)
    with pytest.raises(DBAPIError) as error:
        async with business(runtime) as (_, session):
            await session.execute(text("""INSERT INTO content_versions
                (id,content_item_id,version_no,body,title,link_url,payload,source,ai_generation_id,created_by)
                VALUES (:id,:content,2,'forged body','Human title','https://example.test/event','{}','ai',:generation,:actor)"""),
                {"id": uuid4(), "content": content_id, "generation": generation_id, "actor": A})
    assert error.value.orig.sqlstate == "42501"
