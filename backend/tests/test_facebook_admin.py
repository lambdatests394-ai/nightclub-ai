"""Local HTTP/JWT/coordinator/provider rehearsal. No remote calls or PostgreSQL."""
import base64
import logging
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from backend.app.core.config import Settings
from backend.app.main import create_app
from backend.app.modules.identity.policy import MemberRole, OrganizationContext
from backend.app.modules.integrations.dependencies import FacebookConnectionCoordinator
from backend.app.modules.integrations.credentials import CredentialBinding, CredentialCipher
from backend.app.modules.integrations.oauth_state import OAuthStateCodec
from backend.app.modules.integrations.service import FacebookConnectionService
from backend.tests.test_facebook_service import Repository, Idempotency, Audit

pytestmark = pytest.mark.anyio
COMPLETE = "/api/v1/connections/facebook/oauth/complete"
PUBLIC_KEY = "sb_publishable_" + "x" * 32


def settings(**kwargs):
    return Settings(_env_file=None, facebook_oauth_admin_enabled=True,
                    supabase_url="https://identity.example.test",
                    supabase_jwt_issuer="https://identity.example.test/auth/v1",
                    supabase_publishable_key=PUBLIC_KEY, **kwargs)


async def test_ui_is_disabled_by_default_and_new_complete_is_gated():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(Settings(_env_file=None))),
                                base_url="https://app.example.test") as client:
        for path in ("/admin/facebook", "/admin/facebook/config", "/admin/facebook/return",
                     "/admin/facebook/assets/admin.js"):
            assert (await client.get(path)).status_code == 404
        assert (await client.post(COMPLETE, json={"state": "s", "code": "c"})).status_code == 404


async def test_static_return_is_secret_free_query_independent_and_has_no_side_effect():
    app = create_app(settings())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://app.example.test") as client:
        clean = await client.get("/admin/facebook/return")
        returned = await client.get("/admin/facebook/return?code=NEVER_RENDER_CODE&state=NEVER_RENDER_STATE")
        assert clean.content == returned.content
        assert "NEVER_RENDER" not in returned.text
        for path in ("/admin/facebook", "/admin/facebook/return", "/admin/facebook/config",
                     "/admin/facebook/assets/admin.js", "/admin/facebook/assets/return.js"):
            response = await client.get(path)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["referrer-policy"] == "no-referrer"
            assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
            assert "unsafe-inline" not in response.headers["content-security-policy"]
            assert "set-cookie" not in response.headers
        config = (await client.get("/admin/facebook/config")).json()
        assert config == {"authUrl": "https://identity.example.test/auth/v1", "publishableKey": PUBLIC_KEY}
        assert (await client.get("/admin/facebook/assets/config")).status_code == 404
        assert "https://identity.example.test" in (await client.get("/admin/facebook")).headers["content-security-policy"]


async def test_static_ui_is_served_as_utf8_and_legacy_cp1252_variant_regresses(tmp_path):
    assets = Path(__file__).resolve().parents[1] / "app" / "modules" / "integrations" / "admin_ui"
    source = (assets / "admin.js").read_bytes()
    expected = source.decode("utf-8")
    assert "Conexión guardada" in expected

    # Deterministically reproduce the previous Windows default-decoding behavior
    # without changing the application file or the process locale.
    temporary = tmp_path / "admin.js"
    temporary.write_bytes(source)
    legacy_variant = temporary.read_text(encoding="cp1252")
    assert legacy_variant != expected
    assert "Conexión guardada" not in legacy_variant

    app = create_app(settings())
    routes = {
        "/admin/facebook": "index.html",
        "/admin/facebook/return": "return.html",
        "/admin/facebook/assets/admin.js": "admin.js",
        "/admin/facebook/assets/return.js": "return.js",
        "/admin/facebook/assets/style.css": "style.css",
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="https://app.example.test") as client:
        for route, filename in routes.items():
            response = await client.get(route)
            assert response.status_code == 200
            assert response.content == (assets / filename).read_bytes()
        assert "Conexión guardada" in (await client.get("/admin/facebook/assets/admin.js")).text


