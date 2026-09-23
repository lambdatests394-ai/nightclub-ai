"""Asset HTTP contracts use ASGI and local doubles only."""
from dataclasses import replace
from uuid import UUID, uuid4
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from backend.app.core import database
from backend.app.core.config import Settings
from backend.app.main import create_app
from backend.app.modules.assets import dependencies
from backend.app.modules.assets.dependencies import get_asset_coordinator
from backend.app.modules.assets.filenames import sanitize_filename
from backend.app.modules.assets.schemas import UploadIntent
from backend.app.modules.assets.storage import StorageInvalidResponse, StorageMissing, StorageUnavailable
from backend.app.modules.identity.dependencies import get_current_user
from backend.app.modules.identity import dependencies as identity_dependencies
from backend.app.modules.identity.errors import Forbidden
from backend.app.modules.identity.policy import CurrentUser, MemberRole, Permission, require_permission
from backend.tests.assets_fakes import AssetFixture, intent

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("role", list(MemberRole))
@pytest.mark.parametrize("action", ["upload", "complete", "download"])
async def test_asset_role_matrix_http(role, action):
    case = AssetFixture()
    asset_id = UUID((await case.coordinator.upload(uuid4(), intent()))[1]["asset"]["id"])
    if action == "download":
        await case.coordinator.complete(asset_id, uuid4())
    case.service.context = replace(case.context, role=role)
    app = create_app(); app.dependency_overrides[get_asset_coordinator] = lambda: case.coordinator
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        if action == "upload":
            # Same ready hash is valid reuse; pending would be the deliberate 409.
            body = intent().model_copy(update={"sha256": "f"*64})
            response = await client.post("/api/v1/assets/upload-url", headers={"Idempotency-Key": str(uuid4())},
                                         json=body.model_dump(by_alias=True))
        elif action == "complete":
            response = await client.post(f"/api/v1/assets/{asset_id}/complete", headers={"Idempotency-Key": str(uuid4())})
        else:
            response = await client.get(f"/api/v1/assets/{asset_id}/download-url")
    allowed = action == "download" or role in {MemberRole.OWNER, MemberRole.MANAGER, MemberRole.EDITOR}
    assert response.status_code == ((201 if action == "upload" else 200) if allowed else 403)
    assert response.headers["Cache-Control"] == "no-store"
    assert "Retry-After" not in response.headers


@pytest.mark.parametrize("headers", [[], [("Idempotency-Key", "invalid")], [("Idempotency-Key", str(uuid4())), ("Idempotency-Key", str(uuid4()))]])
@pytest.mark.parametrize("operation", ["upload-url", "complete"])
async def test_mutation_requires_exact_uuid_key(headers, operation):
    case = AssetFixture(); app = create_app()
    app.dependency_overrides[get_asset_coordinator] = lambda: case.coordinator
    path = "/api/v1/assets/upload-url" if operation == "upload-url" else f"/api/v1/assets/{uuid4()}/complete"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        response = await client.post(path, headers=headers, **({"json": intent().model_dump(by_alias=True)} if operation == "upload-url" else {}))
    assert response.status_code == 422 and not case.repository.rows


@pytest.mark.parametrize("headers", [[], [("Authorization", "bad")], [("Authorization", "Bearer one"), ("Authorization", "Bearer two")]])
async def test_missing_duplicate_malformed_authorization(headers):
    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        result = await client.get(f"/api/v1/assets/{uuid4()}/download-url", headers=headers)
    assert result.status_code == 401


@pytest.mark.parametrize("headers", [[], [("X-Organization-Id", "bad")], [("X-Organization-Id", str(uuid4())), ("X-Organization-Id", str(uuid4()))]])
async def test_exact_organization_selector(headers):
    app = create_app(); app.dependency_overrides[get_current_user] = lambda: CurrentUser(uuid4())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        result = await client.get(f"/api/v1/assets/{uuid4()}/download-url", headers=headers)
    assert result.status_code == 403


@pytest.mark.parametrize("name", ["", ".", "..", ".hidden.png", " .hidden.png", "../a.png", "a/b.png", "a\\b.png",
    "a\u202e.png", "a\x00.png", "file.exe.png", "file.html.jpg", "file.js.webp", "file.ｅｘｅ.png", "a:stream.png", "ñ"*128])
def test_filename_validation(name):
    with pytest.raises(ValueError): sanitize_filename(name, "image/png")


def test_filename_canonicalization():
    assert sanitize_filename("Fête del CLUB!!.jpeg", "image/png") == "fete-del-club.png"
    assert sanitize_filename("東京.png", "image/jpeg") == "asset.jpg"
    assert sanitize_filename("a"*120 + ".webp", "image/webp") == "a"*80 + ".webp"


@pytest.mark.parametrize("mime", ["image/jpeg", "image/png", "image/webp"])
def test_only_declared_media_allowed(mime):
    assert intent(mime=mime).mime_type == mime


@pytest.mark.parametrize("field,value", [("mimeType","image/svg+xml"),("mimeType","image/gif"),("mimeType","video/mp4"),
    ("byteSize",0),("byteSize",True),("byteSize",10_485_761),("byteSize","42"),("sha256","A"*64),("sha256","f"*63),
    ("assetId",str(uuid4())),("organizationId",str(uuid4())),("uploadedBy",str(uuid4())),("kind","video"),
    ("bucket","other"),("storageBucket","other"),("storageKey","path"),("status","ready"),("deletedAt",None),
    ("width",2),("height",2),("durationMs",100),("upsert",True),("signedUrl","https://example.test"),("token","synthetic")])
def test_strict_upload_input(field, value):
    payload = intent().model_dump(by_alias=True); payload[field] = value
    with pytest.raises(ValidationError): UploadIntent.model_validate(payload)


