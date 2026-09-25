"""Facebook HTTP composition with provider I/O outside database transactions."""

from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import Depends, Request
from pydantic import SecretStr

from backend.app.core.config import Settings
from backend.app.modules.identity.dependencies import get_current_user, protected_session
from backend.app.modules.identity.errors import Forbidden
from backend.app.modules.identity.policy import CurrentUser, Permission, require_permission
from backend.app.modules.identity.repository import SQLAlchemyIdentityRepository
from backend.app.modules.identity.service import IdentityService
from backend.app.modules.integrations.audit import FacebookAuditWriter
from backend.app.modules.integrations.credentials import CredentialCipher
from backend.app.modules.integrations.errors import FacebookNotConfigured
from backend.app.modules.integrations.facebook_provider import MetaGraphOAuthAdapter
from backend.app.modules.integrations.oauth_state import OAuthStateCodec
from backend.app.modules.integrations.repository import FacebookConnectionRepository
from backend.app.modules.integrations.service import FacebookConnectionService
from backend.app.shared.idempotency import IdempotencyStore


class FacebookConnectionCoordinator:
    """Own the consume/network/persist boundaries of the OAuth callback."""

    def __init__(
            self, request: Request, user: CurrentUser, organization_id: UUID,
            settings: Settings):
        self.request = request
        self.user = user
        self.organization_id = organization_id
        self.settings = settings

    def _provider(self) -> MetaGraphOAuthAdapter:
        client = getattr(self.request.app.state, "http_client", None)
        if client is None:
            raise FacebookNotConfigured()
        return MetaGraphOAuthAdapter(
            graph_version=self.settings.meta_graph_api_version,
            app_id=self.settings.meta_app_id,
            app_secret=self.settings.meta_app_secret,
            redirect_uri=self.settings.meta_oauth_redirect_uri,
            client=client,
        )

    @asynccontextmanager
    async def _context(self):
        async with protected_session(self.user) as session:
            identity = SQLAlchemyIdentityRepository(session)
            context, _ = await IdentityService(
                identity,
                organization_context_installer=identity.establish_organization_context,
            ).organization(self.user, self.organization_id)
            self.request.state.organization_id = str(context.organization_id)
            yield session, context

    def _service(self, session, context) -> FacebookConnectionService:
        return FacebookConnectionService(
            context,
            FacebookConnectionRepository(
                session, context.organization_id, context.user_id,
            ),
            IdempotencyStore(session, context),
            FacebookAuditWriter(
                session, context, self.request.state.correlation_id,
            ),
            CredentialCipher(
                self.settings.meta_credential_encryption_key,
                self.settings.meta_credential_key_version,
            ),
            OAuthStateCodec(self.settings.meta_oauth_state_key),
        )

    async def list_connections(self) -> list[dict]:
        async with self._context() as (session, context):
            require_permission(context, Permission.FACEBOOK_MANAGE_CONNECTION)
            repository = FacebookConnectionRepository(
                session, context.organization_id, context.user_id,
            )
            rows = await repository.list_facebook_connections()
            return [row.model_dump(mode="json", by_alias=True) for row in rows]

    async def start(self, idempotency_key: UUID, requested_page_id: str):
        provider = self._provider()
        async with self._context() as (session, context):
            status, internal = await self._service(
                session, context,
            ).begin_facebook_oauth(idempotency_key, requested_page_id)
        state = internal["state"]
        return status, {
            "authorizationUrl": provider.authorization_url(state),
            "requestedPageId": internal["requestedPageId"],
            "scopes": internal["scopes"],
            "expiresAt": internal["expiresAt"],
        }

    async def callback(self, state_token: str, authorization_code: str) -> dict:
        provider = self._provider()
        async with self._context() as (session, context):
            state = await self._service(
                session, context,
            ).consume_facebook_oauth_state(state_token)

        code = SecretStr(authorization_code)
        try:
            page = await provider.exchange_page(
                authorization_code=code,
                requested_page_id=state.requested_page_id,
            )
        finally:
            code = None

        async with self._context() as (session, context):
            return await self._service(
                session, context,
            ).persist_verified_facebook_connection(
                page, expected_page_id=state.requested_page_id,
            )


async def get_facebook_connection_coordinator(
        request: Request, user: CurrentUser = Depends(get_current_user),
) -> FacebookConnectionCoordinator:
    values = request.headers.getlist("x-organization-id")
    try:
        if len(values) != 1:
            raise ValueError()
        organization_id = UUID(values[0])
    except ValueError:
        raise Forbidden() from None
    return FacebookConnectionCoordinator(
        request, user, organization_id, request.app.state.settings,
    )
