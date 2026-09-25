import base64
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError

from backend.app.modules.identity.errors import Forbidden
from backend.app.modules.identity.policy import MemberRole, OrganizationContext, Permission, require_permission
from backend.app.modules.integrations.credentials import CredentialBinding, CredentialCipher
from backend.app.modules.integrations.errors import (
    FacebookInvalidConnection,
    FacebookInvalidOAuthState,
    FacebookOAuthStateExpired,
    FacebookOAuthStateReplayed,
    FacebookPageMismatch,
)
from backend.app.modules.integrations.oauth_state import OAuthStateCodec
from backend.app.modules.integrations.repository import FacebookCredentialMaterial, OAuthStateRecord
from backend.app.modules.integrations.schemas import (
    FacebookCapabilities,
    PlatformConnectionRead,
    VerifiedFacebookPage,
)
from backend.app.modules.integrations.scopes import FACEBOOK_OAUTH_SCOPES
from backend.app.modules.integrations.service import FacebookConnectionService
from backend.app.shared.errors import IdempotencyConflict


NOW = datetime(2030, 1, 1, tzinfo=UTC)


def encoded(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class Idempotency:
    def __init__(self):
        self.rows = {}

    async def claim(self, key, operation, request_hash):
        row = self.rows.get(key)
        if row is None:
            self.rows[key] = [operation, request_hash, None]
            return None
        if row[:2] != [operation, request_hash] or row[2] is None:
            raise IdempotencyConflict()
        return row[2]

    async def complete(self, key, status, body):
        self.rows[key][2] = (status, body)


class Audit:
    def __init__(self):
        self.events = []

    async def oauth(self, action, state_id, page_id):
        self.events.append((action, str(state_id), page_id))

    async def connection(self, action, connection):
        self.events.append((action, str(connection.id), sorted(connection.capabilities)))


class Repository:
    def __init__(self, context):
        self.context = context
        self.oauth = {}
        self.connections = {}
        self.credentials = {}

    async def insert_oauth_state(self, **values):
        row = OAuthStateRecord(uuid4(), self.context.organization_id, self.context.user_id,
                               values["state_digest"], values["nonce_digest"],
                               values["requested_page_id"], values["expires_at"], None)
        self.oauth[row.state_digest] = row
        return row

    async def get_oauth_state(self, digest, *, lock=False):
        return self.oauth.get(digest)

    async def consume_oauth_state(self, state_id, consumed_at):
        for digest, row in self.oauth.items():
            if row.id == state_id and row.consumed_at is None:
                self.oauth[digest] = replace(row, consumed_at=consumed_at)
                return True
        return False

    async def list_facebook_connections(self):
        return list(self.connections.values())

    async def get_by_external_page_id(self, page_id, *, lock=False):
        return self.connections.get(page_id)

    async def insert_verified_facebook_connection(self, **values):
        if values["page_id"] in self.connections:
            return False
        view = PlatformConnectionRead(
            id=values["connection_id"], platform="facebook",
            external_account_id=values["page_id"], display_name=values["display_name"],
            capabilities=values["capabilities"], token_expires_at=values["token_expires_at"],
            status="active", last_verified_at=values["verified_at"], last_error_code=None,
            last_error_at=None, created_at=NOW, updated_at=NOW,
        )
        self.connections[values["page_id"]] = view
        self.credentials[view.id] = (values["ciphertext"], values["key_version"], values["page_id"])
        return True

    async def reconnect_facebook_connection(self, connection_id, **values):
        current = next((row for row in self.connections.values() if row.id == connection_id), None)
        if current is None:
            return None
        view = current.model_copy(update={
            "display_name": values["display_name"], "capabilities": values["capabilities"],
            "token_expires_at": values["token_expires_at"], "status": "active",
            "last_verified_at": values["verified_at"], "last_error_code": None,
            "last_error_at": None, "updated_at": values["verified_at"],
        })
        self.connections[current.external_account_id] = view
        self.credentials[view.id] = (values["ciphertext"], values["key_version"], current.external_account_id)
        return view

    async def get_facebook_credentials(self, connection_id):
        value = self.credentials.get(connection_id)
        if value is None:
            return None
        ciphertext, version, page = value
        return FacebookCredentialMaterial(connection_id, self.context.organization_id,
                                          "facebook", page, version, None, "active", ciphertext)


@pytest.fixture
def case():
    context = OrganizationContext(uuid4(), uuid4(), MemberRole.OWNER)
    repository, idempotency, audit = Repository(context), Idempotency(), Audit()
    cipher = CredentialCipher(encoded(bytes(range(32))), 1)
    codec = OAuthStateCodec(encoded(bytes(range(32, 64))), clock=lambda: NOW)
    service = FacebookConnectionService(context, repository, idempotency, audit, cipher, codec,
                                        clock=lambda: NOW)
    return service, repository, idempotency, audit


@pytest.mark.parametrize("role,allowed", [
    (MemberRole.OWNER, True), (MemberRole.MANAGER, True), (MemberRole.EDITOR, False),
    (MemberRole.REVIEWER, False), (MemberRole.OPERATOR, False), (MemberRole.VIEWER, False),
])
@pytest.mark.parametrize("permission", [Permission.FACEBOOK_MANAGE_CONNECTION, Permission.FACEBOOK_PUBLISH])
def test_facebook_rbac_is_owner_manager_only(role, allowed, permission):
    context = OrganizationContext(uuid4(), uuid4(), role)
    if allowed:
        require_permission(context, permission)
    else:
        with pytest.raises(Forbidden):
            require_permission(context, permission)


@pytest.mark.anyio
async def test_oauth_start_is_fixed_scope_idempotent_structural_and_secret_safe(case):
    service, repository, idempotency, audit = case
    key = uuid4()
    first = await service.begin_facebook_oauth(key, "123456")
    second = await service.begin_facebook_oauth(key, "123456")
    assert first == second and first[0] == 201
    assert first[1]["scopes"] == list(FACEBOOK_OAUTH_SCOPES)
    assert len(repository.oauth) == 1 and len(audit.events) == 1
    row = next(iter(repository.oauth.values()))
    assert first[1]["state"] not in repr(row)
    assert service.state_codec.verify(first[1]["state"]).nonce.get_secret_value() not in repr(row)
    assert first[1]["state"] not in repr(audit.events)
    durable_body = idempotency.rows[key][2][1]
    assert "state" not in durable_body
    assert first[1]["state"] not in repr(durable_body)


@pytest.mark.anyio
async def test_oauth_start_changed_semantics_conflicts_and_page_is_validated(case):
    service, *_ = case
    key = uuid4()
    await service.begin_facebook_oauth(key, "123")
    with pytest.raises(IdempotencyConflict):
        await service.begin_facebook_oauth(key, "456")
    for invalid in ("", "01", "abc", "1 2"):
        with pytest.raises(FacebookInvalidConnection):
            await service.begin_facebook_oauth(uuid4(), invalid)


@pytest.mark.anyio
async def test_oauth_callback_consumes_once_and_rejects_replay(case):
    service, repository, _, audit = case
    _, body = await service.begin_facebook_oauth(uuid4(), "123")
    state = await service.consume_facebook_oauth_state(body["state"])
    assert state.requested_page_id == "123"
    assert next(iter(repository.oauth.values())).consumed_at == NOW
    assert [event[0] for event in audit.events] == ["facebook.oauth.started", "facebook.oauth.consumed"]
    with pytest.raises(FacebookOAuthStateReplayed):
        await service.consume_facebook_oauth_state(body["state"])


@pytest.mark.anyio
async def test_oauth_callback_revalidates_actor_role_and_row_binding(case):
    service, repository, _, _ = case
    _, body = await service.begin_facebook_oauth(uuid4(), "123")
    original = next(iter(repository.oauth.values()))
    repository.oauth[original.state_digest] = replace(original, nonce_digest="0" * 64)
    with pytest.raises(FacebookInvalidOAuthState):
        await service.consume_facebook_oauth_state(body["state"])
    repository.oauth[original.state_digest] = original
    service.context = replace(service.context, role=MemberRole.EDITOR)
    with pytest.raises(Forbidden):
        await service.consume_facebook_oauth_state(body["state"])


@pytest.mark.anyio
async def test_oauth_callback_rejects_wrong_actor_org_tampering_and_expiry(case):
    service, _, _, _ = case
    _, body = await service.begin_facebook_oauth(uuid4(), "123")
    for context in (
        replace(service.context, user_id=uuid4()), replace(service.context, organization_id=uuid4()),
    ):
        changed = FacebookConnectionService(context, service.repository, service.idempotency,
                                            service.audit, service.credential_cipher,
                                            service.state_codec, clock=lambda: NOW)
        with pytest.raises(FacebookInvalidOAuthState):
            await changed.consume_facebook_oauth_state(body["state"])
    replacement = "A" if body["state"][-1] != "A" else "B"
    with pytest.raises(FacebookInvalidOAuthState):
        await service.consume_facebook_oauth_state(body["state"][:-1] + replacement)
    service.clock = lambda: NOW + timedelta(minutes=11)
    with pytest.raises(FacebookOAuthStateExpired):
        await service.consume_facebook_oauth_state(body["state"])


def verified_page(token="sensitive-token", page="123"):
    return VerifiedFacebookPage(
        external_account_id=page, display_name="Night Club",
        capabilities=FacebookCapabilities(can_publish_posts=True,
                                          can_read_engagement=True, can_list_pages=True),
        access_token=SecretStr(token), token_type="bearer",
        token_expires_at=NOW + timedelta(days=30),
    )


@pytest.mark.anyio
async def test_persist_creates_then_reconnects_and_decrypts_without_public_secret(case):
    service, repository, _, audit = case
    created = await service.persist_verified_facebook_connection(verified_page(), expected_page_id="123")
    connection_id = next(iter(repository.credentials))
    first_ciphertext = repository.credentials[connection_id][0]
    updated = await service.persist_verified_facebook_connection(verified_page("rotated-token"), expected_page_id="123")
    assert updated["id"] == str(connection_id)
    assert repository.credentials[connection_id][0] != first_ciphertext
    assert set(created) == {"id", "platform", "externalAccountId", "displayName", "capabilities",
                            "tokenExpiresAt", "status", "lastVerifiedAt", "lastErrorCode",
                            "lastErrorAt", "createdAt", "updatedAt"}
    assert "sensitive-token" not in repr(created) + repr(updated) + repr(audit.events)
    credentials = await service.get_decrypted_facebook_credentials(connection_id)
    assert credentials.access_token.get_secret_value() == "rotated-token"
    assert [event[0] for event in audit.events] == ["facebook.connection.created", "facebook.connection.updated"]


@pytest.mark.anyio
async def test_persist_rejects_page_mismatch_and_unknown_capabilities(case):
    service, *_ = case
    with pytest.raises(FacebookPageMismatch):
        await service.persist_verified_facebook_connection(verified_page(), expected_page_id="456")
    with pytest.raises(ValidationError):
        FacebookCapabilities(canPublishPosts=True, canReadEngagement=True,
                             canListPages=True, arbitrary=True)
