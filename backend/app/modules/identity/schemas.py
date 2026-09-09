from datetime import datetime
from uuid import UUID
from typing import Generic, TypeVar
from backend.app.shared.schemas import ApiSchema
from backend.app.modules.identity.policy import MemberRole

DataT = TypeVar("DataT")


class ResponseMeta(ApiSchema):
    correlation_id: UUID


class PageMeta(ResponseMeta):
    next_cursor: str | None = None


class Envelope(ApiSchema, Generic[DataT]):
    data: DataT
    meta: ResponseMeta


class PageEnvelope(ApiSchema, Generic[DataT]):
    data: list[DataT]
    meta: PageMeta


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


class OrganizationMembershipRead(OrganizationRead):
    role: MemberRole