async def test_incomplete_auth_config_fails_closed():
    for config in (Settings(_env_file=None, facebook_oauth_admin_enabled=True),
                   settings().model_copy(update={"supabase_jwt_issuer": "https://wrong.example.test/auth/v1"})):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(config)),
                                    base_url="https://app.example.test") as client:
            assert (await client.get("/admin/facebook/config")).status_code == 503
            assert (await client.get("/admin/facebook")).status_code == 503


@pytest.mark.parametrize("key", ["sb_secret_" + "a" * 32, "eyJhbGciOiJIUzI1NiJ9.service-role", " anon "])
async def test_config_cannot_expose_server_or_legacy_jwt_key(key):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, supabase_publishable_key=key)


@pytest.fixture
async def rehearsal(security_material, monkeypatch):
    material = security_material
    now = datetime.fromtimestamp(material.now, UTC)
    context = OrganizationContext(uuid4(), material.user_id, MemberRole.OWNER)
    repository, audit, idempotency = Repository(context), Audit(), Idempotency()
    encoded = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")
    cipher = CredentialCipher(encoded(bytes(range(32))), 1)
    codec = OAuthStateCodec(encoded(bytes(range(32, 64))), clock=lambda: now)
    state = {"role": MemberRole.OWNER, "clock": now, "providerFailure": False, "wrongPage": False}
    calls = []

    def handler(request):
        calls.append(request)
        if state["providerFailure"]:
            return httpx.Response(500, json={"error": {"message": "RAW_SECRET_PROVIDER_ERROR"}})
        if request.url.path.endswith("/oauth/access_token"):
            assert parse_qs(request.content.decode())["redirect_uri"] == ["https://app.example.test/admin/facebook/return"]
            return httpx.Response(200, json={"access_token": "SYNTHETIC_USER_TOKEN"})
        return httpx.Response(200, json={"data": [{"id": "456" if state["wrongPage"] else "123",
            "name": "Synthetic Page", "access_token": "SYNTHETIC_PAGE_TOKEN", "tasks": ["CREATE_CONTENT"]}]})

    @asynccontextmanager
    async def context_boundary(self):
        # Only persistence is substituted; JWT dependency and real state/RBAC/cipher/provider remain.
        yield None, replace(context, organization_id=self.organization_id, user_id=self.user.user_id, role=state["role"])

    def service(self, _session, current_context):
        return FacebookConnectionService(current_context, repository, idempotency, audit, cipher, codec,
                                         clock=lambda: state["clock"])

    monkeypatch.setattr(FacebookConnectionCoordinator, "_context", context_boundary)
    monkeypatch.setattr(FacebookConnectionCoordinator, "_service", service)
    app = create_app(settings(meta_app_id="123456", meta_app_secret=SecretStr("SYNTHETIC_APP_SECRET"),
                              meta_graph_api_version="v24.0",
                              meta_oauth_redirect_uri="https://app.example.test/admin/facebook/return"))
    app.state.token_verifier = material.verifier
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as provider:
        app.state.http_client = provider
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://app.example.test") as client:
            yield client, material, context, repository, state, calls, cipher


async def start(rehearsal):
    client, material, context, *_ = rehearsal
    headers = {"Authorization": "Bearer " + material.token(), "X-Organization-ID": str(context.organization_id)}
    response = await client.post("/api/v1/connections/facebook/oauth/start", json={"requestedPageId": "123"},
                                 headers={**headers, "Idempotency-Key": str(uuid4())})
    assert response.status_code == 201
    state = parse_qs(urlsplit(response.json()["data"]["authorizationUrl"]).query)["state"][0]
    return headers, {"state": state, "code": "SYNTHETIC_CODE"}


