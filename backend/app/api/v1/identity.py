"""Prompt 4 read-only protected HTTP surface."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from backend.app.modules.identity.dependencies import get_current_user, get_identity_service
from backend.app.modules.identity.errors import Forbidden
from backend.app.modules.identity.policy import CurrentUser
from backend.app.modules.identity.schemas import (
    Envelope, OrganizationMembershipRead, OrganizationRead, PageEnvelope,
    PageMeta, ProfileRead, ResponseMeta,
)
from backend.app.modules.identity.service import IdentityService

router = APIRouter(prefix="/api/v1", tags=["identity"])
UserDependency = Annotated[CurrentUser, Depends(get_current_user)]
ServiceDependency = Annotated[IdentityService, Depends(get_identity_service)]


@router.get("/me", response_model=Envelope[ProfileRead])
async def me(request: Request, user: UserDependency, service: ServiceDependency):
    profile = await service.active_profile(user)
    return Envelope(data=ProfileRead.model_validate(profile),
                    meta=ResponseMeta(correlation_id=request.state.correlation_id))


@router.get("/me/organizations", response_model=PageEnvelope[OrganizationMembershipRead])
async def my_organizations(request: Request, user: UserDependency, service: ServiceDependency,
                           limit: Annotated[int, Query(ge=1, le=100)] = 50,
                           cursor: Annotated[str | None, Query(max_length=22)] = None):
    rows, next_cursor = await service.organizations(user, cursor, limit)
    return PageEnvelope(
        data=[OrganizationMembershipRead(
            **OrganizationRead.model_validate(row.organization).model_dump(), role=row.role,
        ) for row in rows],
        meta=PageMeta(correlation_id=request.state.correlation_id, next_cursor=next_cursor),
    )


@router.get("/organizations/{organization_id}", response_model=Envelope[OrganizationRead])
async def organization(request: Request, organization_id: UUID,
                       user: UserDependency, service: ServiceDependency):
    selectors = request.headers.getlist("x-organization-id")
    if len(selectors) > 1:
        raise Forbidden()
    context, organization = await service.organization(
        user, organization_id, selectors[0] if selectors else None,
    )
    request.state.organization_id = str(context.organization_id)
    return Envelope(data=OrganizationRead.model_validate(organization),
                    meta=ResponseMeta(correlation_id=request.state.correlation_id))
