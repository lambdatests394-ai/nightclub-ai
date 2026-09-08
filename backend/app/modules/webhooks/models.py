"""Durable Meta webhook inbox mapping."""
from datetime import datetime
from uuid import UUID, uuid4
from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from backend.app.platform.database import Base, TimestampMixin


class WebhookEvent(TimestampMixin, Base):
    __tablename__ = "webhook_events"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"))
    source: Mapped[str] = mapped_column(String, nullable=False)
    event_key: Mapped[str] = mapped_column(String, nullable=False)
    headers: Mapped[dict] = mapped_column(JSONB, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String, default="received", nullable=False)
    resolution_status: Mapped[str] = mapped_column(String, default="pending", nullable=False)
    processed_at: Mapped[datetime | None]
    error_code: Mapped[str | None] = mapped_column(Text)
