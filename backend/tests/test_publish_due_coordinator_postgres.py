"""Opt-in B4 cross-role certification; never uses a real Meta provider."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
import hashlib
import hmac
import os
import time
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core.database_security import (
    establish_system_automation_context, verify_runtime_role,
)
from backend.app.core.scheduler_database import set_scheduler_transaction_read_only
from backend.app.core.config import Settings
from backend.app.main import create_app
from backend.app.modules.automation.coordinator import PublishDueCoordinator
from backend.app.modules.automation.discovery import DiscoveredPublicationJob, SQLAlchemyDueJobDiscovery
from backend.app.modules.automation.executor import (
    FacebookPublicationExecutor,
    SQLAlchemyPublicationExecutionStore,
)
from backend.app.modules.automation.schemas import ExecutionStatus
from backend.app.modules.automation.internal_auth import canonical_payload
from backend.app.modules.integrations.credentials import CredentialCipher
from backend.app.modules.integrations.facebook_provider import (
    ProviderDisposition, ProviderResult, ReconciliationDisposition, ReconciliationResult,
)
from backend.app.modules.identity.policy import SystemAutomationContext


pytestmark = pytest.mark.anyio
DATABASE = "nightclub_ai_prompt11_coordinator_test"
ORG_A = UUID("20000000-0000-0000-0000-000000000001")
ORG_B = UUID("20000000-0000-0000-0000-000000000002")
JOB_A = UUID("96200000-0000-0000-0000-000000000003")
JOB_PENDING_A = UUID("96200000-0000-0000-0000-000000000001")
JOB_PENDING_B = UUID("96200000-0000-0000-0000-000000000002")
JOB_INACTIVE = UUID("96200000-0000-0000-0000-000000000004")
JOB_CANCELLED = UUID("96200000-0000-0000-0000-000000000005")
JOB_FUTURE = UUID("96200000-0000-0000-0000-000000000006")
JOB_DURABLE = UUID("96200000-0000-0000-0000-000000000007")
HMAC_SECRET = "synthetic-b4-internal-hmac-secret-0001"


def guarded_url(name, role):
    try:
        url = make_url(os.environ.get(name, ""))
    except Exception:
        pytest.fail(f"{name}: explicit local environment required (value withheld)", pytrace=False)
    if (url.host not in {"localhost", "127.0.0.1"} or url.port != 5432
            or url.database != DATABASE or url.username != role or not url.password
            or url.query or url.drivername not in {"postgresql+asyncpg", "postgresql+psycopg"}):
        pytest.fail(f"{name}: unsafe target (value withheld)", pytrace=False)
    return url.set(drivername="postgresql+asyncpg")


@pytest.fixture
async def factories(request):
    if not request.config.getoption("--prompt11-coordinator-postgres"):
        pytest.skip("B4 PostgreSQL certification requires explicit opt-in")
    scheduler_engine = create_async_engine(guarded_url(
        "PROMPT11_COORDINATOR_SCHEDULER_URL", "nightclub_scheduler",
    ), pool_size=1, max_overflow=0, hide_parameters=True)
    runtime_engine = create_async_engine(guarded_url(
        "PROMPT11_COORDINATOR_RUNTIME_URL", "nightclub_api",
    ), pool_size=1, max_overflow=0, hide_parameters=True)
    try:
        yield (async_sessionmaker(scheduler_engine, expire_on_commit=False),
               async_sessionmaker(runtime_engine, expire_on_commit=False))
    finally:
        await scheduler_engine.dispose()
        await runtime_engine.dispose()


@asynccontextmanager
async def scheduler_scope(factory):
    async with factory() as session:
        async with session.begin():
            await set_scheduler_transaction_read_only(session)
            yield session


class FakeProvider:
    def __init__(self):
        self.calls = 0

    async def publish_post(self, **_values):
        self.calls += 1
        return ProviderResult(
            ProviderDisposition.SUCCEEDED, external_post_id="synthetic-b4-post",
            provider_request_id="synthetic-b4-request", http_status=200,
        )

    async def reconcile_post(self, **_values):
        return ReconciliationResult(ReconciliationDisposition.NOT_FOUND, http_status=200)


async def test_scheduler_discovers_cross_tenant_due_only_and_cannot_write_or_read_secrets(factories):
    scheduler, _ = factories
    # The production discoverer verifies the direct scheduler role inside this scope.
    discovery = SQLAlchemyDueJobDiscovery(
        session_scope=lambda: scheduler_scope(scheduler),
        clock=lambda: datetime.now(UTC),
    )
    rows = await discovery.discover(8)
    assert [row.publication_job_id for row in rows] == [
        JOB_PENDING_A, JOB_PENDING_B, JOB_INACTIVE, JOB_A, JOB_DURABLE,
    ]
    assert {ORG_A, ORG_B} <= {row.organization_id for row in rows}
    assert JOB_FUTURE not in {row.publication_job_id for row in rows}
    assert JOB_CANCELLED not in {row.publication_job_id for row in rows}
    async with scheduler_scope(scheduler) as session:
        with pytest.raises(DBAPIError) as error:
            await session.execute(text("SELECT credentials_ciphertext FROM public.platform_connections"))
    assert error.value.orig.sqlstate == "42501"
    async with scheduler_scope(scheduler) as session:
        with pytest.raises(DBAPIError) as error:
            await session.execute(text("UPDATE public.publication_jobs SET status='cancelled' WHERE id=:id"),
                                  {"id": JOB_A})
    assert error.value.orig.sqlstate == "25006" or error.value.orig.sqlstate == "42501"


async def test_scheduler_scope_is_closed_before_runtime_execution(factories):
    scheduler, runtime = factories
    closed = False

    @asynccontextmanager
    async def tracked_scope():
        nonlocal closed
        async with scheduler_scope(scheduler) as session:
            yield session
        closed = True

    provider = FakeProvider()
    real_executor = FacebookPublicationExecutor(
        SQLAlchemyPublicationExecutionStore(runtime), provider,
        CredentialCipher(SecretStr("AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"), 1),
    )

    class CheckedExecutor:
        async def execute_system(self, organization_id, publication_job_id):
            assert closed
            return await real_executor.execute_system(organization_id, publication_job_id)

    discovery = SQLAlchemyDueJobDiscovery(session_scope=tracked_scope,
                                          clock=lambda: datetime.now(UTC))
    selected = await discovery.discover(8)
    assert JOB_A in {row.publication_job_id for row in selected}
    # Cross-tenant calls cannot lease the selected job, even with system authority.
    assert (await real_executor.execute_system(ORG_B, JOB_A)).status == ExecutionStatus.LEASE_UNAVAILABLE
    result = await PublishDueCoordinator(
        discovery, CheckedExecutor(), batch_size=4, max_concurrency=2,
    ).run()
    assert result.processed == result.selected == 4
    assert result.published == 1  # expired leased job recovered via existing executor
    assert provider.calls == 1
    async with runtime() as session:
        async with session.begin():
            await verify_runtime_role(session, "nightclub_api")
            await establish_system_automation_context(session, SystemAutomationContext(ORG_A))
            statuses = (await session.execute(text("""SELECT id,status FROM public.publication_jobs
                WHERE id IN (:published,:future,:cancelled)"""), {
                    "published": JOB_A, "future": JOB_FUTURE, "cancelled": JOB_CANCELLED,
                })).all()
    assert dict(statuses) == {
        JOB_A: "succeeded", JOB_FUTURE: "pending", JOB_CANCELLED: "cancelled",
    }


async def test_endpoint_503_keeps_first_real_runtime_commit(factories):
    _scheduler, runtime = factories
    provider = FakeProvider()
    real_executor = FacebookPublicationExecutor(
        SQLAlchemyPublicationExecutionStore(runtime), provider,
        CredentialCipher(SecretStr("AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"), 1),
    )

    class Selected:
        async def discover(self, limit):
            assert limit == 2
            return (DiscoveredPublicationJob(ORG_A, JOB_DURABLE),
                    DiscoveredPublicationJob(ORG_A, JOB_FUTURE))

    class FailingSecond:
        async def execute_system(self, organization_id, job_id):
            if job_id == JOB_FUTURE:
                raise RuntimeError("synthetic private infrastructure detail")
            return await real_executor.execute_system(organization_id, job_id)

    app = create_app(Settings(_env_file=None, n8n_internal_secret=HMAC_SECRET))
    app.state.publish_due_coordinator_factory = lambda _request: PublishDueCoordinator(
        Selected(), FailingSecond(), batch_size=2, max_concurrency=1,
    )
    timestamp = str(int(time.time()))
    payload = canonical_payload(
        method="POST", path="/internal/automation/publish-due",
        timestamp=timestamp, raw_body=b"",
    )
    signature = "v1=" + hmac.new(HMAC_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        response = await client.post("/internal/automation/publish-due", headers={
            "X-N8N-Timestamp": timestamp, "X-N8N-Signature": signature,
        })
    assert response.status_code == 503
    assert response.json()["code"] == "AUTOMATION_UNAVAILABLE"
    assert "synthetic private" not in response.text
    assert provider.calls == 1
    async with runtime() as session:
        async with session.begin():
            await verify_runtime_role(session, "nightclub_api")
            await establish_system_automation_context(session, SystemAutomationContext(ORG_A))
            status = await session.scalar(text(
                "SELECT status FROM public.publication_jobs WHERE id=:job",
            ), {"job": JOB_DURABLE})
    assert status == "succeeded"
