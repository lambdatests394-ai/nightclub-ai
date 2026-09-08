from datetime import datetime
from uuid import UUID
from pydantic import Field
from backend.app.shared.schemas import ApiSchema


class CampaignCreate(ApiSchema):
    name: str
    objective: str | None = None
    brief: dict = Field(default_factory=dict)
    starts_at: datetime | None = None
    ends_at: datetime | None = None


class CampaignRead(CampaignCreate):
    id: UUID
    organization_id: UUID
    status: str
    created_at: datetime