async def test_full_http_flow_encrypts_page_token_and_rejects_replay(rehearsal, caplog):
    caplog.set_level(logging.INFO, logger="nightclub.security")
    client, _, context, repository, _, calls, cipher = rehearsal
    headers, body = await start(rehearsal)
    assert (await client.get("/admin/facebook/return", params=body)).status_code == 200
    assert not calls and not repository.connections
    response = await client.post(COMPLETE, json=body, headers=headers)
    assert response.status_code == 200
    assert response.json()["data"]["externalAccountId"] == "123"
    assert len(calls) == 2 and len(repository.connections) == 1
    ciphertext, version, page = next(iter(repository.credentials.values()))
    assert b"SYNTHETIC_PAGE_TOKEN" not in ciphertext
    assert cipher.decrypt(ciphertext, CredentialBinding(context.organization_id, "facebook", page, version)).access_token.get_secret_value() == "SYNTHETIC_PAGE_TOKEN"
    replay = await client.post(COMPLETE, json=body, headers=headers)
    assert replay.status_code == 409 and len(calls) == 2
    for sensitive in (body["state"], body["code"], "SYNTHETIC_PAGE_TOKEN", "SYNTHETIC_APP_SECRET"):
        security_logs = "\n".join(record.message for record in caplog.records if record.name == "nightclub.security")
        assert sensitive not in response.text + security_logs


@pytest.mark.parametrize("case", ["missing", "expired", "wrong-user", "wrong-org", "missing-org", "duplicate-org", "role", "tampered", "state-expired"])
async def test_auth_state_and_rbac_fail_before_provider_or_persistence(rehearsal, case):
    client, material, _, repository, state, calls, _ = rehearsal
    headers, body = await start(rehearsal)
    if case == "missing": headers.pop("Authorization")
    elif case == "expired": headers["Authorization"] = "Bearer " + material.token(changes={"iat": material.now - 1200, "exp": material.now - 600})
    elif case == "wrong-user": headers["Authorization"] = "Bearer " + material.token(changes={"sub": str(uuid4())})
    elif case == "wrong-org": headers["X-Organization-ID"] = str(uuid4())
    elif case == "missing-org": headers.pop("X-Organization-ID")
    elif case == "duplicate-org": headers = list(headers.items()) + [("X-Organization-ID", str(uuid4()))]
    elif case == "role": state["role"] = MemberRole.EDITOR
    elif case == "tampered": body["state"] = "altered-state"
    elif case == "state-expired": state["clock"] += timedelta(minutes=11)
    response = await client.post(COMPLETE, json=body, headers=headers)
    assert response.status_code in {400, 401, 403}
    assert not calls and not repository.connections


@pytest.mark.parametrize("body", [{"state": "s", "code": ""}, {"state": "s", "code": "a" * 2049},
    {"state": "s" * 4097, "code": "c"}, {"state": " s", "code": "c"},
    {"state": "s", "code": "SECRET\n"}, {"state": "s", "code": "c", "accessToken": "NEVER_ACCEPT"}])
async def test_complete_validation_is_bounded_and_does_not_echo_inputs(rehearsal, body):
    client, *_ = rehearsal
    headers, _ = await start(rehearsal)
    response = await client.post(COMPLETE, json=body, headers=headers)
    assert response.status_code == 422
    assert "SECRET" not in response.text and "NEVER_ACCEPT" not in response.text


@pytest.mark.parametrize("failure", ["providerFailure", "wrongPage"])
async def test_failed_exchange_saves_nothing_and_requires_new_authorization(rehearsal, failure):
    client, _, _, repository, state, calls, _ = rehearsal
    headers, body = await start(rehearsal)
    state[failure] = True
    response = await client.post(COMPLETE, json=body, headers=headers)
    assert response.status_code in {422, 502}
    assert "RAW_SECRET" not in response.text
    assert not repository.connections
    count = len(calls)
    assert (await client.post(COMPLETE, json=body, headers=headers)).status_code == 409
    assert len(calls) == count


async def test_existing_get_callback_still_requires_bearer(rehearsal):
    client, *_ = rehearsal
    response = await client.get("/api/v1/connections/facebook/oauth/callback?state=s&code=c")
    assert response.status_code == 401
