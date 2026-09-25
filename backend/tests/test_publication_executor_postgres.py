"""Opt-in B2-D PostgreSQL execution tests; Meta behavior is always a local fake."""

import asyncio
import base64
from datetime import UTC, datetime, timedelta
import os
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.modules.automation.executor import (
    FacebookPublicationExecutor,
    SQLAlchemyPublicationExecutionStore,
)
from backend.app.modules.automation.schemas import ExecutionStatus
from backend.app.modules.content.schemas import ContentScheduleRequest
from backend.app.modules.integrations.credentials import (
    CredentialBinding,
    CredentialCipher,
    FacebookCredentials,
)
from backend.app.modules.integrations.facebook_provider import (
    ProviderDisposition,
    ProviderResult,
    ReconciliationDisposition,
    ReconciliationResult,
)
from backend.tests.test_facebook_postgres import (
    A, B, ORG_A, ORG_B, create_connection, create_content, tenant,
)
from backend.tests.test_publication_postgres import service
from backend.tests.prompt10_postgres_safety import validated_prompt10_url


pytestmark = pytest.mark.anyio
NOW = datetime(2026, 9, 25, 18, 0, tzinfo=UTC)


def safe_url():
    try:
        return validated_prompt10_url(
            os.environ.get("PROMPT10_RUNTIME_URL", ""), "nightclub_api",
        )
    except ValueError:
        pytest.fail("PROMPT10_RUNTIME_URL: explicit local environment required (value withheld)", pytrace=False)


@pytest.fixture
async def runtime(request):
    if not request.config.getoption("--prompt10-postgres"):
        pytest.skip("Prompt 10 B2-D real PostgreSQL opt-in is not enabled")
    engine = create_async_engine(
        safe_url().set(drivername="postgresql+asyncpg"),
        pool_size=5, max_overflow=0, hide_parameters=True,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    statements = []
    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        normalized = statement.lstrip().upper()
        if normalized.startswith("INSERT INTO AUDIT_LOGS"):
            statements.append("audit")
    event.listen(engine.sync_engine, "before_cursor_execute", observe)
    try:
        yield factory, statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observe)
        await engine.dispose()


def cipher():
    encoded = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
    return CredentialCipher(encoded, 1)


class Provider:
    def __init__(self, publish=None, reconcile=None):
        self.publish_result = publish or ProviderResult(
            ProviderDisposition.SUCCEEDED,
            external_post_id="mocked-meta-post", provider_request_id="mocked-request",
            http_status=200,
        )
        self.reconcile_result = reconcile or ReconciliationResult(
            ReconciliationDisposition.NOT_FOUND, http_status=200,
        )
        self.calls = []

    async def publish_post(self, **values):
        self.calls.append("post")
        assert values["access_token"].get_secret_value() == "synthetic-b2d-token"
        return self.publish_result

    async def reconcile_post(self, **values):
        self.calls.append("reconcile")
        assert values["access_token"].get_secret_value() == "synthetic-b2d-token"
        return self.reconcile_result


async def setup_job(runtime, *, scheduled_for=None):
    factory, _ = runtime
    connection = await create_connection(factory)
    content, version = await create_content(factory, connection)
    crypto = cipher()
    async with tenant(factory) as session:
        page_id = await session.scalar(text(
            "SELECT external_account_id FROM platform_connections WHERE id=:id"
        ), {"id": connection})
        envelope = crypto.encrypt(
            FacebookCredentials(SecretStr("synthetic-b2d-token")),
            CredentialBinding(ORG_A, "facebook", page_id, 1),
        )
        await session.execute(text("""UPDATE platform_connections
            SET credentials_ciphertext=:ciphertext,credential_key_version=1,
                token_expires_at=:expires,status='active'
            WHERE id=:id"""), {
                "ciphertext": envelope, "expires": NOW + timedelta(days=30), "id": connection,
            })
    key = uuid4()
    async with tenant(factory) as session:
        status, _ = await service(session).schedule(
            content, key, ContentScheduleRequest(
                versionNo=1,
                scheduledFor=scheduled_for or NOW + timedelta(minutes=1),
            ),
        )
        assert status == 201
    async with tenant(factory) as session:
        job = await session.scalar(text(
            "SELECT id FROM publication_jobs WHERE content_item_id=:id"
        ), {"id": content})
    return content, version, job, crypto


def executor(runtime, provider, crypto, now):
    return FacebookPublicationExecutor(
        SQLAlchemyPublicationExecutionStore(runtime[0]),
        provider, crypto, clock=lambda: now,
    )


async def rows(runtime, content, job):
    async with tenant(runtime[0]) as session:
        item = (await session.execute(text("""SELECT status,published_at,external_post_id,
            last_error_code FROM content_items WHERE id=:id"""), {"id": content})).one()
        publication = (await session.execute(text("""SELECT status,attempt_count,lease_token,
            lease_expires_at,published_external_id,next_attempt_at
            FROM publication_jobs WHERE id=:id"""), {"id": job})).one()
        attempts = (await session.execute(text("""SELECT attempt_no,outcome,
            request_fingerprint,provider_response,error_code
            FROM publication_attempts WHERE publication_job_id=:id ORDER BY attempt_no"""),
            {"id": job})).all()
    return item, publication, attempts


