"""Facebook connection application core; no HTTP routes or provider network calls."""

import hmac
from datetime import UTC, datetime
from uuid import UUID, uuid4

from backend.app.modules.identity.policy import Permission, require_permission
from backend.app.modules.integrations.credentials import (
    CredentialBinding,
    CredentialCipher,
    FacebookCredentials,
)
from backend.app.modules.integrations.errors import (
    FacebookConnectionConflict,
    FacebookInvalidConnection,
    FacebookInvalidOAuthState,
    FacebookOAuthStateExpired,
    FacebookOAuthStateReplayed,
    FacebookPageMismatch,
)
from backend.app.modules.integrations.oauth_state import (
    OAuthStateCodec,
    VerifiedOAuthState,
    nonce_digest,
    state_digest,
)
from backend.app.modules.integrations.scopes import FACEBOOK_OAUTH_SCOPES
from backend.app.modules.integrations.schemas import PlatformConnectionRead, VerifiedFacebookPage
from backend.app.shared.idempotency import fingerprint


class FacebookConnectionService:
    def __init__(self, context, repository, idempotency, audit,
                 credential_cipher: CredentialCipher, state_codec: OAuthStateCodec,
                 *, clock=None):
        self.context, self.repository = context, repository
        self.idempotency, self.audit = idempotency, audit
        self.credential_cipher, self.state_codec = credential_cipher, state_codec
        self.clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _page_id(value: str) -> str:
        if (not isinstance(value, str) or not value.isascii() or not value.isdigit()
                or value.startswith("0") or len(value) > 128):
            raise FacebookInvalidConnection()
        return value

    async def list_connections(self) -> list[dict]:
        require_permission(self.context, Permission.FACEBOOK_MANAGE_CONNECTION)
        rows = await self.repository.list_facebook_connections()
        return [row.model_dump(mode="json", by_alias=True) for row in rows]

    async def begin_facebook_oauth(self, idempotency_key: UUID, requested_page_id: str):
        require_permission(self.context, Permission.FACEBOOK_MANAGE_CONNECTION)
        page_id = self._page_id(requested_page_id)
        operation = "facebook:oauth:start"
        semantic_request = {
            "organizationId": str(self.context.organization_id),
            "actorId": str(self.context.user_id),
            "requestedPageId": page_id,
            "scopes": list(FACEBOOK_OAUTH_SCOPES),
        }
        replay = await self.idempotency.claim(
            idempotency_key, operation, fingerprint(operation, semantic_request)
        )
        if replay is not None:
            status, durable = replay
            try:
                if (not isinstance(durable, dict)
                        or set(durable) != {"requestedPageId", "scopes", "issuedAt", "expiresAt"}
                        or durable["requestedPageId"] != page_id
                        or durable["scopes"] != list(FACEBOOK_OAUTH_SCOPES)):
                    raise ValueError()
                issued_at = datetime.fromisoformat(durable["issuedAt"])
                if issued_at.tzinfo is None or issued_at.utcoffset() is None:
                    raise ValueError()
                issued = self.state_codec.issue_idempotent(
                    self.context.organization_id, self.context.user_id, page_id,
                    idempotency_key, issued_at=issued_at,
                )
                if issued.state.expires_at.isoformat() != durable["expiresAt"]:
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise FacebookConnectionConflict() from None
            return status, {
                "state": issued.reveal_token(),
                "requestedPageId": page_id,
                "scopes": list(FACEBOOK_OAUTH_SCOPES),
                "expiresAt": issued.state.expires_at.isoformat(),
            }
        issued_at = self.clock().astimezone(UTC).replace(microsecond=0)
        issued = self.state_codec.issue_idempotent(
            self.context.organization_id, self.context.user_id, page_id,
            idempotency_key, issued_at=issued_at,
        )
        record = await self.repository.insert_oauth_state(
            state_digest=issued.state_digest, nonce_digest=issued.nonce_digest,
            requested_page_id=page_id, expires_at=issued.state.expires_at,
        )
        await self.audit.oauth("facebook.oauth.started", record.id, page_id)
        durable = {
            "requestedPageId": page_id,
            "scopes": list(FACEBOOK_OAUTH_SCOPES),
            "issuedAt": issued.state.issued_at.isoformat(),
            "expiresAt": issued.state.expires_at.isoformat(),
        }
        await self.idempotency.complete(idempotency_key, 201, durable)
        return 201, {
            "state": issued.reveal_token(),
            "requestedPageId": page_id,
            "scopes": list(FACEBOOK_OAUTH_SCOPES),
            "expiresAt": issued.state.expires_at.isoformat(),
        }

    async def verify_facebook_oauth_state(self, token: str) -> tuple[VerifiedOAuthState, object]:
        require_permission(self.context, Permission.FACEBOOK_MANAGE_CONNECTION)
        state = self.state_codec.verify(token)
        if (state.organization_id != self.context.organization_id
                or state.actor_id != self.context.user_id):
            raise FacebookInvalidOAuthState()
        digest = state_digest(token)
        record = await self.repository.get_oauth_state(digest, lock=True)
        expected_nonce_digest = nonce_digest(state.nonce.get_secret_value())
        if (record is None or record.organization_id != state.organization_id
                or record.actor_id != state.actor_id
                or record.requested_page_id != state.requested_page_id
                or not hmac.compare_digest(record.state_digest, digest)
                or not hmac.compare_digest(record.nonce_digest, expected_nonce_digest)
                or record.expires_at.astimezone(UTC) != state.expires_at):
            raise FacebookInvalidOAuthState()
        if record.consumed_at is not None:
            raise FacebookOAuthStateReplayed()
        if self.clock().astimezone(UTC) >= record.expires_at.astimezone(UTC):
            raise FacebookOAuthStateExpired()
        return state, record

    async def consume_facebook_oauth_state(self, token: str) -> VerifiedOAuthState:
        state, record = await self.verify_facebook_oauth_state(token)
        consumed_at = self.clock().astimezone(UTC)
        if not await self.repository.consume_oauth_state(record.id, consumed_at):
            raise FacebookOAuthStateReplayed()
        await self.audit.oauth("facebook.oauth.consumed", record.id, state.requested_page_id)
        return state

    async def persist_verified_facebook_connection(
            self, page: VerifiedFacebookPage, *, expected_page_id: str | None = None) -> dict:
        require_permission(self.context, Permission.FACEBOOK_MANAGE_CONNECTION)
        page_id = self._page_id(page.external_account_id)
        if expected_page_id is not None and page_id != self._page_id(expected_page_id):
            raise FacebookPageMismatch()
        if page.token_expires_at is not None and (
                page.token_expires_at.tzinfo is None or page.token_expires_at.utcoffset() is None):
            raise FacebookInvalidConnection()
        verified_at = self.clock().astimezone(UTC)
        binding = CredentialBinding(
            self.context.organization_id, "facebook", page_id,
            self.credential_cipher.key_version,
        )
        ciphertext = self.credential_cipher.encrypt(
            FacebookCredentials(page.access_token, page.token_type), binding
        )
        capabilities = page.capabilities.model_dump(mode="json", by_alias=True)
        existing = await self.repository.get_by_external_page_id(page_id, lock=True)
        created = False
        if existing is None:
            created = await self.repository.insert_verified_facebook_connection(
                connection_id=uuid4(), page_id=page_id,
                display_name=page.display_name.strip(), capabilities=capabilities,
                ciphertext=ciphertext, key_version=self.credential_cipher.key_version,
                token_expires_at=page.token_expires_at, verified_at=verified_at,
            )
            existing = await self.repository.get_by_external_page_id(page_id, lock=True)
        if existing is None:
            raise FacebookConnectionConflict()
        if not created:
            existing = await self.repository.reconnect_facebook_connection(
                existing.id, display_name=page.display_name.strip(), capabilities=capabilities,
                ciphertext=ciphertext, key_version=self.credential_cipher.key_version,
                token_expires_at=page.token_expires_at, verified_at=verified_at,
            )
        if existing is None:
            raise FacebookConnectionConflict()
        await self.audit.connection(
            "facebook.connection.created" if created else "facebook.connection.updated", existing
        )
        return PlatformConnectionRead.model_validate(existing).model_dump(mode="json", by_alias=True)

    async def get_decrypted_facebook_credentials(self, connection_id: UUID) -> FacebookCredentials:
        require_permission(self.context, Permission.FACEBOOK_PUBLISH)
        material = await self.repository.get_facebook_credentials(connection_id)
        if material is None:
            raise FacebookInvalidConnection()
        binding = CredentialBinding(
            material.organization_id, material.platform, material.external_account_id,
            material.credential_key_version,
        )
        return self.credential_cipher.decrypt(material.credentials_ciphertext, binding)
