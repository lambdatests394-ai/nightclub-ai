"""Static OAuth bridge. Public return never consumes state or exchanges a code."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, JSONResponse

from backend.app.modules.identity.errors import AuthUnavailable


router = APIRouter(prefix="/admin/facebook", tags=["facebook-admin"])
_ASSETS = Path(__file__).resolve().parents[2] / "modules" / "integrations" / "admin_ui"


def require_facebook_admin(request: Request):
    if not request.app.state.settings.facebook_oauth_admin_enabled:
        raise HTTPException(status_code=404, detail="Not found")


def browser_config(request: Request) -> dict:
    settings = request.app.state.settings
    if (not settings.supabase_url or not settings.supabase_publishable_key
            or settings.supabase_jwt_issuer != settings.supabase_url + "/auth/v1"):
        raise AuthUnavailable()
    return {"authUrl": settings.supabase_url + "/auth/v1",
            "publishableKey": settings.supabase_publishable_key}


def headers(auth_origin: str = "") -> dict[str, str]:
    return {
        "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
        "Content-Security-Policy": (
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            f"connect-src 'self'{(' ' + auth_origin) if auth_origin else ''}; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        ),
    }


@router.get("/config", dependencies=[Depends(require_facebook_admin)])
def config(request: Request):
    # Only the publishable key is deliberately exposed. No server secret.
    return JSONResponse(browser_config(request), headers=headers())


@router.get("", dependencies=[Depends(require_facebook_admin)], include_in_schema=False)
def screen(request: Request):
    browser_config(request)
    return Response((_ASSETS / "index.html").read_text(encoding="utf-8"), media_type="text/html",
                    headers=headers(request.app.state.settings.supabase_url))


@router.get("/return", dependencies=[Depends(require_facebook_admin)], include_in_schema=False)
def oauth_return():
    # Never interpolate query parameters into HTML or perform provider/database I/O.
    return Response((_ASSETS / "return.html").read_text(encoding="utf-8"),
                    media_type="text/html", headers=headers())


@router.get("/assets/{name}", dependencies=[Depends(require_facebook_admin)], include_in_schema=False)
def asset(name: str):
    media = {"admin.js": "text/javascript", "return.js": "text/javascript", "style.css": "text/css"}
    if name not in media:
        raise HTTPException(status_code=404, detail="Not found")
    return Response((_ASSETS / name).read_text(encoding="utf-8"),
                    media_type=media[name], headers=headers())
