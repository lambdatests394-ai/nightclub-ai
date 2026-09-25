"""Pure executor coordination tests; provider and persistence are local fakes."""

import base64
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

from pydantic import SecretStr
import pytest

from backend.app.modules.automation.executor import (
    AMBIGUOUS_RECONCILIATION_DELAY,
    LEASE_DURATION,
    MAX_PROVIDER_POST_ATTEMPTS,
    RETRY_BACKOFF,
    FacebookPublicationExecutor,
    request_fingerprint,
)
from backend.app.modules.automation.audit import PublicationAuditWriter
from backend.app.modules.automation.schemas import (
    CredentialEnvelope,
    ExecutionMode,
    ExecutionSnapshot,
    ExecutionStatus,
    Preparation,
)
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
from backend.app.modules.identity.policy import MemberRole, OrganizationContext


pytestmark = pytest.mark.anyio
NOW = datetime(2026, 9, 25, 18, 0, tzinfo=UTC)
TOKEN = "synthetic-executor-token"


def encoded_key():
    return base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")


def encrypted_snapshot(*, mode=ExecutionMode.POST, expires=None):
    organization, actor, content, version, job, connection = (uuid4() for _ in range(6))
    cipher = CredentialCipher(encoded_key(), 1)
    binding = CredentialBinding(organization, "facebook", "12345", 1)
    ciphertext = cipher.encrypt(FacebookCredentials(SecretStr(TOKEN)), binding)
    snapshot = ExecutionSnapshot(
        organization_id=organization,
        actor_id=actor,
        content_id=content,
        content_version_id=version,
        job_id=job,
        lease_token=uuid4(),
        connection=CredentialEnvelope(
            connection_id=connection, organization_id=organization,
            platform="facebook", page_id="12345", key_version=1,
            token_expires_at=expires, ciphertext=ciphertext,
        ),
        message="Exact approved body",
        link_url="https://example.test/event",
        request_fingerprint=request_fingerprint(
            "12345", version, "Exact approved body", "https://example.test/event",
        ),
        mode=mode,
        attempt_started_at=NOW - timedelta(minutes=1) if mode == ExecutionMode.RECONCILE else None,
    )
    return snapshot, cipher


class Store:
    def __init__(self, preparation):
        self.preparation = preparation
        self.calls = []
        self.provider_status = None
        self.reconciliation_status = None

    async def prepare(self, organization, actor, job, now):
        self.calls.append(("prepare", now))
        return self.preparation

    async def begin_post(self, snapshot, now):
        self.calls.append(("begin_post", now))
        return replace(
            snapshot, mode=ExecutionMode.POST, attempt_id=uuid4(),
            attempt_no=1, attempt_started_at=now,
        )

    async def finalize_provider(self, snapshot, result, now):
        self.calls.append(("finalize_provider", result.disposition, now))
        if self.provider_status is not None:
            return self.provider_status
        return {
            ProviderDisposition.SUCCEEDED: ExecutionStatus.PUBLISHED,
            ProviderDisposition.RETRYABLE: ExecutionStatus.RETRY_SCHEDULED,
            ProviderDisposition.AMBIGUOUS: ExecutionStatus.RETRY_SCHEDULED,
            ProviderDisposition.PERMANENT: ExecutionStatus.PERMANENT_FAILURE,
        }[result.disposition]

    async def finalize_preflight_failure(self, snapshot, error_code, now):
        self.calls.append(("finalize_preflight_failure", error_code, now))
        return True

    async def finalize_reconciliation(self, snapshot, result, now):
        self.calls.append(("finalize_reconciliation", result.disposition, now))
        if self.reconciliation_status is not None:
            return self.reconciliation_status
        return (
            ExecutionStatus.RECONCILED_PUBLISHED
            if result.disposition == ReconciliationDisposition.MATCHED
            else ExecutionStatus.PERMANENT_FAILURE
        )


