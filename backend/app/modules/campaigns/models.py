"""Campaign persistence mapping."""
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4
from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from backend.app.platform.database import Base, TimestampMixin


class CampaignStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class Campaign(TimestampMixin, Base):
    __tablename__ = "campaigns"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    objective: Mapped[str | None] = mapped_column(Text)
    brief: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[CampaignStatus] = mapped_column(ENUM(CampaignStatus, name="campaign_status", schema="public", create_type=False, values_callable=lambda values: [v.value for v in values]), default=CampaignStatus.DRAFT, nullable=False)
    created_by: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="RESTRICT"), nullable=False)
