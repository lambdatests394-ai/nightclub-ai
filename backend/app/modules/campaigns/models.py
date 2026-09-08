"""Campaign persistence mapping."""
from datetime import datetime
from uuid import UUID, uuid4
from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from backend.app.platform.database import Base, TimestampMixin


class Campaign(TimestampMixin, Base):
    __tablename__ = "campaigns"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    objective: Mapped[str | None] = mapped_column(Text)
    brief: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    starts_at: Mapped[datetime | None]
    ends_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(String, default="draft", nullable=False)
    created_by: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="RESTRICT"), nullable=False)
