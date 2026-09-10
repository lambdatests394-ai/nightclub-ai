"""Only the five approved Prompt 6 endpoints; no publishing or content routes."""
from typing import Annotated
from uuid import UUID
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from backend.app.modules.campaigns.dependencies import get_campaign_service
from backend.app.modules.campaigns.errors import InvalidCampaignRequest
from backend.app.modules.campaigns.schemas import CampaignCreate, CampaignPatch
from backend.app.modules.campaigns.service import CampaignService

router = APIRouter(prefix="/api/v1/campaigns", tags=["campaigns"])
Service = Annotated[CampaignService, Depends(get_campaign_service, scope="function")]


def idempotency_key(request):
    values = request.headers.getlist("idempotency-key")
    try:
        if len(values) != 1:
            raise ValueError()
        return UUID(values[0])
    except ValueError:
        raise InvalidCampaignRequest() from None


def envelope(request, data, **meta):
    return {"data": data, "meta": {"correlationId": str(request.state.correlation_id), **meta}}


@router.get("")
async def list_campaigns(request: Request, service: Service,
                         limit: Annotated[int, Query(ge=1, le=100)] = 50,
                         cursor: Annotated[str | None, Query(max_length=22)] = None):
    rows, cursor = await service.page(cursor, limit)
    return envelope(request, rows, nextCursor=cursor)


@router.get("/{campaign_id}")
async def get_campaign(campaign_id: UUID, request: Request, service: Service):
    return envelope(request, await service.read(campaign_id))


@router.post("", status_code=201)
async def create_campaign(body: CampaignCreate, request: Request, service: Service):
    status, data = await service.mutate("create", idempotency_key(request), body.model_dump())
    return JSONResponse(envelope(request, data), status_code=status)


@router.patch("/{campaign_id}")
async def patch_campaign(campaign_id: UUID, body: CampaignPatch, request: Request, service: Service):
    status, data = await service.mutate("patch", idempotency_key(request),
                                      body.model_dump(exclude_unset=True), campaign_id)
    return JSONResponse(envelope(request, data), status_code=status)


@router.post("/{campaign_id}/archive")
async def archive_campaign(campaign_id: UUID, request: Request, service: Service):
    # This operation has no payload. Reject ignored client overrides explicitly.
    if await request.body():
        raise InvalidCampaignRequest()
    status, data = await service.mutate("archive", idempotency_key(request), {}, campaign_id)
    return JSONResponse(envelope(request, data), status_code=status)
