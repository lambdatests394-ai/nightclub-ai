"""Publication and orchestration persistence mappings."""
from datetime import datetime
from uuid import UUID, uuid4
from sqlalchemy import ForeignKey, ForeignKeyConstraint, SmallInteger, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from backend.app.platform.database import Base, TimestampMixin


class PublicationJob(TimestampMixin, Base):
    __tablename__ = "publication_jobs"
    __table_args__ = (
        ForeignKeyConstraint(["content_item_id", "content_version_id"], ["content_versions.content_item_id", "content_versions.id"], ondelete="RESTRICT", onupdate="RESTRICT", name="publication_jobs_version_belongs_to_item"),
    )
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    content_item_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("content_items.id", ondelete="RESTRICT"), nullable=False)
    content_version_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    idempotency_key: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), unique=True, nullable=False)
    scheduled_for: Mapped[datetime] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String, default="pending", nullable=False)
    attempt_count: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)
    lease_token: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None]
    published_external_id: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None]
    created_by: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="RESTRICT"), nullable=False)


class PublicationAttempt(TimestampMixin, Base):
    __tablename__ = "publication_attempts"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    publication_job_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("publication_jobs.id", ondelete="RESTRICT"), nullable=False)
    attempt_no: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    started_at: Mapped[datetime] = mapped_column(nullable=False)
    finished_at: Mapped[datetime | None]
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_request_id: Mapped[str | None] = mapped_column(Text)
    provider_response: Mapped[dict | None] = mapped_column(JSONB)
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)


class OutboxEvent(TimestampMixin, Base):
    __tablename__ = "outbox_events"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"))
    aggregate_type: Mapped[str] = mapped_column(String, nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    idempotency_key: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String, default="pending", nullable=False)
    attempt_count: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)
    available_at: Mapped[datetime] = mapped_column(nullable=False)
    lease_token: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None]
    delivered_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)


class AutomationRun(TimestampMixin, Base):
    __tablename__ = "automation_runs"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    outbox_event_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("outbox_events.id", ondelete="RESTRICT"))
    workflow_name: Mapped[str] = mapped_column(String, nullable=False)
    n8n_execution_id: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime] = mapped_column(nullable=False)
    finished_at: Mapped[datetime | None]
    details: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
