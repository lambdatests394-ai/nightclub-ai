"""Exactly the three approved Prompt 9 public endpoints."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from backend.app.modules.ai.coordinator import AICoordinator
from backend.app.modules.ai.dependencies import get_ai_coordinator
from backend.app.modules.ai.errors import AIInvalidRequest
from backend.app.modules.ai.schemas import AIGenerationApply, AIGenerationCreate

router = APIRouter(prefix="/api/v1/ai/generations", tags=["ai"])
Coordinator = Annotated[AICoordinator, Depends(get_ai_coordinator)]


def key(request: Request) -> UUID:
    values = request.headers.getlist("idempotency-key")
    try:
        if len(values) != 1:
            raise ValueError()
        return UUID(values[0])
    except ValueError:
        raise AIInvalidRequest() from None


def envelope(request: Request, data: dict) -> dict:
    return {"data": data, "meta": {"correlationId": str(request.state.correlation_id)}}


@router.post("", status_code=201)
async def create(body: AIGenerationCreate, request: Request, coordinator: Coordinator):
    status, data = await coordinator.generate(key(request), body)
    response = JSONResponse(envelope(request, data), status_code=status)
    response.headers["Location"] = f"/api/v1/ai/generations/{data['generationId']}"
    return response


@router.get("/{generation_id}")
async def get(generation_id: UUID, request: Request, coordinator: Coordinator):
    return envelope(request, await coordinator.read(generation_id))


@router.post("/{generation_id}/apply")
async def apply(generation_id: UUID, body: AIGenerationApply, request: Request,
                coordinator: Coordinator):
    status, data = await coordinator.apply(generation_id, key(request), body.variant_index)
    return JSONResponse(envelope(request, data), status_code=status)
