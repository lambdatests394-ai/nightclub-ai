"""Exactly the three approved asset endpoints."""
from typing import Annotated
from uuid import UUID
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from backend.app.modules.assets.coordinator import AssetCoordinator
from backend.app.modules.assets.dependencies import get_asset_coordinator
from backend.app.modules.assets.errors import InvalidAssetRequest
from backend.app.modules.assets.schemas import UploadIntent

router = APIRouter(prefix="/api/v1/assets", tags=["assets"])
Coordinator = Annotated[AssetCoordinator, Depends(get_asset_coordinator)]


def key(request: Request) -> UUID:
    values = request.headers.getlist("idempotency-key")
    try:
        if len(values) != 1:
            raise ValueError()
        return UUID(values[0])
    except ValueError:
        raise InvalidAssetRequest() from None


def envelope(request: Request, data: dict) -> dict:
    return {"data": data, "meta": {"correlationId": str(request.state.correlation_id)}}


@router.post("/upload-url", status_code=201)
async def upload_url(body: UploadIntent, request: Request, coordinator: Coordinator):
    status, data = await coordinator.upload(key(request), body)
    return JSONResponse(envelope(request, data), status_code=status)


@router.post("/{asset_id}/complete")
async def complete(asset_id: UUID, request: Request, coordinator: Coordinator):
    if await request.body():
        raise InvalidAssetRequest()
    status, data = await coordinator.complete(asset_id, key(request))
    return JSONResponse(envelope(request, data), status_code=status)


@router.get("/{asset_id}/download-url")
async def download_url(asset_id: UUID, request: Request, coordinator: Coordinator):
    return envelope(request, await coordinator.download(asset_id))
