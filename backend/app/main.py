"""FastAPI application with injectable security and per-process JWKS lifecycle."""
import json
import logging
import sys
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from backend.app.api.v1.identity import router
from backend.app.api.v1.campaigns import router as campaigns_router
from backend.app.core.config import Settings, get_settings
from backend.app.modules.identity.authentication import SupabaseJWTVerifier
from backend.app.modules.identity.errors import IdentityUnavailable, SecurityError
from backend.app.modules.identity.jwks import HTTPJWKSProvider, JWKSCache

logger = logging.getLogger("nightclub.security")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(settings.log_level.upper())
        async with httpx.AsyncClient(verify=True, follow_redirects=False, trust_env=False) as client:
            if settings.supabase_jwks_url and settings.supabase_jwt_issuer and settings.supabase_jwt_audience:
                application.state.token_verifier = SupabaseJWTVerifier(
                    settings, JWKSCache(HTTPJWKSProvider(settings.supabase_jwks_url, client),
                                        ttl=settings.supabase_jwks_cache_ttl_seconds),
                )
            try:
                yield
            finally:
                application.state.token_verifier = None
                logger.removeHandler(handler)
                handler.close()

    application = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

    @application.middleware("http")
    async def security_log(request: Request, call_next):
        request.state.correlation_id = uuid4()
        response = await call_next(request)
        response.headers["X-Correlation-Id"] = str(request.state.correlation_id)
        if request.url.path.startswith("/api/v1/"):
            response.headers["Cache-Control"] = "no-store"
            # Allow-list: no request headers, query, JWT or exception text.
            logger.info(json.dumps({
                "event": "protected_request", "correlationId": str(request.state.correlation_id),
                "userId": getattr(request.state, "verified_user_id", None),
                "organizationId": getattr(request.state, "organization_id", None),
                "endpoint": getattr(request.scope.get("route"), "path", "unmatched"),
                "status": response.status_code,
            }))
        return response

    @application.exception_handler(SecurityError)
    async def security_error(request: Request, error: SecurityError):
        headers = {"WWW-Authenticate": "Bearer"} if error.status == 401 else {}
        if error.status == 503:
            # Fixed retry guidance, aligned with the 30-second JWKS refresh floor;
            # it is not a promise that authentication/storage will have recovered.
            headers["Retry-After"] = "30"
        return JSONResponse(
            status_code=error.status, media_type="application/problem+json",
            headers=headers,
            content={"type": "about:blank", "title": error.title, "status": error.status,
                     "code": error.code, "detail": error.title,
                     "correlationId": str(request.state.correlation_id)},
        )

    @application.exception_handler(SQLAlchemyError)
    @application.exception_handler(ConnectionError)
    @application.exception_handler(OSError)
    async def storage_error(request: Request, error: Exception):
        return await security_error(request, IdentityUnavailable())

    @application.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError):
        # Validation errors include input values; never echo them into public errors.
        return JSONResponse(status_code=422, media_type="application/problem+json", content={
            "type": "about:blank", "title": "Invalid request", "status": 422,
            "code": "INVALID_REQUEST", "correlationId": str(request.state.correlation_id),
        })

    @application.get("/health/live", tags=["health"])
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/health/ready", tags=["health"])
    def ready() -> dict[str, object]:
        # Preserved scaffold contract; not an external dependency probe.
        return {"status": "ready", "checks": {"application": "ok"}}

    application.include_router(router)
    application.include_router(campaigns_router)
    return application


app = create_app()
