"""Function-scoped dependency commits before FastAPI sends the response."""
from fastapi import Depends, Request
from uuid import UUID

from backend.app.modules.audit.writer import CampaignAuditWriter
from backend.app.modules.campaigns.repository import CampaignRepository
from backend.app.modules.campaigns.service import CampaignService
from backend.app.modules.identity.dependencies import get_current_user, protected_session
from backend.app.modules.identity.errors import Forbidden
from backend.app.modules.identity.policy import CurrentUser
from backend.app.modules.identity.repository import SQLAlchemyIdentityRepository
from backend.app.modules.identity.service import IdentityService
from backend.app.shared.idempotency import IdempotencyStore


async def get_campaign_service(request: Request, user: CurrentUser = Depends(get_current_user)):
    selectors = request.headers.getlist("x-organization-id")
    try:
        if len(selectors) != 1:
            raise ValueError()
        organization_id = UUID(selectors[0])
    except ValueError:
        raise Forbidden() from None
    async with protected_session(user) as session:
        repository = SQLAlchemyIdentityRepository(session)
        identity = IdentityService(repository, organization_context_installer=repository.establish_organization_context)
        context, _ = await identity.organization(user, organization_id)
        request.state.organization_id = str(context.organization_id)
        yield CampaignService(
            context, CampaignRepository(session, context.organization_id),
            IdempotencyStore(session, context),
            CampaignAuditWriter(session, context, request.state.correlation_id),
        )
