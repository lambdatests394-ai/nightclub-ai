"""Safe external integration contracts; credentials are deliberately absent."""

from datetime import datetime
from uuid import UUID

from pydantic import ConfigDict, Field, SecretStr, field_validator

from backend.app.shared.schemas import ApiSchema


class PlatformConnectionRead(ApiSchema):
    """Redacted connection view; never add credentials_ciphertext here."""

    id: UUID
    platform: str
    external_account_id: str
    display_name: str
    capabilities: dict
    status: str
    token_expires_at: datetime | None
    last_verified_at: datetime | None
    last_error_code: str | None
    last_error_at: datetime | None
    created_at: datetime
    updated_at: datetime


class FacebookOAuthStartRequest(ApiSchema):
    model_config = ConfigDict(extra="forbid")
    requested_page_id: str = Field(min_length=1, max_length=128, pattern=r"^[1-9][0-9]*$")


class FacebookCapabilities(ApiSchema):
    """Allow-listed provider-derived capability flags."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    can_publish_posts: bool
    can_read_engagement: bool
    can_list_pages: bool


class VerifiedFacebookPage(ApiSchema):
    """Internal normalized provider result; never accept it from an API client."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    external_account_id: str = Field(min_length=1, max_length=128, pattern=r"^[1-9][0-9]*$")
    display_name: str = Field(min_length=1, max_length=255)
    capabilities: FacebookCapabilities
    access_token: SecretStr = Field(repr=False, exclude=True)
    token_type: str | None = Field(default=None, min_length=1, max_length=32)
    token_expires_at: datetime | None = None

    @field_validator("display_name", "token_type")
    @classmethod
    def exact_nonblank(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or value != value.strip()):
            raise ValueError("Value must be exact nonblank text")
        return value

    @field_validator("token_expires_at")
    @classmethod
    def timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Token expiry must be timezone-aware")
        return value


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
