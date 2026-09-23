"""JWT first; the coordinator explicitly opens/closes each protected transaction."""
from contextlib import asynccontextmanager
from uuid import UUID
from fastapi import Depends, Request

from backend.app.modules.assets.audit import AssetAuditWriter
from backend.app.modules.assets.coordinator import AssetCoordinator
from backend.app.modules.assets.errors import AssetUnavailable
from backend.app.modules.assets.repository import AssetRepository
from backend.app.modules.assets.service import AssetService
from backend.app.modules.identity.dependencies import get_current_user, protected_session
from backend.app.modules.identity.errors import Forbidden
from backend.app.modules.identity.policy import CurrentUser
from backend.app.modules.identity.repository import SQLAlchemyIdentityRepository
from backend.app.modules.identity.service import IdentityService
from backend.app.shared.idempotency import IdempotencyStore


async def get_asset_coordinator(request: Request, user: CurrentUser = Depends(get_current_user)) -> AssetCoordinator:
    selectors = request.headers.getlist("x-organization-id")
    try:
        if len(selectors) != 1:
            raise ValueError()
        organization_id = UUID(selectors[0])
    except ValueError:
        raise Forbidden() from None
    settings = request.app.state.settings
    provider = getattr(request.app.state, "storage_provider", None)
    if provider is None:
        raise AssetUnavailable()

    @asynccontextmanager
    async def transaction():
        async with protected_session(user) as session:
            repository = SQLAlchemyIdentityRepository(session)
            context, _ = await IdentityService(repository,
                organization_context_installer=repository.establish_organization_context).organization(user, organization_id)
            request.state.organization_id = str(context.organization_id)
            yield AssetService(context, AssetRepository(session, context.organization_id),
                IdempotencyStore(session, context), AssetAuditWriter(session, context, request.state.correlation_id), settings)

    return AssetCoordinator(transaction, provider, download_ttl=settings.storage_signed_download_ttl_seconds,
                            timeout=settings.asset_verification_timeout_seconds)
