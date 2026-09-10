from datetime import datetime
from uuid import UUID
from pydantic import AwareDatetime, ConfigDict, Field, JsonValue, field_validator, model_validator
from backend.app.shared.schemas import ApiSchema
from backend.app.modules.campaigns.models import CampaignStatus


class CampaignCreate(ApiSchema):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    name: str = Field(min_length=1, max_length=200)
    objective: str | None = None
    brief: dict[str, JsonValue] = Field(default_factory=dict)
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def valid_range(self):
        if self.starts_at and self.ends_at and self.ends_at < self.starts_at:
            raise ValueError("Invalid date range")
        return self


class CampaignPatch(ApiSchema):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    objective: str | None = None
    brief: dict[str, JsonValue] | None = None
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None

    @field_validator("name", "brief")
    @classmethod
    def required_when_supplied(cls, value):
        if value is None:
            raise ValueError("Field cannot be null")
        return value


class CampaignRead(CampaignCreate):
    id: UUID
    organization_id: UUID
    status: CampaignStatus
    created_at: datetime
