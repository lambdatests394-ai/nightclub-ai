"""Pydantic v2 contracts for the content domain."""

from datetime import datetime
from uuid import UUID

from pydantic import Field

from backend.app.platform.enums import ContentStatus
from backend.app.shared.schemas import ApiSchema


class ContentCreate(ApiSchema):
    campaign_id: UUID | None = None
    platform: str
    connection_id: UUID | None = None
    body: str
    asset_ids: list[UUID] = Field(default_factory=list)


class ContentReviewRequest(ApiSchema):
    version_no: int
    decision: str
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
