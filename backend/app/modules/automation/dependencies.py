"""Request composition for publication mutations in one protected transaction."""

from uuid import UUID

from fastapi import Depends, Request

from backend.app.modules.automation.audit import PublicationAuditWriter
from backend.app.modules.automation.repository import PublicationRepository
from backend.app.modules.automation.service import PublicationService
from backend.app.modules.content.repository import ContentRepository
from backend.app.modules.identity.dependencies import get_current_user, protected_session
from backend.app.modules.identity.errors import Forbidden
from backend.app.modules.identity.policy import CurrentUser
from backend.app.modules.identity.repository import SQLAlchemyIdentityRepository
from backend.app.modules.identity.service import IdentityService
from backend.app.modules.integrations.repository import FacebookConnectionRepository
from backend.app.shared.idempotency import IdempotencyStore


async def get_publication_service(
        request: Request, user: CurrentUser = Depends(get_current_user)):
    selectors = request.headers.getlist("x-organization-id")
    try:
        if len(selectors) != 1:
            raise ValueError()
        organization_id = UUID(selectors[0])
    except ValueError:
        raise Forbidden() from None

    async with protected_session(user) as session:
        identity = SQLAlchemyIdentityRepository(session)
        context, _ = await IdentityService(
            identity,
            organization_context_installer=identity.establish_organization_context,
        ).organization(user, organization_id)
        request.state.organization_id = str(context.organization_id)
        yield PublicationService(
            context,
            ContentRepository(session, context.organization_id),
            PublicationRepository(session, context.organization_id),
            FacebookConnectionRepository(session, context.organization_id, context.user_id),
            IdempotencyStore(session, context),
            PublicationAuditWriter(session, context, request.state.correlation_id),
        )
