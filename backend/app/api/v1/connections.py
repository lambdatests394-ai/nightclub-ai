"""Redacted Facebook connection and server-side OAuth routes."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from backend.app.modules.integrations.dependencies import (
    FacebookConnectionCoordinator,
    get_facebook_connection_coordinator,
)
from backend.app.modules.integrations.errors import (
    FacebookInvalidConnection,
    FacebookInvalidOAuthState,
)
from backend.app.modules.integrations.schemas import FacebookOAuthStartRequest


router = APIRouter(prefix="/api/v1/connections", tags=["connections"])
Coordinator = Annotated[
    FacebookConnectionCoordinator,
    Depends(get_facebook_connection_coordinator),
]


def envelope(request: Request, data):
    return {
        "data": data,
        "meta": {"correlationId": str(request.state.correlation_id)},
    }


def idempotency_key(request: Request) -> UUID:
    values = request.headers.getlist("idempotency-key")
    try:
        if len(values) != 1:
            raise ValueError()
        return UUID(values[0])
    except ValueError:
        raise FacebookInvalidConnection() from None


def callback_value(request: Request, name: str, limit: int) -> str:
    values = request.query_params.getlist(name)
    if (len(values) != 1 or not values[0] or len(values[0]) > limit
            or values[0] != values[0].strip()
            or any(ord(character) < 32 for character in values[0])):
        if name == "state":
            raise FacebookInvalidOAuthState()
        raise FacebookInvalidConnection()
    return values[0]


@router.get("")
async def connections(request: Request, coordinator: Coordinator):
    return envelope(request, await coordinator.list_connections())


@router.post("/facebook/oauth/start", status_code=201)
async def facebook_oauth_start(
        body: FacebookOAuthStartRequest, request: Request,
        coordinator: Coordinator):
    status, data = await coordinator.start(
        idempotency_key(request), body.requested_page_id,
    )
    return JSONResponse(envelope(request, data), status_code=status)


@router.get("/facebook/oauth/callback")
async def facebook_oauth_callback(request: Request, coordinator: Coordinator):
    data = await coordinator.callback(
        callback_value(request, "state", 4096),
        callback_value(request, "code", 2048),
    )
    return envelope(request, data)
