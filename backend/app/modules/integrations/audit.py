"""Structural Facebook audit; no token, state, nonce, digest or ciphertext values."""

from sqlalchemy import insert

from backend.app.modules.audit.models import AuditLog


class FacebookAuditWriter:
    def __init__(self, session, context, correlation_id):
        self.session, self.context, self.correlation_id = session, context, correlation_id

    async def oauth(self, action: str, state_id, page_id: str) -> None:
        await self.session.execute(insert(AuditLog.__table__).inline().values(
            organization_id=self.context.organization_id, actor_type="user",
            actor_id=str(self.context.user_id), action=action,
            entity_type="facebook_connection", entity_id=state_id,
            correlation_id=self.correlation_id,
            after={"organizationId": str(self.context.organization_id),
                   "requestedPageId": page_id},
        ))

    async def connection(self, action: str, connection) -> None:
        await self.session.execute(insert(AuditLog.__table__).inline().values(
            organization_id=self.context.organization_id, actor_type="user",
            actor_id=str(self.context.user_id), action=action,
            entity_type="facebook_connection", entity_id=connection.id,
            correlation_id=self.correlation_id,
            after={"organizationId": str(self.context.organization_id),
                   "connectionId": str(connection.id),
                   "externalAccountId": connection.external_account_id,
                   "platform": connection.platform, "status": connection.status,
                   "capabilityKeys": sorted(connection.capabilities)},
        ))