class Provider:
    def __init__(self, publish=None, reconcile=None):
        self.publish_result = publish or ProviderResult(
            ProviderDisposition.SUCCEEDED, external_post_id="post-1", http_status=200,
        )
        self.reconcile_result = reconcile or ReconciliationResult(
            ReconciliationDisposition.NOT_FOUND, http_status=200,
        )
        self.calls = []

    async def publish_post(self, **values):
        self.calls.append(("publish", values))
        return self.publish_result

    async def reconcile_post(self, **values):
        self.calls.append(("reconcile", values))
        return self.reconcile_result


async def execute(snapshot, cipher, provider=None, store=None):
    provider = provider or Provider()
    store = store or Store(Preparation(snapshot=snapshot))
    result = await FacebookPublicationExecutor(
        store, provider, cipher, clock=lambda: NOW,
    ).execute(snapshot.organization_id, snapshot.actor_id, snapshot.job_id)
    return result, store, provider


async def test_success_has_three_phases_and_never_exposes_token():
    snapshot, cipher = encrypted_snapshot()
    result, store, provider = await execute(snapshot, cipher)
    assert result.status == ExecutionStatus.PUBLISHED and result.attempt_no == 1
    assert [call[0] for call in store.calls] == [
        "prepare", "begin_post", "finalize_provider",
    ]
    assert [call[0] for call in provider.calls] == ["publish"]
    assert provider.calls[0][1]["access_token"].get_secret_value() == TOKEN
    assert TOKEN not in repr(result) and TOKEN not in repr(snapshot)


async def test_executor_publishes_exact_job_bound_snapshot_payload():
    snapshot, cipher = encrypted_snapshot()
    snapshot = replace(
        snapshot,
        message="Immutable approved version body",
        link_url="https://example.test/approved-version",
    )
    result, _store, provider = await execute(snapshot, cipher)
    assert result.status == ExecutionStatus.PUBLISHED
    call = provider.calls[0][1]
    assert call["message"] == "Immutable approved version body"
    assert call["link_url"] == "https://example.test/approved-version"


@pytest.mark.parametrize("status", [ExecutionStatus.NOT_DUE, ExecutionStatus.LEASE_UNAVAILABLE])
async def test_prepare_terminal_result_does_not_decrypt_or_call_provider(status):
    snapshot, cipher = encrypted_snapshot()
    store, provider = Store(Preparation(status=status)), Provider()
    result = await FacebookPublicationExecutor(
        store, provider, cipher, clock=lambda: NOW,
    ).execute(snapshot.organization_id, snapshot.actor_id, snapshot.job_id)
    assert result.status == status
    assert [call[0] for call in store.calls] == ["prepare"] and provider.calls == []


@pytest.mark.parametrize("disposition,expected", [
    (ProviderDisposition.RETRYABLE, ExecutionStatus.RETRY_SCHEDULED),
    (ProviderDisposition.AMBIGUOUS, ExecutionStatus.RETRY_SCHEDULED),
    (ProviderDisposition.PERMANENT, ExecutionStatus.PERMANENT_FAILURE),
])
async def test_provider_failure_is_finalized_once_without_hidden_retry(disposition, expected):
    snapshot, cipher = encrypted_snapshot()
    provider = Provider(ProviderResult(disposition, error_code="SAFE_CODE"))
    result, store, provider = await execute(snapshot, cipher, provider)
    assert result.status == expected
    assert [call[0] for call in provider.calls] == ["publish"]
    assert [call[0] for call in store.calls].count("finalize_provider") == 1


async def test_expired_token_fails_before_attempt_and_provider_call():
    snapshot, cipher = encrypted_snapshot(expires=NOW)
    result, store, provider = await execute(snapshot, cipher)
    assert result.status == ExecutionStatus.PERMANENT_FAILURE
    assert result.error_code == "FACEBOOK_TOKEN_EXPIRED"
    assert [call[0] for call in store.calls] == ["prepare", "finalize_preflight_failure"]
    assert provider.calls == []


async def test_corrupt_ciphertext_fails_safely_without_meta_call():
    snapshot, cipher = encrypted_snapshot()
    snapshot = replace(
        snapshot,
        connection=replace(snapshot.connection, ciphertext=b"corrupt-ciphertext"),
    )
    result, store, provider = await execute(snapshot, cipher)
    assert result.status == ExecutionStatus.PERMANENT_FAILURE
    assert result.error_code == "FACEBOOK_CREDENTIAL_DECRYPTION_FAILED"
    assert provider.calls == []
    assert b"corrupt-ciphertext" not in repr(result).encode()


