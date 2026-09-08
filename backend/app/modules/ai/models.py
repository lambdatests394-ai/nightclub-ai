"""Auditable provider-neutral AI generation request mapping."""
from uuid import UUID, uuid4
from sqlalchemy import ForeignKey, ForeignKeyConstraint, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from backend.app.platform.database import Base, TimestampMixin


class AIGenerationRequest(TimestampMixin, Base):
    __tablename__ = "ai_generation_requests"
    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_ai_generation_requests_id_organization"),
        ForeignKeyConstraint(["content_item_id", "organization_id"], ["content_items.id", "content_items.organization_id"], ondelete="RESTRICT", onupdate="RESTRICT", name="ai_generation_requests_content_same_organization"),
    )
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    content_item_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    provider: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    prompt_template_key: Mapped[str] = mapped_column(String, nullable=False)
    prompt_template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    input_redacted: Mapped[dict] = mapped_column(JSONB, nullable=False)
    output: Mapped[dict | None] = mapped_column(JSONB)
    provider_request_id: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6))
    status: Mapped[str] = mapped_column(String, default="queued", nullable=False)
    error_code: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="RESTRICT"), nullable=False)
