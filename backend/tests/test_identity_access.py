from dataclasses import asdict
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import insert
from sqlalchemy.dialects.postgresql import asyncpg
from sqlalchemy.exc import OperationalError

from backend.app.core.config import Settings
from backend.app.main import create_app
from backend.app.modules.identity.dependencies import get_identity_repository, get_token_verifier
from backend.app.modules.identity.errors import AuthUnavailable, Forbidden, InvalidCursor
from backend.app.modules.identity.models import Organization, OrganizationMember, Profile
from backend.app.modules.identity.policy import CurrentUser, MemberRole, OrganizationContext, Permission, require_permission
from backend.app.modules.identity.repository import Membership
from backend.app.modules.identity.service import IdentityService, decode_cursor, encode_cursor

pytestmark = pytest.mark.anyio


def organization(identifier=None, active=True):
    return Organization(id=identifier or uuid4(), name="Test organization", slug="test-org",
                        timezone="America/Mexico_City", is_active=active, created_at=datetime.now(UTC))


@pytest.fixture
def repository(security_material):
    repository = AsyncMock()
    repository.get_profile.return_value = Profile(id=security_material.user_id,
        display_name="Test profile", email="person@example.test", is_active=True)
    repository.get_membership.return_value = None
    repository.list_memberships.return_value = []
    return repository


@pytest.fixture
def api(security_material, repository):
    app = create_app(Settings(_env_file=None))
    app.dependency_overrides[get_token_verifier] = lambda: security_material.verifier
    app.dependency_overrides[get_identity_repository] = lambda: repository
    return app


@pytest.mark.parametrize("role", list(MemberRole))
async def test_all_roles_allow_only_registered_permission(role):
    context = OrganizationContext(uuid4(), uuid4(), role)
    require_permission(context, Permission.ORGANIZATION_READ)
    with pytest.raises(Forbidden):
        require_permission(context, "not-registered")


@pytest.mark.parametrize("role", ["admin", "service_role", "", None])
async def test_unknown_role_denied(role):
    with pytest.raises(Forbidden):
        require_permission(OrganizationContext(uuid4(), uuid4(), role), Permission.ORGANIZATION_READ)


@pytest.mark.parametrize("role", list(MemberRole))
async def test_validated_context_has_only_approved_fields(security_material, repository, role):
    user = CurrentUser(security_material.user_id)
    org = organization()
    repository.get_membership.return_value = Membership(user.user_id, org, role)
    context, result = await IdentityService(repository).organization(user, org.id)
    assert asdict(context) == {"user_id": user.user_id, "organization_id": org.id, "role": role}
    assert result.id == org.id
    repository.get_membership.assert_awaited_once_with(user.user_id, org.id)


@pytest.mark.parametrize("defect", ["absent", "wrong-user", "wrong-org", "inactive-org", "unknown-role",
                                   "missing-profile", "inactive-profile", "wrong-profile"])
async def test_context_rejects_invalid_or_cross_tenant_membership(security_material, repository, defect):
    user = CurrentUser(security_material.user_id)
    org = organization()
    grant = Membership(user.user_id, org, "owner")
    if defect == "absent": grant = None
    if defect == "wrong-user": grant = Membership(uuid4(), org, "owner")
    if defect == "wrong-org": grant = Membership(user.user_id, organization(), "owner")
    if defect == "inactive-org": org.is_active = False
    if defect == "unknown-role": grant = Membership(user.user_id, org, "admin")
    if defect == "missing-profile": repository.get_profile.return_value = None
    if defect == "inactive-profile": repository.get_profile.return_value.is_active = False
    if defect == "wrong-profile": repository.get_profile.return_value.id = uuid4()
    repository.get_membership.return_value = grant
    with pytest.raises(Forbidden):
        await IdentityService(repository).organization(user, org.id)


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Basic fixture"}, {"Authorization": "Bearer"},
                                     {"Authorization": "Bearer a b"}, {"Authorization": "Bearer a.b.c"}])
async def test_http_missing_malformed_invalid_jwt_is_401(api, repository, headers):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        response = await client.get("/api/v1/me", headers=headers)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert "Retry-After" not in response.headers
    assert response.headers["content-type"].startswith("application/problem+json")
    repository.get_profile.assert_not_awaited()


async def test_http_duplicate_authorization_is_rejected(api, security_material):
    header = ("Authorization", "Bearer " + security_material.token())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        response = await client.get("/api/v1/me", headers=[header, header])
    assert response.status_code == 401


async def test_http_jwks_outage_is_503_and_repository_not_executed(api, security_material, repository):
    security_material.provider.error = AuthUnavailable()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        response = await client.get("/api/v1/me", headers={"Authorization": "Bearer " + security_material.token()})
    assert response.status_code == 503
    assert response.json()["code"] == "AUTHENTICATION_UNAVAILABLE"
    repository.get_profile.assert_not_awaited()


