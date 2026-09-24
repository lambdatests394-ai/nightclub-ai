"""JWT-first AI composition with a fresh protected transaction per coordinator phase."""
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import Depends, Request

from backend.app.modules.ai.audit import AIAuditWriter
from backend.app.modules.ai.coordinator import AICoordinator
from backend.app.modules.ai.errors import AIProviderNotConfigured
from backend.app.modules.ai.repository import AIRepository
from backend.app.modules.ai.service import AIService
from backend.app.modules.content.audit import ContentAuditWriter
from backend.app.modules.content.repository import ContentRepository
from backend.app.modules.identity.dependencies import get_current_user, protected_session
from backend.app.modules.identity.errors import Forbidden
from backend.app.modules.identity.policy import CurrentUser
from backend.app.modules.identity.repository import SQLAlchemyIdentityRepository
from backend.app.modules.identity.service import IdentityService
from backend.app.shared.idempotency import IdempotencyStore


async def get_ai_coordinator(request: Request, user: CurrentUser = Depends(get_current_user)) -> AICoordinator:
    selectors = request.headers.getlist("x-organization-id")
    try:
        if len(selectors) != 1:
            raise ValueError()
        organization_id = UUID(selectors[0])
    except ValueError:
        raise Forbidden() from None
    registry = getattr(request.app.state, "ai_provider_registry", None)
    if registry is None:
        raise AIProviderNotConfigured()
    settings = request.app.state.settings

    @asynccontextmanager
    async def transaction():
        async with protected_session(user) as session:
            identity = SQLAlchemyIdentityRepository(session)
            context, _ = await IdentityService(
                identity, organization_context_installer=identity.establish_organization_context,
            ).organization(user, organization_id)
            request.state.organization_id = str(context.organization_id)
            yield AIService(
                context,
                AIRepository(session, context.organization_id, context.user_id),
                ContentRepository(session, context.organization_id),
                IdempotencyStore(session, context),
                AIAuditWriter(session, context, request.state.correlation_id),
                ContentAuditWriter(session, context, request.state.correlation_id),
                settings,
            )

    return AICoordinator(transaction, registry)
