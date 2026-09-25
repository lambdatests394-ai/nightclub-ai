from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID
from backend.app.shared.schemas import ApiSchema


class PublicationJobRead(ApiSchema):
    id: UUID
    content_item_id: UUID
    content_version_id: UUID
    scheduled_for: datetime
    status: str
    attempt_count: int


class PublicationActionRead(ApiSchema):
    """Compact scheduling result; content and provider payloads stay private."""

    content_id: UUID
    content_status: str
    version_no: int
    publication_job_id: UUID
    publication_job_status: str
    scheduled_for: datetime | None
    connection_id: UUID


class ExecutionMode(StrEnum):
    POST = "post"
    RECONCILE = "reconcile"


class ExecutionStatus(StrEnum):
    PUBLISHED = "published"
    RETRY_SCHEDULED = "retry_scheduled"
    PERMANENT_FAILURE = "permanent_failure"
    NOT_DUE = "not_due"
    LEASE_UNAVAILABLE = "lease_unavailable"
    RECONCILED_PUBLISHED = "reconciled_published"


@dataclass(frozen=True, slots=True)
class CredentialEnvelope:
    connection_id: UUID
    organization_id: UUID
    platform: str
    page_id: str
    key_version: int
    token_expires_at: datetime | None
    ciphertext: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class ExecutionSnapshot:
    organization_id: UUID
    actor_id: UUID
    content_id: UUID
    content_version_id: UUID
    job_id: UUID
    lease_token: UUID
    connection: CredentialEnvelope = field(repr=False)
    message: str = field(repr=False)
    link_url: str | None = field(default=None, repr=False)
    request_fingerprint: str = ""
    mode: ExecutionMode = ExecutionMode.POST
    attempt_id: UUID | None = None
    attempt_no: int | None = None
    attempt_started_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Preparation:
    status: ExecutionStatus | None = None
    snapshot: ExecutionSnapshot | None = None


@dataclass(frozen=True, slots=True)
class ExecutorResult:
    status: ExecutionStatus
    publication_job_id: UUID
    attempt_no: int | None = None
    error_code: str | None = None
