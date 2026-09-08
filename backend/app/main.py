"""Phase 1 FastAPI application entry point."""

from fastapi import FastAPI

from backend.app.core.config import get_settings

settings = get_settings()

app = FastAPI(title=settings.app_name, version="0.1.0")


@app.get("/health/live", tags=["health"])
def live() -> dict[str, str]:
    """Report that the API process is running."""

    return {"status": "ok"}


@app.get("/health/ready", tags=["health"])
def ready() -> dict[str, object]:
    """Report Phase 1 application readiness without external dependency checks."""

    return {"status": "ready", "checks": {"application": "ok"}}