async def test_complete_no_body_and_no_unapproved_routes():
    case = AssetFixture(); app = create_app()
    app.dependency_overrides[get_asset_coordinator] = lambda: case.coordinator
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        result = await client.post(f"/api/v1/assets/{uuid4()}/complete", json={}, headers={"Idempotency-Key":str(uuid4())})
        assert result.status_code == 422
        assert (await client.get("/api/v1/assets")).status_code == 404
        assert (await client.get(f"/api/v1/assets/{uuid4()}")).status_code == 404
        assert (await client.delete(f"/api/v1/assets/{uuid4()}")).status_code == 404


@pytest.mark.parametrize("error,status", [(StorageUnavailable,503),(StorageInvalidResponse,502),(StorageMissing,409)])
async def test_storage_http_errors_and_retry_after(error,status):
    case = AssetFixture(); asset_id = UUID((await case.coordinator.upload(uuid4(),intent()))[1]["asset"]["id"])
    completion_key = uuid4()
    case.provider.error = error
    app = create_app(); app.dependency_overrides[get_asset_coordinator] = lambda: case.coordinator
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        response = await client.post(f"/api/v1/assets/{asset_id}/complete",headers={"Idempotency-Key":str(completion_key)})
    assert response.status_code == status
    assert response.headers.get("Retry-After") == ("30" if status==503 else None)
    assert "synthetic" not in response.text
    if error is StorageMissing:
        assert case.repository.rows[asset_id].status == "pending"
        assert completion_key not in case.idempotency.rows
        assert case.audit.write.await_count == 1


@pytest.mark.parametrize("fails", [False, True])
async def test_real_dependency_composition_root_commit_before_signing_and_http(monkeypatch, fails):
    case, events, session = AssetFixture(), [], AsyncMock()
    session.info = {}
    async def commit():
        events.append("commit")
        if fails: raise ConnectionError("private synthetic failure")
    session.commit.side_effect = commit
    monkeypatch.setattr(database, "SessionFactory", lambda: session)
    monkeypatch.setattr(identity_dependencies, "verify_runtime_role", AsyncMock())
    monkeypatch.setattr(identity_dependencies, "establish_user_context", AsyncMock())
    monkeypatch.setattr(dependencies.IdentityService,"organization",AsyncMock(return_value=(case.context,object())))
    monkeypatch.setattr(dependencies,"AssetService",lambda *args:case.service)
    original = case.provider.create_signed_upload
    async def sign(*args,**kwargs):
        events.append("sign"); assert events[0] == "commit"
        return await original(*args,**kwargs)
    case.provider.create_signed_upload = sign
    app = create_app(); app.state.storage_provider = case.provider
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(case.context.user_id)
    async def observed(scope,receive,send):
        async def record(message):
            if message["type"]=="http.response.start": events.append(message["status"])
            await send(message)
        await app(scope,receive,record)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=observed),base_url="http://local.test") as client:
        response=await client.post("/api/v1/assets/upload-url",json=intent().model_dump(by_alias=True),
            headers={"Idempotency-Key":str(uuid4()),"X-Organization-Id":str(case.context.organization_id)})
    assert response.status_code == (503 if fails else 201)
    assert events == (["commit",503] if fails else ["commit","sign",201])


@pytest.mark.parametrize("url", ["http://example.test", "https://user@example.test", "https://example.test/?query=1", "https://example.test/#x", "https://example.test/path", "https://example.test:8000"])
def test_storage_config_rejects_unsafe_origins(url):
    with pytest.raises(ValidationError): Settings(_env_file=None,supabase_url=url)


def test_storage_secret_is_hidden_and_limits_are_bounded():
    settings=Settings(_env_file=None,supabase_service_role_key="synthetic-server-only-key")
    assert "synthetic-server-only-key" not in repr(settings) and "supabase_service_role_key" not in settings.model_dump()
    assert "storage_signed_upload_ttl_seconds" not in Settings.model_fields
    for values in ({"storage_bucket":"other"},{"asset_max_image_bytes":10_485_761},{"asset_verification_timeout_seconds":16}):
        with pytest.raises(ValidationError): Settings(_env_file=None,**values)
    with pytest.raises(Forbidden): require_permission(replace(AssetFixture().context,role="unknown"),Permission.ASSET_READ)


async def test_signing_failure_http_is_503_retry_after_with_durable_intent():
    case = AssetFixture(); case.provider.sign_error = StorageUnavailable
    app = create_app(); app.dependency_overrides[get_asset_coordinator] = lambda: case.coordinator
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        result = await client.post("/api/v1/assets/upload-url", json=intent().model_dump(by_alias=True), headers={"Idempotency-Key":str(uuid4())})
    assert result.status_code == 503 and result.headers["Retry-After"] == "30"
    assert len(case.repository.rows) == 1 and len(case.idempotency.rows) == 1


async def test_membership_denied_before_idempotency_or_signing(monkeypatch):
    case, session = AssetFixture(), AsyncMock()
    session.info = {}
    monkeypatch.setattr(database, "SessionFactory", lambda: session)
    monkeypatch.setattr(identity_dependencies, "verify_runtime_role", AsyncMock())
    monkeypatch.setattr(identity_dependencies, "establish_user_context", AsyncMock())
    monkeypatch.setattr(dependencies.IdentityService, "organization", AsyncMock(side_effect=Forbidden()))
    app = create_app(); app.state.storage_provider = case.provider
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(case.context.user_id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        result = await client.post("/api/v1/assets/upload-url", json=intent().model_dump(by_alias=True),
            headers={"X-Organization-Id":str(case.context.organization_id),"Idempotency-Key":str(uuid4())})
    assert result.status_code == 403 and case.provider.uploads == []
    session.commit.assert_not_awaited()