@pytest.mark.parametrize("code", ["AUTHENTICATION_UNAVAILABLE", "IDENTITY_UNAVAILABLE"])
async def test_http_503_includes_retry_after_30_seconds(api, security_material, repository, code):
    if code == "AUTHENTICATION_UNAVAILABLE":
        security_material.provider.error = AuthUnavailable()
    else:
        repository.get_profile.side_effect = OperationalError("statement", {}, Exception("local fixture"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        response = await client.get("/api/v1/me", headers={"Authorization": "Bearer " + security_material.token()})
    assert response.status_code == 503
    assert response.json()["code"] == code
    assert response.headers["Retry-After"] == "30"
    assert "WWW-Authenticate" not in response.headers


@pytest.mark.parametrize("profile_state", ["missing", "inactive"])
async def test_http_profile_failure_is_403(api, security_material, repository, profile_state):
    if profile_state == "missing": repository.get_profile.return_value = None
    else: repository.get_profile.return_value.is_active = False
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        response = await client.get("/api/v1/me", headers={"Authorization": "Bearer " + security_material.token()})
    assert response.status_code == 403
    assert "Retry-After" not in response.headers


async def test_http_me_camelcase_no_identity_override_or_secret_leak(api, security_material, repository, caplog):
    caplog.set_level("INFO", logger="nightclub.security")
    token = security_material.token()
    spoof = str(uuid4())
    repository.get_profile.return_value.credentials_ciphertext = b"never-emit"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        response = await client.request("GET", "/api/v1/me?user_id=" + spoof,
            headers={"Authorization": "Bearer " + token, "X-User-Id": spoof, "X-Organization-Id": spoof},
            json={"user_id": spoof})
    assert response.status_code == 200
    assert "Retry-After" not in response.headers
    payload = response.json()
    assert set(payload) == {"data", "meta"}
    assert payload["data"]["id"] == str(security_material.user_id)
    assert "displayName" in payload["data"] and "isActive" in payload["data"]
    assert payload["meta"]["correlationId"] == response.headers["X-Correlation-Id"]
    assert response.headers["Cache-Control"] == "no-store"
    assert "credentials" not in response.text and token not in response.text
    assert token not in caplog.text and "never-emit" not in caplog.text
    security_logs = [record.message for record in caplog.records if record.name == "nightclub.security"]
    assert security_logs and spoof not in "".join(security_logs)
    repository.get_profile.assert_awaited_once_with(security_material.user_id)


@pytest.mark.parametrize("role", list(MemberRole))
async def test_http_all_roles_can_read_own_organization_only(api, security_material, repository, role):
    owned = organization()
    foreign = organization()
    async def lookup(user_id, organization_id):
        return Membership(user_id, owned, role) if organization_id == owned.id else None
    repository.get_membership.side_effect = lookup
    headers = {"Authorization": "Bearer " + security_material.token()}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        response = await client.get(f"/api/v1/organizations/{owned.id}", headers=headers)
        assert response.status_code == 200
        assert response.json()["data"]["id"] == str(owned.id)
        denied = await client.get(f"/api/v1/organizations/{foreign.id}", headers=headers)
        assert denied.status_code == 403
        assert str(foreign.id) not in denied.text


@pytest.mark.parametrize("selector", ["matching", "mismatch", "invalid", "duplicate"])
async def test_http_path_is_canonical_selector(api, security_material, repository, selector):
    org = organization()
    repository.get_membership.return_value = Membership(security_material.user_id, org, "owner")
    value = str(org.id) if selector == "matching" else ("bad-id" if selector == "invalid" else str(uuid4()))
    headers = [("Authorization", "Bearer " + security_material.token()), ("X-Organization-Id", value)]
    if selector == "duplicate": headers.append(("X-Organization-Id", str(org.id)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        response = await client.get(f"/api/v1/organizations/{org.id}", headers=headers)
    assert response.status_code == (200 if selector == "matching" else 403)


async def test_http_list_pagination_and_empty_list(api, security_material, repository):
    orgs = [organization(UUID(int=number)) for number in (1, 2)]
    repository.list_memberships.return_value = [Membership(security_material.user_id, org, "viewer") for org in orgs]
    headers = {"Authorization": "Bearer " + security_material.token()}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        first = (await client.get("/api/v1/me/organizations?limit=1", headers=headers)).json()
        assert len(first["data"]) == 1
        assert first["data"][0]["role"] == "viewer"
        cursor = first["meta"]["nextCursor"]
        assert decode_cursor(cursor) == orgs[0].id
        repository.list_memberships.return_value = []
        last = await client.get("/api/v1/me/organizations", params={"cursor": cursor}, headers=headers)
        assert last.json()["data"] == [] and last.json()["meta"]["nextCursor"] is None
    repository.list_memberships.assert_awaited_with(security_material.user_id, orgs[0].id, 51)


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "cursor=invalid", "cursor=" + "x" * 30])
async def test_invalid_pagination_is_safe_422(api, security_material, query):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        response = await client.get("/api/v1/me/organizations?" + query,
                                    headers={"Authorization": "Bearer " + security_material.token()})
    assert response.status_code == 422
    assert "input" not in response.json()


async def test_configuration_absent_never_disables_authentication():
    app = create_app(Settings(_env_file=None))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/v1/me")).status_code == 401
        assert (await client.get("/api/v1/me", headers={"Authorization": "Bearer fixture"})).status_code == 503


async def test_persistence_failure_has_safe_error(api, security_material, repository):
    repository.get_profile.side_effect = OperationalError("statement", {}, Exception("sensitive connection details"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        response = await client.get("/api/v1/me", headers={"Authorization": "Bearer " + security_material.token()})
    assert response.status_code == 503
    assert "sensitive" not in response.text


async def test_orm_uses_existing_enum_and_timezone_without_external_auth_entity():
    statement = str(insert(OrganizationMember).compile(dialect=asyncpg.dialect()))
    assert "::member_role" in statement and "::VARCHAR" not in statement
    assert OrganizationMember.__table__.c.role.type.create_type is False
    assert OrganizationMember.__table__.c.role.type.enums == [role.value for role in MemberRole]
    assert Profile.__table__.c.last_seen_at.type.timezone is True
    assert "auth.users" not in Profile.metadata.tables


async def test_cursor_is_untrusted_position_only():
    identifier = uuid4()
    assert decode_cursor(encode_cursor(identifier)) == identifier
    with pytest.raises(InvalidCursor):
        decode_cursor("not-a-cursor")
