"""Only the six frozen Prompt 7 endpoints."""
from typing import Annotated
from uuid import UUID
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from backend.app.modules.content.dependencies import get_content_service
from backend.app.modules.content.errors import InvalidContentRequest
from backend.app.modules.content.schemas import ContentCreate, ContentPatch, ContentReviewRequest
from backend.app.modules.content.service import ContentService

router = APIRouter(prefix="/api/v1/content", tags=["content"])
Service = Annotated[ContentService, Depends(get_content_service, scope="function")]


def key(request):
    values = request.headers.getlist("idempotency-key")
    try:
        if len(values) != 1:
            raise ValueError()
        return UUID(values[0])
    except ValueError:
        raise InvalidContentRequest() from None


def envelope(request, data, **meta):
    return {"data": data, "meta": {"correlationId": str(request.state.correlation_id), **meta}}


@router.get("")
async def collection(request: Request, service: Service,
                     limit: Annotated[int, Query(ge=1, le=100)] = 50,
                     cursor: Annotated[str | None, Query(max_length=22)] = None):
    rows, next_cursor = await service.page(cursor, limit)
    return envelope(request, rows, nextCursor=next_cursor)


@router.get("/{content_id}")
async def get(content_id: UUID, request: Request, service: Service):
    return envelope(request, await service.read(content_id))


@router.post("", status_code=201)
async def create(body: ContentCreate, request: Request, service: Service):
    status, data = await service.mutate("create", key(request), body.model_dump())
    return JSONResponse(envelope(request, data), status_code=status)


@router.patch("/{content_id}")
async def patch(content_id: UUID, body: ContentPatch, request: Request, service: Service):
    status, data = await service.mutate("patch", key(request), body.model_dump(exclude_unset=True), content_id)
    return JSONResponse(envelope(request, data), status_code=status)


@router.post("/{content_id}/submit-review")
async def submit(content_id: UUID, request: Request, service: Service):
    if await request.body():
        raise InvalidContentRequest()
    status, data = await service.mutate("submit-review", key(request), {}, content_id)
    return JSONResponse(envelope(request, data), status_code=status)


@router.post("/{content_id}/review")
async def review(content_id: UUID, body: ContentReviewRequest, request: Request, service: Service):
    status, data = await service.mutate("review", key(request), body.model_dump(), content_id)
    return JSONResponse(envelope(request, data), status_code=status)