async def test_real_postgres_success_attempt_state_and_atomic_audit(runtime):
    content, version, job, crypto = await setup_job(runtime)
    baseline_audits = runtime[1].count("audit")
    provider = Provider()
    result = await executor(runtime, provider, crypto, NOW + timedelta(minutes=1)).execute(
        ORG_A, A, job,
    )
    item, publication, attempts = await rows(runtime, content, job)
    assert result.status == ExecutionStatus.PUBLISHED
    assert provider.calls == ["post"]
    assert item.status == "published" and item.external_post_id == "mocked-meta-post"
    assert item.published_at == NOW + timedelta(minutes=1)
    assert publication.status == "succeeded" and publication.attempt_count == 1
    assert publication.published_external_id == "mocked-meta-post"
    assert publication.lease_token is None and publication.lease_expires_at is None
    assert publication.next_attempt_at is None
    assert len(attempts) == 1 and attempts[0].outcome == "succeeded"
    assert len(attempts[0].request_fingerprint) == 64
    # publication.started + publication.succeeded
    assert runtime[1].count("audit") - baseline_audits == 2


async def test_future_job_does_not_lease_decrypt_or_call_meta(runtime):
    content, _version, job, crypto = await setup_job(
        runtime, scheduled_for=NOW + timedelta(hours=1),
    )
    provider = Provider()
    result = await executor(runtime, provider, crypto, NOW).execute(ORG_A, A, job)
    item, publication, attempts = await rows(runtime, content, job)
    assert result.status == ExecutionStatus.NOT_DUE and provider.calls == []
    assert item.status == "scheduled" and publication.status == "pending"
    assert publication.attempt_count == 0 and attempts == []


async def test_real_postgres_two_workers_one_lease_and_one_post(runtime):
    _content, _version, job, crypto = await setup_job(runtime)
    provider = Provider()
    worker = executor(runtime, provider, crypto, NOW + timedelta(minutes=1))
    results = await asyncio.wait_for(asyncio.gather(
        worker.execute(ORG_A, A, job), worker.execute(ORG_A, A, job),
    ), timeout=15)
    assert [result.status for result in results].count(ExecutionStatus.PUBLISHED) == 1
    assert provider.calls == ["post"]
    _item, publication, attempts = await rows(runtime, _content, job)
    assert publication.attempt_count == 1 and len(attempts) == 1


async def test_retry_backoff_due_gate_and_second_attempt(runtime):
    content, _version, job, crypto = await setup_job(runtime)
    retry = Provider(ProviderResult(
        ProviderDisposition.RETRYABLE, http_status=429,
        error_code="META_4", error_message="Provider rate limited publication",
    ))
    first_at = NOW + timedelta(minutes=1)
    first = await executor(runtime, retry, crypto, first_at).execute(ORG_A, A, job)
    assert first.status == ExecutionStatus.RETRY_SCHEDULED and retry.calls == ["post"]
    _item, publication, attempts = await rows(runtime, content, job)
    assert publication.status == "retryable_failure"
    assert publication.next_attempt_at == first_at + timedelta(seconds=30)
    assert attempts[0].outcome == "retryable_failure"
    blocked = await executor(
        runtime, Provider(), crypto, first_at + timedelta(seconds=29),
    ).execute(ORG_A, A, job)
    assert blocked.status == ExecutionStatus.NOT_DUE
    success_provider = Provider()
    second = await executor(
        runtime, success_provider, crypto, first_at + timedelta(seconds=30),
    ).execute(ORG_A, A, job)
    assert second.status == ExecutionStatus.PUBLISHED and success_provider.calls == ["post"]
    _item, publication, attempts = await rows(runtime, content, job)
    assert publication.attempt_count == 2 and [row.attempt_no for row in attempts] == [1, 2]


async def test_second_backoff_and_third_attempt_exhaustion(runtime):
    content, _version, job, crypto = await setup_job(runtime)
    failure = ProviderResult(
        ProviderDisposition.RETRYABLE, http_status=503,
        error_code="META_2", error_message="Provider temporarily rejected publication",
    )
    first_at = NOW + timedelta(minutes=1)
    first_provider = Provider(failure)
    assert (await executor(runtime, first_provider, crypto, first_at).execute(
        ORG_A, A, job,
    )).status == ExecutionStatus.RETRY_SCHEDULED
    second_at = first_at + timedelta(seconds=30)
    second_provider = Provider(failure)
    assert (await executor(runtime, second_provider, crypto, second_at).execute(
        ORG_A, A, job,
    )).status == ExecutionStatus.RETRY_SCHEDULED
    _item, publication, attempts = await rows(runtime, content, job)
    assert publication.next_attempt_at == second_at + timedelta(seconds=120)
    assert publication.attempt_count == 2 and len(attempts) == 2
    third_provider = Provider(failure)
    third = await executor(
        runtime, third_provider, crypto, second_at + timedelta(seconds=120),
    ).execute(ORG_A, A, job)
    item, publication, attempts = await rows(runtime, content, job)
    assert third.status == ExecutionStatus.PERMANENT_FAILURE
    assert third_provider.calls == ["post"]
    assert publication.status == "permanent_failure" and publication.attempt_count == 3
    assert publication.next_attempt_at is None and item.status == "failed"
    assert item.last_error_code == "RETRY_LIMIT_EXHAUSTED"
    assert [row.outcome for row in attempts] == [
        "retryable_failure", "retryable_failure", "permanent_failure",
    ]