async def test_reconciliation_match_never_posts_again():
    snapshot, cipher = encrypted_snapshot(mode=ExecutionMode.RECONCILE)
    provider = Provider(reconcile=ReconciliationResult(
        ReconciliationDisposition.MATCHED, external_post_id="matched-post", http_status=200,
    ))
    result, store, provider = await execute(snapshot, cipher, provider)
    assert result.status == ExecutionStatus.RECONCILED_PUBLISHED
    assert [call[0] for call in provider.calls] == ["reconcile"]
    assert [call[0] for call in store.calls] == ["prepare", "finalize_reconciliation"]


async def test_reconciliation_not_found_occurs_before_one_new_post():
    snapshot, cipher = encrypted_snapshot(mode=ExecutionMode.RECONCILE)
    result, store, provider = await execute(snapshot, cipher)
    assert result.status == ExecutionStatus.PUBLISHED
    assert [call[0] for call in provider.calls] == ["reconcile", "publish"]
    assert [call[0] for call in store.calls] == [
        "prepare", "begin_post", "finalize_provider",
    ]


@pytest.mark.parametrize("disposition", [
    ReconciliationDisposition.AMBIGUOUS_MULTIPLE,
    ReconciliationDisposition.INCONCLUSIVE,
])
async def test_unsafe_reconciliation_never_posts(disposition):
    snapshot, cipher = encrypted_snapshot(mode=ExecutionMode.RECONCILE)
    provider = Provider(reconcile=ReconciliationResult(
        disposition, error_code="RECONCILIATION_UNSAFE",
    ))
    result, store, provider = await execute(snapshot, cipher, provider)
    assert result.status == ExecutionStatus.PERMANENT_FAILURE
    assert [call[0] for call in provider.calls] == ["reconcile"]


def test_retry_lease_and_fingerprint_contract_is_deterministic_and_secret_free():
    version = uuid4()
    first = request_fingerprint("123", version, "body", "https://example.test")
    second = request_fingerprint("123", version, "body", "https://example.test")
    assert first == second and len(first) == 64 and TOKEN not in first
    assert MAX_PROVIDER_POST_ATTEMPTS == 3
    assert RETRY_BACKOFF == {1: timedelta(seconds=30), 2: timedelta(seconds=120)}
    assert AMBIGUOUS_RECONCILIATION_DELAY == timedelta(seconds=60)
    assert LEASE_DURATION == timedelta(seconds=120)


def test_persisted_provider_messages_are_server_owned():
    from backend.app.modules.automation.executor import _server_owned_error_message

    provider_controlled = "raw provider text must never be persisted"
    for disposition in (
        ProviderDisposition.RETRYABLE,
        ProviderDisposition.AMBIGUOUS,
        ProviderDisposition.PERMANENT,
    ):
        message = _server_owned_error_message(disposition)
        assert provider_controlled not in message
        assert message in {
            "Provider publication will be retried",
            "Publication result was ambiguous",
            "Provider rejected publication",
        }


async def test_execution_audit_is_structural_and_redacted():
    session = AsyncMock()
    organization, actor, content, job = (uuid4() for _ in range(4))
    writer = PublicationAuditWriter(
        session,
        OrganizationContext(organization, actor, MemberRole.OWNER),
        uuid4(),
    )
    await writer.execution(
        "publication.succeeded", content_id=content, job_id=job,
        version_no=2, attempt_no=1, status="succeeded", outcome="succeeded",
        reconciled=False, provider_http_status=200,
    )
    statement = session.execute.await_args.args[0]
    params = statement.compile().params
    assert params["action"] == "publication.succeeded"
    assert params["entity_type"] == "publication" and params["entity_id"] == job
    after = params["after"]
    assert set(after) == {
        "organizationId", "contentId", "versionNo", "jobId", "attemptNo",
        "status", "outcome", "reconciled", "providerHttpStatus", "errorCode",
        "nextAttemptAt",
    }
    assert not ({"body", "message", "title", "link", "token", "ciphertext",
                 "providerResponse"} & set(after))
