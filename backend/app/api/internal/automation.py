"""Single HMAC-authenticated n8n-compatible publication trigger."""

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.app.modules.automation.internal_auth import (
    InternalAuthenticationFailed,
    InternalHMACAuthenticator,
)
from backend.app.modules.automation.publish_due_dependencies import get_publish_due_coordinator


router = APIRouter(tags=["internal-automation"])
PATH = "/internal/automation/publish-due"


def problem(request: Request, status: int, code: str, title: str) -> JSONResponse:
    return JSONResponse(
        status_code=status, media_type="application/problem+json",
        content={"type": "about:blank", "title": title, "status": status,
                 "code": code, "detail": title,
                 "correlationId": str(request.state.correlation_id)},
    )


@router.post(PATH)
async def publish_due(request: Request) -> JSONResponse:
    raw_body = await request.body()
    settings = request.app.state.settings
    authenticator = InternalHMACAuthenticator(
        settings.n8n_internal_secret,
        settings.n8n_internal_secret_previous,
        max_skew_seconds=settings.automation_hmac_max_skew_seconds,
    )
    try:
        authenticator.authenticate(
            method=request.method, path=request.scope["path"], raw_body=raw_body,
            timestamp_values=request.headers.getlist("x-n8n-timestamp"),
            signature_values=request.headers.getlist("x-n8n-signature"),
        )
    except InternalAuthenticationFailed:
        return problem(request, 401, "INTERNAL_AUTHENTICATION_FAILED",
                       "Internal authentication failed")

    if raw_body or request.scope.get("query_string", b""):
        return problem(request, 422, "AUTOMATION_INVALID_REQUEST", "Invalid automation request")

    try:
        # Test injection is process-local and cannot be supplied by the caller.
        factory = getattr(request.app.state, "publish_due_coordinator_factory", None)
        coordinator = factory(request) if factory is not None else get_publish_due_coordinator(request)
        counts = await coordinator.run()
    except asyncio.CancelledError:
        raise
    except Exception:
        return problem(request, 503, "AUTOMATION_UNAVAILABLE", "Automation unavailable")
    return JSONResponse({
        "data": counts.as_dict(),
        "meta": {"correlationId": str(request.state.correlation_id)},
    })
