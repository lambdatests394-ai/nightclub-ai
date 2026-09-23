"""Persistence mappings for versioned content and review workflow."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Integer, SmallInteger, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.platform.database import Base, TimestampMixin
from backend.app.platform.enums import ContentStatus


class ContentItem(TimestampMixin, Base):
    __tablename__ = "content_items"
    __table_args__ = (
        CheckConstraint("platform IN ('facebook', 'whatsapp')", name="content_items_supported_platform"),
        CheckConstraint("approved_version_no IS NULL OR approved_version_no <= current_version_no", name="content_items_approved_version_is_current_or_older"),
        UniqueConstraint("id", "organization_id", name="uq_content_items_id_organization"),
        ForeignKeyConstraint(
            ["connection_id", "organization_id", "platform"],
            ["platform_connections.id", "platform_connections.organization_id", "platform_connections.platform"],
            ondelete="RESTRICT", onupdate="RESTRICT", name="content_items_connection_same_organization_and_platform",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    campaign_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="RESTRICT"))
    platform: Mapped[str] = mapped_column(ENUM("facebook", "whatsapp", "instagram", name="platform_type", schema="public", create_type=False), nullable=False)
    connection_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    status: Mapped[ContentStatus] = mapped_column(ENUM(ContentStatus, name="content_status", schema="public", create_type=False, values_callable=lambda values: [v.value for v in values]), default=ContentStatus.DRAFT, nullable=False)
    current_version_no: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    approved_version_no: Mapped[int | None] = mapped_column(Integer)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    external_post_id: Mapped[str | None] = mapped_column(Text)
    last_error_code: Mapped[str | None] = mapped_column(Text)
    last_error_message: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="RESTRICT"), nullable=False)


class ContentVersion(TimestampMixin, Base):
    __tablename__ = "content_versions"
    __table_args__ = (
        UniqueConstraint("content_item_id", "version_no", name="uq_content_versions_item_version"),
        UniqueConstraint("content_item_id", "id", name="uq_content_versions_item_id"),
        CheckConstraint("source IN ('manual', 'ai', 'import')", name="content_versions_valid_source"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    content_item_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("content_items.id", ondelete="RESTRICT"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    link_url: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    ai_generation_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("ai_generation_requests.id", ondelete="RESTRICT"))
    created_by: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="RESTRICT"), nullable=False)


class ContentAsset(TimestampMixin, Base):
    __tablename__ = "content_assets"
    __table_args__ = (
        UniqueConstraint("content_version_id", "position", name="uq_content_assets_version_position"),
        ForeignKeyConstraint(["content_item_id", "content_version_id"], ["content_versions.content_item_id", "content_versions.id"], ondelete="CASCADE", onupdate="RESTRICT", name="content_assets_version_belongs_to_item"),
        ForeignKeyConstraint(["content_item_id", "organization_id"], ["content_items.id", "content_items.organization_id"], ondelete="RESTRICT", onupdate="RESTRICT", name="content_assets_item_same_organization"),
        ForeignKeyConstraint(["asset_id", "organization_id"], ["assets.id", "assets.organization_id"], ondelete="RESTRICT", onupdate="RESTRICT", name="content_assets_asset_same_organization"),
    )

    content_version_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    asset_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    content_item_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    organization_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    position: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)


class ReviewDecision(TimestampMixin, Base):
    __tablename__ = "review_decisions"
    __table_args__ = (
        CheckConstraint("decision IN ('approved', 'changes_requested')", name="review_decisions_valid_decision"),
        ForeignKeyConstraint(["content_item_id", "content_version_id"], ["content_versions.content_item_id", "content_versions.id"], ondelete="RESTRICT", onupdate="RESTRICT", name="review_decisions_version_belongs_to_item"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    content_item_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    content_version_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    decision: Mapped[str] = mapped_column(String, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="RESTRICT"), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
