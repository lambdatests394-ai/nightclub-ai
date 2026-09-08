"""Meta connection and WhatsApp persistence mappings."""
from datetime import datetime
from uuid import UUID, uuid4
from sqlalchemy import ForeignKey, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from backend.app.platform.database import Base, TimestampMixin


class PlatformConnection(TimestampMixin, Base):
    __tablename__ = "platform_connections"
    __table_args__ = (
        UniqueConstraint("organization_id", "platform", "external_account_id"),
        UniqueConstraint("id", "organization_id", "platform"),
    )
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    platform: Mapped[str] = mapped_column(String, nullable=False)
    external_account_id: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    capabilities: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    credentials_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    credential_key_version: Mapped[int] = mapped_column(nullable=False)
    token_expires_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(String, default="pending", nullable=False)
    last_verified_at: Mapped[datetime | None]
    last_error_code: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None]


class WhatsAppConversation(TimestampMixin, Base):
    __tablename__ = "whatsapp_conversations"
    __table_args__ = (UniqueConstraint("connection_id", "wa_id"),)
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    connection_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("platform_connections.id", ondelete="RESTRICT"), nullable=False)
    wa_id: Mapped[str] = mapped_column(String, nullable=False)
    contact_name: Mapped[str | None] = mapped_column(String)
    last_message_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(String, default="open", nullable=False)


class WhatsAppMessage(TimestampMixin, Base):
    __tablename__ = "whatsapp_messages"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("whatsapp_conversations.id", ondelete="RESTRICT"), nullable=False)
    direction: Mapped[str] = mapped_column(String, nullable=False)
    meta_message_id: Mapped[str | None] = mapped_column(String, unique=True)
    message_type: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[dict] = mapped_column(JSONB, nullable=False)
    delivery_status: Mapped[str | None] = mapped_column(String)
    sent_by: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="RESTRICT"))
    sent_at: Mapped[datetime | None]
    received_at: Mapped[datetime | None]