async def test_expired_connection_and_corrupt_ciphertext_never_call_meta(runtime):
    for mode in ("expired", "corrupt"):
        content, _version, job, crypto = await setup_job(runtime)
        async with tenant(runtime[0]) as session:
            if mode == "expired":
                await session.execute(text("""UPDATE platform_connections pc
                    SET token_expires_at=:expired FROM content_items ci
                    WHERE ci.connection_id=pc.id AND ci.id=:content"""), {
                        "expired": NOW, "content": content,
                    })
            else:
                await session.execute(text("""UPDATE platform_connections pc
                    SET credentials_ciphertext=decode('010203','hex') FROM content_items ci
                    WHERE ci.connection_id=pc.id AND ci.id=:content"""), {"content": content})
        provider = Provider()
        result = await executor(runtime, provider, crypto, NOW + timedelta(minutes=1)).execute(
            ORG_A, A, job,
        )
        item, publication, attempts = await rows(runtime, content, job)
        assert result.status == ExecutionStatus.PERMANENT_FAILURE and provider.calls == []
        assert publication.status == "permanent_failure" and publication.attempt_count == 0
        assert item.status == "failed" and attempts == []


async def test_ambiguous_result_reconciles_before_retry_without_second_post(runtime):
    content, _version, job, crypto = await setup_job(runtime)
    ambiguous = Provider(ProviderResult(
        ProviderDisposition.AMBIGUOUS,
        error_code="META_PUBLICATION_AMBIGUOUS",
        error_message="Publication result was ambiguous",
    ))
    first_at = NOW + timedelta(minutes=1)
    first = await executor(runtime, ambiguous, crypto, first_at).execute(ORG_A, A, job)
    assert first.status == ExecutionStatus.RETRY_SCHEDULED and ambiguous.calls == ["post"]
    matched = Provider(reconcile=ReconciliationResult(
        ReconciliationDisposition.MATCHED,
        external_post_id="reconciled-post", http_status=200,
    ))
    second = await executor(
        runtime, matched, crypto, first_at + timedelta(seconds=60),
    ).execute(ORG_A, A, job)
    assert second.status == ExecutionStatus.RECONCILED_PUBLISHED
    assert matched.calls == ["reconcile"]
    item, publication, attempts = await rows(runtime, content, job)
    assert item.status == "published" and item.external_post_id == "reconciled-post"
    assert publication.status == "succeeded" and publication.attempt_count == 1
    assert len(attempts) == 1


async def test_stale_worker_cannot_finalize_after_reclaim(runtime):
    content, _version, job, crypto = await setup_job(runtime)
    store = SQLAlchemyPublicationExecutionStore(runtime[0])
    prepared = await store.prepare(ORG_A, A, job, NOW + timedelta(minutes=1))
    worker_a = await store.begin_post(prepared.snapshot, NOW + timedelta(minutes=1))
    assert worker_a is not None
    worker_b = await store.prepare(ORG_A, A, job, NOW + timedelta(minutes=4))
    assert worker_b.snapshot is not None
    late = await store.finalize_provider(
        worker_a,
        ProviderResult(ProviderDisposition.SUCCEEDED, external_post_id="late-post"),
        NOW + timedelta(minutes=4),
    )
    assert late is None
    matched = await store.finalize_reconciliation(
        worker_b.snapshot,
        ReconciliationResult(
            ReconciliationDisposition.MATCHED, external_post_id="winner-post",
        ),
        NOW + timedelta(minutes=4),
    )
    assert matched == ExecutionStatus.RECONCILED_PUBLISHED
    item, publication, attempts = await rows(runtime, content, job)
    assert item.external_post_id == "winner-post" and publication.published_external_id == "winner-post"
    assert attempts[0].outcome == "retryable_failure"


async def test_cross_tenant_executor_cannot_lease_or_read_credentials(runtime):
    content, _version, job, crypto = await setup_job(runtime)
    provider = Provider()
    result = await executor(runtime, provider, crypto, NOW + timedelta(minutes=1)).execute(
        ORG_B, B, job,
    )
    assert result.status == ExecutionStatus.LEASE_UNAVAILABLE and provider.calls == []
    item, publication, attempts = await rows(runtime, content, job)
    assert item.status == "scheduled" and publication.status == "pending" and attempts == []
