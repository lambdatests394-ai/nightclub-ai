from datetime import datetime
from uuid import UUID
from backend.app.shared.schemas import ApiSchema


class OrganizationRead(ApiSchema):
    id: UUID
    name: str
    slug: str
    timezone: str
    is_active: bool
    created_at: datetime


class ProfileRead(ApiSchema):
    id: UUID
    display_name: str | None
    email: str | None
    is_active: bool
