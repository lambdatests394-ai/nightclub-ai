"""Pydantic v2 contracts for the content domain."""

from datetime import datetime
from uuid import UUID
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from backend.app.platform.enums import ContentStatus
from backend.app.shared.schemas import ApiSchema


class ContentCreate(ApiSchema):
    model_config = ConfigDict(extra="forbid")
    campaign_id: UUID | None = None
    platform: Literal["facebook", "whatsapp"]
    connection_id: UUID | None = None
    body: str
    title: str | None = None
    link_url: str | None = None


class ContentPatch(ApiSchema):
    model_config = ConfigDict(extra="forbid")
    body: str | None = None
    title: str | None = None
    link_url: str | None = None

    @field_validator("body")
    @classmethod
    def nonnull_body(cls, value):
        if value is None:
            raise ValueError("Body cannot be null")
        return value

    @model_validator(mode="after")
    def nonempty(self):
        if not self.model_fields_set:
            raise ValueError("At least one editable field required")
        return self


class ContentReviewRequest(ApiSchema):
    model_config = ConfigDict(extra="forbid")
    version_no: int = Field(ge=1, strict=True)
    decision: Literal["approved", "changes_requested"]
    comment: str | None = None


class ContentScheduleRequest(ApiSchema):
    version_no: int
    scheduled_for: datetime


class ContentRead(ApiSchema):
    id: UUID
    status: ContentStatus
    current_version_no: int
    approved_version_no: int | None
    platform: str
    created_at: datetime


class ContentCurrentRead(ContentRead):
    """Public Prompt 7 projection; legacy minimal ContentRead remains compatible."""
    campaign_id: UUID | None
    connection_id: UUID | None
    body: str
    title: str | None
    link_url: str | None
    created_by: UUID
    updated_at: datetime
