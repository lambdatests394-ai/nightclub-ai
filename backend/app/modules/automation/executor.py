"""Three-phase Facebook publication executor; provider I/O never holds a DB transaction."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.orm.attributes import set_committed_value

from backend.app.core import database
from backend.app.core.database_security import establish_user_context, verify_runtime_role
from backend.app.modules.automation.audit import PublicationAuditWriter
from backend.app.modules.automation.models import PublicationAttempt, PublicationJob
from backend.app.modules.automation.schemas import (
    CredentialEnvelope,
    ExecutionMode,
    ExecutionSnapshot,
    ExecutionStatus,
    ExecutorResult,
    Preparation,
)
from backend.app.modules.content.models import ContentAsset, ContentItem, ContentVersion
from backend.app.modules.content.state_machine import ContentState, ContentStateMachine
from backend.app.modules.identity.policy import CurrentUser, Permission, require_permission
from backend.app.modules.identity.repository import SQLAlchemyIdentityRepository
from backend.app.modules.identity.service import IdentityService
from backend.app.modules.integrations.credentials import CredentialBinding, CredentialCipher
from backend.app.modules.integrations.errors import FacebookCredentialError
from backend.app.modules.integrations.facebook_provider import (
    FacebookPublicationProvider,
    ProviderDisposition,
    ProviderResult,
    ReconciliationDisposition,
    ReconciliationResult,
)
from backend.app.modules.integrations.models import PlatformConnection
from backend.app.modules.integrations.repository import FacebookConnectionRepository


MAX_PROVIDER_POST_ATTEMPTS = 3
LEASE_DURATION = timedelta(seconds=120)
AMBIGUOUS_RECONCILIATION_DELAY = timedelta(seconds=60)
RETRY_BACKOFF = {1: timedelta(seconds=30), 2: timedelta(seconds=120)}


class PublicationExecutionStore(Protocol):
    async def prepare(
        self, organization_id: UUID, actor_id: UUID, job_id: UUID, now: datetime,
    ) -> Preparation: ...

    async def begin_post(
        self, snapshot: ExecutionSnapshot, now: datetime,
    ) -> ExecutionSnapshot | None: ...

    async def finalize_provider(
        self, snapshot: ExecutionSnapshot, result: ProviderResult, now: datetime,
    ) -> ExecutionStatus | None: ...

    async def finalize_preflight_failure(
        self, snapshot: ExecutionSnapshot, error_code: str, now: datetime,
    ) -> bool: ...

    async def finalize_reconciliation(
        self, snapshot: ExecutionSnapshot, result: ReconciliationResult, now: datetime,
    ) -> ExecutionStatus | None: ...


def request_fingerprint(
        page_id: str, content_version_id: UUID, message: str,
        link_url: str | None) -> str:
    payload = json.dumps({
        "provider": "facebook",
        "pageId": page_id,
        "contentVersionId": str(content_version_id),
        "message": message,
        "link": link_url,
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _valid_link(value: str | None) -> bool:
    if value is None:
        return True
    try:
        parsed = urlsplit(value)
    except (TypeError, UnicodeError, ValueError):
        return False
    return bool(
        isinstance(value, str) and parsed.scheme == "https" and parsed.hostname
        and parsed.username is None and parsed.password is None
        and not any(character.isspace() or ord(character) < 32 for character in value)
    )


def _provider_response(
        result: ProviderResult | ReconciliationResult, *, reconciliation=None,
        retry_after: int | None = None) -> dict:
    value = {
        "status_category": result.disposition.value,
        "http_status": result.http_status,
        "provider_request_id": result.provider_request_id,
        "normalized_error_code": result.error_code,
        "reconciliation_result": reconciliation,
        "retry_after_seconds": retry_after,
    }
    return {key: item for key, item in value.items() if item is not None}


def _safe_code(value: str | None, fallback: str) -> str:
    if not value:
        return fallback
    normalized = "".join(
        character for character in value.upper()
        if character.isascii() and (character.isalnum() or character == "_")
    )
    return normalized[:128] or fallback


def _server_owned_error_message(
        disposition: ProviderDisposition, *, exhausted: bool = False) -> str:
    """Return a fixed persistence-safe message; never retain provider text."""
    if exhausted:
        return "Provider retry limit was exhausted"
    if disposition == ProviderDisposition.AMBIGUOUS:
        return "Publication result was ambiguous"
    if disposition == ProviderDisposition.RETRYABLE:
        return "Provider publication will be retried"
    return "Provider rejected publication"


class SQLAlchemyPublicationExecutionStore:
    """Own short root transactions; repository methods never commit or roll back."""

    def __init__(self, session_factory=None, *, expected_role: str = "nightclub_api"):
        self._factory = session_factory
        self.expected_role = expected_role

    @asynccontextmanager
    async def _transaction(self, organization_id: UUID, actor_id: UUID):
        factory = self._factory or database.SessionFactory
        if factory is None:
            raise RuntimeError("Database session factory is unavailable")
        async with factory() as session:
            async with session.begin():
                user = CurrentUser(actor_id)
                await verify_runtime_role(session, self.expected_role)
                await establish_user_context(session, user)
                identity = SQLAlchemyIdentityRepository(session)
                context, _ = await IdentityService(
                    identity,
                    organization_context_installer=identity.establish_organization_context,
                ).organization(user, organization_id)
                require_permission(context, Permission.FACEBOOK_PUBLISH)
                yield session, context

    @staticmethod
    async def _locked_job(session, job_id: UUID):
        return await session.scalar(
            select(PublicationJob).where(PublicationJob.id == job_id).with_for_update()
        )

    @staticmethod
    async def _latest_attempt(session, job_id: UUID):
        return await session.scalar(select(PublicationAttempt).where(
            PublicationAttempt.publication_job_id == job_id,
        ).order_by(PublicationAttempt.attempt_no.desc()).limit(1))

    @staticmethod
    async def _job_transition(session, job: PublicationJob, status: str, **values):
        result = await session.execute(update(PublicationJob).where(
            PublicationJob.id == job.id,
            PublicationJob.status == job.status,
        ).values(status=status, **values))
        if result.rowcount != 1:
            return False
        set_committed_value(job, "status", status)
        for key, value in values.items():
            set_committed_value(job, key, value)
        return True

    @staticmethod
    async def _content_transition(session, item: ContentItem, status: str, **values):
        result = await session.execute(update(ContentItem).where(
            ContentItem.id == item.id,
            ContentItem.status == item.status,
        ).values(status=status, **values))
        if result.rowcount != 1:
            return False
        set_committed_value(item, "status", status)
        for key, value in values.items():
            set_committed_value(item, key, value)
        return True

    async def prepare(
            self, organization_id: UUID, actor_id: UUID, job_id: UUID,
            now: datetime) -> Preparation:
        lease_token = uuid4()
        async with self._transaction(organization_id, actor_id) as (session, context):
            job = await self._locked_job(session, job_id)
            if job is None:
                return Preparation(ExecutionStatus.LEASE_UNAVAILABLE)
            item = await session.scalar(select(ContentItem).where(
                ContentItem.id == job.content_item_id,
            ).with_for_update())
            if item is None or item.organization_id != organization_id:
                return Preparation(ExecutionStatus.LEASE_UNAVAILABLE)

            latest = await self._latest_attempt(session, job.id)
            reconcile = False
            if job.status == "pending":
                if job.scheduled_for > now:
                    return Preparation(ExecutionStatus.NOT_DUE)
            elif job.status == "retryable_failure":
                if job.next_attempt_at is None or job.next_attempt_at > now:
                    return Preparation(ExecutionStatus.NOT_DUE)
                reconcile = bool(
                    latest is not None and isinstance(latest.provider_response, dict)
                    and latest.provider_response.get("reconciliation_result") == "required"
                )
                if job.attempt_count >= MAX_PROVIDER_POST_ATTEMPTS and not reconcile:
                    if not await self._job_transition(
                            session, job, "leased", lease_token=lease_token,
                            lease_expires_at=now + LEASE_DURATION):
                        return Preparation(ExecutionStatus.LEASE_UNAVAILABLE)
                    await self._terminal_without_attempt(
                        session, context, item, job, "RETRY_LIMIT_EXHAUSTED", now,
                    )
                    return Preparation(ExecutionStatus.PERMANENT_FAILURE)
            elif job.status in {"leased", "publishing"}:
                if job.lease_expires_at is not None and job.lease_expires_at > now:
                    return Preparation(ExecutionStatus.LEASE_UNAVAILABLE)
                reconcile = bool(latest is not None and latest.outcome == "in_progress")
                if reconcile:
                    await session.execute(update(PublicationAttempt).where(
                        PublicationAttempt.id == latest.id,
                        PublicationAttempt.outcome == "in_progress",
                    ).values(
                        outcome="retryable_failure", finished_at=now,
                        provider_response={
                            "status_category": "ambiguous",
                            "normalized_error_code": "STALE_EXECUTION",
                            "reconciliation_result": "required",
                            "retry_after_seconds": 0,
                        },
                        error_code="STALE_EXECUTION",
                        error_message="Publication result was ambiguous",
                    ))
                if not await self._job_transition(
                        session, job, "retryable_failure", lease_token=None,
                        lease_expires_at=None, next_attempt_at=now):
                    return Preparation(ExecutionStatus.LEASE_UNAVAILABLE)
            else:
                return Preparation(ExecutionStatus.LEASE_UNAVAILABLE)

            if not await self._job_transition(
                    session, job, "leased", lease_token=lease_token,
                    lease_expires_at=now + LEASE_DURATION):
                return Preparation(ExecutionStatus.LEASE_UNAVAILABLE)

            version = (await session.execute(select(
                ContentVersion.id, ContentVersion.content_item_id,
                ContentVersion.version_no, ContentVersion.body, ContentVersion.link_url,
            ).where(
                ContentVersion.id == job.content_version_id,
                ContentVersion.content_item_id == item.id,
            ))).one_or_none()
            assets = await session.scalar(select(ContentAsset.asset_id).where(
                ContentAsset.content_version_id == job.content_version_id,
            ).limit(1))
            if (version is None or assets is not None or item.platform != "facebook"
                    or item.connection_id is None or item.status not in {"scheduled", "publishing"}
                    or item.approved_version_no != version.version_no
                    or not _valid_link(version.link_url)):
                await self._terminal_without_attempt(
                    session, context, item, job, "PUBLICATION_PRECONDITION_FAILED", now,
                )
                return Preparation(ExecutionStatus.PERMANENT_FAILURE)

            connections = FacebookConnectionRepository(
                session, organization_id, actor_id,
            )
            material = await connections.get_facebook_credentials(
                item.connection_id, lock=True,
            )
            if (material is None or material.status != "active"
                    or material.platform != "facebook"
                    or material.organization_id != organization_id
                    or material.connection_id != item.connection_id):
                await self._terminal_without_attempt(
                    session, context, item, job, "FACEBOOK_CONNECTION_UNAVAILABLE", now,
                )
                return Preparation(ExecutionStatus.PERMANENT_FAILURE)

            mode = ExecutionMode.RECONCILE if reconcile else ExecutionMode.POST
            started_at = latest.started_at if reconcile and latest is not None else None
            snapshot = ExecutionSnapshot(
                organization_id=organization_id,
                actor_id=actor_id,
                content_id=item.id,
                content_version_id=version.id,
                job_id=job.id,
                lease_token=lease_token,
                connection=CredentialEnvelope(
                    connection_id=material.connection_id,
                    organization_id=material.organization_id,
                    platform=material.platform,
                    page_id=material.external_account_id,
                    key_version=material.credential_key_version,
                    token_expires_at=material.token_expires_at,
                    ciphertext=bytes(material.credentials_ciphertext),
                ),
                message=version.body,
                link_url=version.link_url,
                request_fingerprint=request_fingerprint(
                    material.external_account_id, version.id, version.body, version.link_url,
                ),
                mode=mode,
                attempt_started_at=started_at,
            )
            return Preparation(snapshot=snapshot)

    async def _terminal_without_attempt(
            self, session, context, item, job, error_code: str, now: datetime):
        if job.status == "leased":
            await self._job_transition(session, job, "publishing")
        if item.status == "scheduled":
            await self._content_transition(session, item, "publishing")
        await self._job_transition(
            session, job, "permanent_failure", lease_token=None,
            lease_expires_at=None, next_attempt_at=None,
        )
        if item.status == "publishing":
            await self._content_transition(
                session, item, "failed", last_error_code=error_code,
            )
        await PublicationAuditWriter(session, context, uuid4()).execution(
            "publication.permanent_failure", content_id=item.id,
            job_id=job.id, version_no=item.approved_version_no,
            attempt_no=None, status="permanent_failure",
            outcome="permanent_failure", reconciled=False,
            error_code=error_code,
        )

    async def begin_post(
            self, snapshot: ExecutionSnapshot, now: datetime) -> ExecutionSnapshot | None:
        async with self._transaction(
                snapshot.organization_id, snapshot.actor_id) as (session, context):
            job = await self._locked_job(session, snapshot.job_id)
            if (job is None or job.status != "leased" or job.lease_token != snapshot.lease_token
                    or job.lease_expires_at is None or job.lease_expires_at <= now
                    or job.attempt_count >= MAX_PROVIDER_POST_ATTEMPTS):
                return None
            item = await session.scalar(select(ContentItem).where(
                ContentItem.id == snapshot.content_id,
            ).with_for_update())
            if item is None or item.status not in {"scheduled", "publishing"}:
                return None
            attempt_no = job.attempt_count + 1
            attempt_id = uuid4()
            if item.status == "scheduled":
                state = ContentState(
                    item.id, item.created_by, item.status,
                    item.current_version_no, item.approved_version_no,
                )
                await ContentStateMachine.begin_publishing(state)
                if not await self._content_transition(session, item, state.status):
                    return None
            if not await self._job_transition(
                    session, job, "publishing", attempt_count=attempt_no):
                return None
            await session.execute(insert(PublicationAttempt.__table__).inline().values(
                id=attempt_id, publication_job_id=job.id, attempt_no=attempt_no,
                started_at=now, request_fingerprint=snapshot.request_fingerprint,
                outcome="in_progress",
            ))
            await PublicationAuditWriter(session, context, uuid4()).execution(
                "publication.started", content_id=item.id, job_id=job.id,
                version_no=item.approved_version_no, attempt_no=attempt_no,
                status="publishing", outcome="in_progress", reconciled=False,
            )
            return replace(
                snapshot, mode=ExecutionMode.POST,
                attempt_id=attempt_id, attempt_no=attempt_no,
                attempt_started_at=now,
            )

    async def finalize_preflight_failure(
            self, snapshot: ExecutionSnapshot, error_code: str,
            now: datetime) -> bool:
        async with self._transaction(
                snapshot.organization_id, snapshot.actor_id) as (session, context):
            job = await self._locked_job(session, snapshot.job_id)
            if (job is None or job.status != "leased"
                    or job.lease_token != snapshot.lease_token):
                return False
            item = await session.scalar(select(ContentItem).where(
                ContentItem.id == snapshot.content_id,
            ).with_for_update())
            if item is None:
                return False
            await self._terminal_without_attempt(
                session, context, item, job, error_code, now,
            )
            connections = FacebookConnectionRepository(
                session, snapshot.organization_id, snapshot.actor_id,
            )
            await connections.mark_facebook_connection_error(
                snapshot.connection.connection_id, error_code, now,
                terminal_status=(
                    "expired" if error_code == "FACEBOOK_TOKEN_EXPIRED" else "error"
                ),
            )
            return True

    async def finalize_provider(
            self, snapshot: ExecutionSnapshot, result: ProviderResult,
            now: datetime) -> ExecutionStatus | None:
        if snapshot.attempt_id is None or snapshot.attempt_no is None:
            return None
        async with self._transaction(
                snapshot.organization_id, snapshot.actor_id) as (session, context):
            job = await self._locked_job(session, snapshot.job_id)
            if (job is None or job.status != "publishing"
                    or job.lease_token != snapshot.lease_token):
                return None
            attempt = await session.scalar(select(PublicationAttempt).where(
                PublicationAttempt.id == snapshot.attempt_id,
                PublicationAttempt.publication_job_id == job.id,
            ).with_for_update())
            item = await session.scalar(select(ContentItem).where(
                ContentItem.id == snapshot.content_id,
            ).with_for_update())
            if attempt is None or attempt.outcome != "in_progress" or item is None:
                return None

            disposition = result.disposition
            retry_after = None
            error_code = _safe_code(result.error_code, "META_PUBLICATION_FAILED")
            if disposition == ProviderDisposition.SUCCEEDED:
                if result.external_post_id is None:
                    return None
                attempt_outcome, job_status = "succeeded", "succeeded"
                await session.execute(update(PublicationAttempt).where(
                    PublicationAttempt.id == attempt.id,
                    PublicationAttempt.outcome == "in_progress",
                ).values(
                    finished_at=now, provider_request_id=result.provider_request_id,
                    provider_response=_provider_response(result), outcome=attempt_outcome,
                    error_code=None, error_message=None,
                ))
                await self._job_transition(
                    session, job, job_status, lease_token=None, lease_expires_at=None,
                    published_external_id=result.external_post_id, next_attempt_at=None,
                )
                await self._content_transition(
                    session, item, "published", published_at=now,
                    external_post_id=result.external_post_id, last_error_code=None,
                )
                await PublicationAuditWriter(session, context, uuid4()).execution(
                    "publication.succeeded", content_id=item.id, job_id=job.id,
                    version_no=item.approved_version_no, attempt_no=attempt.attempt_no,
                    status="succeeded", outcome="succeeded", reconciled=False,
                    provider_http_status=result.http_status,
                )
                return ExecutionStatus.PUBLISHED

            ambiguous = disposition == ProviderDisposition.AMBIGUOUS
            retryable = disposition == ProviderDisposition.RETRYABLE or ambiguous
            exhausted = retryable and attempt.attempt_no >= MAX_PROVIDER_POST_ATTEMPTS
            if retryable and not exhausted:
                retry_after = (
                    AMBIGUOUS_RECONCILIATION_DELAY if ambiguous
                    else RETRY_BACKOFF[attempt.attempt_no]
                )
                next_attempt = now + retry_after
                provider = _provider_response(
                    result,
                    reconciliation="required" if ambiguous else None,
                    retry_after=int(retry_after.total_seconds()),
                )
                await session.execute(update(PublicationAttempt).where(
                    PublicationAttempt.id == attempt.id,
                    PublicationAttempt.outcome == "in_progress",
                ).values(
                    finished_at=now, provider_request_id=result.provider_request_id,
                    provider_response=provider, outcome="retryable_failure",
                    error_code=error_code,
                    error_message=_server_owned_error_message(disposition),
                ))
                await self._job_transition(
                    session, job, "retryable_failure", lease_token=None,
                    lease_expires_at=None, next_attempt_at=next_attempt,
                )
                await PublicationAuditWriter(session, context, uuid4()).execution(
                    "publication.retryable_failure", content_id=item.id, job_id=job.id,
                    version_no=item.approved_version_no, attempt_no=attempt.attempt_no,
                    status="retryable_failure", outcome="retryable_failure",
                    reconciled=False, provider_http_status=result.http_status,
                    error_code=error_code, next_attempt_at=next_attempt,
                )
                return ExecutionStatus.RETRY_SCHEDULED

            if exhausted:
                error_code = "RETRY_LIMIT_EXHAUSTED"
            await session.execute(update(PublicationAttempt).where(
                PublicationAttempt.id == attempt.id,
                PublicationAttempt.outcome == "in_progress",
            ).values(
                finished_at=now, provider_request_id=result.provider_request_id,
                provider_response=_provider_response(result), outcome="permanent_failure",
                error_code=error_code,
                error_message=_server_owned_error_message(
                    disposition, exhausted=exhausted,
                ),
            ))
            await self._job_transition(
                session, job, "permanent_failure", lease_token=None,
                lease_expires_at=None, next_attempt_at=None,
            )
            await self._content_transition(
                session, item, "failed", last_error_code=error_code,
            )
            if error_code == "META_AUTHENTICATION_FAILED":
                connections = FacebookConnectionRepository(
                    session, snapshot.organization_id, snapshot.actor_id,
                )
                await connections.mark_facebook_connection_error(
                    snapshot.connection.connection_id, error_code, now,
                    terminal_status="error",
                )
            await PublicationAuditWriter(session, context, uuid4()).execution(
                "publication.permanent_failure", content_id=item.id, job_id=job.id,
                version_no=item.approved_version_no, attempt_no=attempt.attempt_no,
                status="permanent_failure", outcome="permanent_failure",
                reconciled=False, provider_http_status=result.http_status,
                error_code=error_code,
            )
            return ExecutionStatus.PERMANENT_FAILURE

    async def finalize_reconciliation(
            self, snapshot: ExecutionSnapshot, result: ReconciliationResult,
            now: datetime) -> ExecutionStatus | None:
        if result.disposition == ReconciliationDisposition.NOT_FOUND:
            return None
        async with self._transaction(
                snapshot.organization_id, snapshot.actor_id) as (session, context):
            job = await self._locked_job(session, snapshot.job_id)
            if (job is None or job.status != "leased"
                    or job.lease_token != snapshot.lease_token):
                return None
            item = await session.scalar(select(ContentItem).where(
                ContentItem.id == snapshot.content_id,
            ).with_for_update())
            if item is None or item.status != "publishing":
                return None
            if not await self._job_transition(session, job, "publishing"):
                return None
            if result.disposition == ReconciliationDisposition.MATCHED:
                if result.external_post_id is None:
                    return None
                await self._job_transition(
                    session, job, "succeeded", lease_token=None,
                    lease_expires_at=None, published_external_id=result.external_post_id,
                    next_attempt_at=None,
                )
                await self._content_transition(
                    session, item, "published", published_at=now,
                    external_post_id=result.external_post_id, last_error_code=None,
                )
                await PublicationAuditWriter(session, context, uuid4()).execution(
                    "publication.succeeded", content_id=item.id, job_id=job.id,
                    version_no=item.approved_version_no,
                    attempt_no=job.attempt_count, status="succeeded",
                    outcome="succeeded", reconciled=True,
                    provider_http_status=result.http_status,
                )
                return ExecutionStatus.RECONCILED_PUBLISHED

            error_code = _safe_code(
                result.error_code,
                "RECONCILIATION_INCONCLUSIVE",
            )
            await self._job_transition(
                session, job, "permanent_failure", lease_token=None,
                lease_expires_at=None, next_attempt_at=None,
            )
            await self._content_transition(
                session, item, "failed", last_error_code=error_code,
            )
            await PublicationAuditWriter(session, context, uuid4()).execution(
                "publication.permanent_failure", content_id=item.id, job_id=job.id,
                version_no=item.approved_version_no,
                attempt_no=job.attempt_count, status="permanent_failure",
                outcome="permanent_failure", reconciled=True,
                provider_http_status=result.http_status, error_code=error_code,
            )
            return ExecutionStatus.PERMANENT_FAILURE


class FacebookPublicationExecutor:
    """Coordinates database phases around one explicit provider operation."""

    def __init__(
            self, store: PublicationExecutionStore,
            provider: FacebookPublicationProvider,
            credential_cipher: CredentialCipher,
            *, clock=None):
        self.store = store
        self.provider = provider
        self.credential_cipher = credential_cipher
        self.clock = clock or (lambda: datetime.now(UTC))

    async def execute(
            self, organization_id: UUID, actor_id: UUID,
            publication_job_id: UUID) -> ExecutorResult:
        prepared_at = self.clock().astimezone(UTC)
        preparation = await self.store.prepare(
            organization_id, actor_id, publication_job_id, prepared_at,
        )
        if preparation.status is not None:
            return ExecutorResult(preparation.status, publication_job_id)
        snapshot = preparation.snapshot
        if snapshot is None:
            return ExecutorResult(ExecutionStatus.LEASE_UNAVAILABLE, publication_job_id)

        token = None
        credentials = None
        try:
            if (snapshot.connection.token_expires_at is not None
                    and snapshot.connection.token_expires_at.astimezone(UTC) <= prepared_at):
                await self.store.finalize_preflight_failure(
                    snapshot, "FACEBOOK_TOKEN_EXPIRED", self.clock().astimezone(UTC),
                )
                return ExecutorResult(
                    ExecutionStatus.PERMANENT_FAILURE, publication_job_id,
                    error_code="FACEBOOK_TOKEN_EXPIRED",
                )
            binding = CredentialBinding(
                snapshot.connection.organization_id,
                snapshot.connection.platform,
                snapshot.connection.page_id,
                snapshot.connection.key_version,
            )
            try:
                credentials = self.credential_cipher.decrypt(
                    snapshot.connection.ciphertext, binding,
                )
                token = credentials.access_token
            except FacebookCredentialError:
                await self.store.finalize_preflight_failure(
                    snapshot, "FACEBOOK_CREDENTIAL_DECRYPTION_FAILED",
                    self.clock().astimezone(UTC),
                )
                return ExecutorResult(
                    ExecutionStatus.PERMANENT_FAILURE, publication_job_id,
                    error_code="FACEBOOK_CREDENTIAL_DECRYPTION_FAILED",
                )

            if snapshot.mode == ExecutionMode.RECONCILE:
                if snapshot.attempt_started_at is None:
                    await self.store.finalize_preflight_failure(
                        snapshot, "RECONCILIATION_CONTEXT_MISSING",
                        self.clock().astimezone(UTC),
                    )
                    return ExecutorResult(
                        ExecutionStatus.PERMANENT_FAILURE, publication_job_id,
                        error_code="RECONCILIATION_CONTEXT_MISSING",
                    )
                observed_at = self.clock().astimezone(UTC)
                reconciliation = await self.provider.reconcile_post(
                    page_id=snapshot.connection.page_id,
                    message=snapshot.message,
                    link_url=snapshot.link_url,
                    attempt_started_at=snapshot.attempt_started_at,
                    observed_at=observed_at,
                    access_token=token,
                )
                if reconciliation.disposition != ReconciliationDisposition.NOT_FOUND:
                    status = await self.store.finalize_reconciliation(
                        snapshot, reconciliation, self.clock().astimezone(UTC),
                    )
                    return ExecutorResult(
                        status or ExecutionStatus.LEASE_UNAVAILABLE,
                        publication_job_id,
                        error_code=reconciliation.error_code,
                    )

            attempt = await self.store.begin_post(snapshot, self.clock().astimezone(UTC))
            if attempt is None:
                return ExecutorResult(ExecutionStatus.LEASE_UNAVAILABLE, publication_job_id)
            result = await self.provider.publish_post(
                page_id=attempt.connection.page_id,
                message=attempt.message,
                link_url=attempt.link_url,
                access_token=token,
            )
            status = await self.store.finalize_provider(
                attempt, result, self.clock().astimezone(UTC),
            )
            return ExecutorResult(
                status or ExecutionStatus.LEASE_UNAVAILABLE,
                publication_job_id,
                attempt_no=attempt.attempt_no,
                error_code=result.error_code,
            )
        finally:
            token = None
            credentials = None
