"""Prompt 6 protected session architecture, shared through the entire request."""
from uuid import UUID
from fastapi import Depends, Request

from backend.app.modules.content.audit import ContentAuditWriter
from backend.app.modules.content.repository import ContentRepository
from backend.app.modules.content.service import ContentService
from backend.app.modules.identity.dependencies import get_current_user, protected_session
from backend.app.modules.identity.errors import Forbidden
from backend.app.modules.identity.policy import CurrentUser
from backend.app.modules.identity.repository import SQLAlchemyIdentityRepository
from backend.app.modules.identity.service import IdentityService
from backend.app.shared.idempotency import IdempotencyStore


async def get_content_service(request: Request, user: CurrentUser = Depends(get_current_user)):
    selectors = request.headers.getlist("x-organization-id")
    try:
        if len(selectors) != 1:
            raise ValueError()
        organization_id = UUID(selectors[0])
    except ValueError:
        raise Forbidden() from None
    async with protected_session(user) as session:
        repository = SQLAlchemyIdentityRepository(session)
        context, _ = await IdentityService(repository,
            organization_context_installer=repository.establish_organization_context).organization(user, organization_id)
        request.state.organization_id = str(context.organization_id)
        yield ContentService(context, ContentRepository(session, context.organization_id),
            IdempotencyStore(session, context), ContentAuditWriter(session, context, request.state.correlation_id))
