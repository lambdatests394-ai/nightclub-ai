"""Safe external integration contracts; credentials are deliberately absent."""

from datetime import datetime
from uuid import UUID

from backend.app.shared.schemas import ApiSchema


class PlatformConnectionRead(ApiSchema):
    """Redacted connection view; never add credentials_ciphertext here."""

    id: UUID
    organization_id: UUID
    platform: str
    external_account_id: str
    display_name: str
    capabilities: dict
    status: str
    token_expires_at: datetime | None
    last_verified_at: datetime | None


class WhatsAppConversationRead(ApiSchema):
    id: UUID
    organization_id: UUID
    connection_id: UUID
    wa_id: str
    contact_name: str | None
    last_message_at: datetime | None
    status: str


class WhatsAppMessageRead(ApiSchema):
    id: UUID
    conversation_id: UUID
    direction: str
    meta_message_id: str | None
    message_type: str
    delivery_status: str | None
    sent_at: datetime | None
    received_at: datetime | None
