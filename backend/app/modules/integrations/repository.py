"""Narrow tenant/Facebook persistence projections; ordinary reads never fetch secrets."""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.modules.integrations.models import FacebookOAuthState, PlatformConnection
from backend.app.modules.integrations.schemas import PlatformConnectionRead


@dataclass(frozen=True, slots=True)
class FacebookCredentialMaterial:
    connection_id: UUID
    organization_id: UUID
    platform: str
    external_account_id: str
    credential_key_version: int
    token_expires_at: datetime | None
    status: str
    credentials_ciphertext: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class OAuthStateRecord:
    id: UUID
    organization_id: UUID
    actor_id: UUID
    state_digest: str
    nonce_digest: str
    requested_page_id: str
    expires_at: datetime
    consumed_at: datetime | None


class FacebookConnectionRepository:
    def __init__(self, session: AsyncSession, organization_id: UUID, actor_id: UUID):
        self.session = session
        self.organization_id = organization_id
        self.actor_id = actor_id

    @staticmethod
    def _read_columns():
        return (
            PlatformConnection.id, PlatformConnection.platform,
            PlatformConnection.external_account_id, PlatformConnection.display_name,
            PlatformConnection.capabilities, PlatformConnection.token_expires_at,
            PlatformConnection.status, PlatformConnection.last_verified_at,
            PlatformConnection.last_error_code, PlatformConnection.last_error_at,
            PlatformConnection.created_at, PlatformConnection.updated_at,
        )

    def _read_query(self):
        return select(*self._read_columns()).where(
            PlatformConnection.organization_id == self.organization_id,
            PlatformConnection.platform == "facebook",
        )

    @staticmethod
    def _view(row) -> PlatformConnectionRead | None:
        return PlatformConnectionRead.model_validate(dict(row._mapping)) if row is not None else None

    async def list_facebook_connections(self) -> list[PlatformConnectionRead]:
        rows = await self.session.execute(self._read_query().order_by(PlatformConnection.id))
        return [self._view(row) for row in rows]

    async def get_facebook_connection(self, connection_id: UUID, *, lock: bool = False) -> PlatformConnectionRead | None:
        statement = self._read_query().where(PlatformConnection.id == connection_id)
        if lock:
            statement = statement.with_for_update()
        return self._view((await self.session.execute(statement)).one_or_none())

    async def get_by_external_page_id(self, page_id: str, *, lock: bool = False) -> PlatformConnectionRead | None:
        statement = self._read_query().where(PlatformConnection.external_account_id == page_id)
        if lock:
            statement = statement.with_for_update()
        return self._view((await self.session.execute(statement)).one_or_none())

    async def insert_verified_facebook_connection(
            self, *, connection_id: UUID, page_id: str, display_name: str,
            capabilities: dict, ciphertext: bytes, key_version: int,
            token_expires_at: datetime | None, verified_at: datetime) -> bool:
        statement = pg_insert(PlatformConnection.__table__).values(
            id=connection_id, organization_id=self.organization_id, platform="facebook",
            external_account_id=page_id, display_name=display_name, capabilities=capabilities,
            credentials_ciphertext=ciphertext, credential_key_version=key_version,
            token_expires_at=token_expires_at, status="active", last_verified_at=verified_at,
            last_error_code=None, last_error_at=None,
        ).on_conflict_do_nothing(
            index_elements=[PlatformConnection.organization_id, PlatformConnection.platform,
                            PlatformConnection.external_account_id]
        ).returning(PlatformConnection.id)
        return await self.session.scalar(statement) is not None

    async def reconnect_facebook_connection(
            self, connection_id: UUID, *, display_name: str, capabilities: dict,
            ciphertext: bytes, key_version: int, token_expires_at: datetime | None,
            verified_at: datetime) -> PlatformConnectionRead | None:
        result = await self.session.execute(update(PlatformConnection).where(
            PlatformConnection.id == connection_id,
            PlatformConnection.organization_id == self.organization_id,
            PlatformConnection.platform == "facebook",
        ).values(
            display_name=display_name, capabilities=capabilities,
            credentials_ciphertext=ciphertext, credential_key_version=key_version,
            token_expires_at=token_expires_at, status="active", last_verified_at=verified_at,
            last_error_code=None, last_error_at=None,
        ))
        if result.rowcount != 1:
            return None
        return await self.get_facebook_connection(connection_id)

    async def get_facebook_credentials(
            self, connection_id: UUID, *, lock: bool = False,
    ) -> FacebookCredentialMaterial | None:
        # This is the only connection read projection allowed to retrieve secret columns.
        statement = select(
            PlatformConnection.id, PlatformConnection.organization_id, PlatformConnection.platform,
            PlatformConnection.external_account_id, PlatformConnection.credential_key_version,
            PlatformConnection.token_expires_at, PlatformConnection.status,
            PlatformConnection.credentials_ciphertext,
        ).where(
            PlatformConnection.id == connection_id,
            PlatformConnection.organization_id == self.organization_id,
            PlatformConnection.platform == "facebook",
        )
        if lock:
            statement = statement.with_for_update()
        row = (await self.session.execute(statement)).one_or_none()
        return FacebookCredentialMaterial(*row) if row is not None else None

    async def mark_facebook_connection_error(
            self, connection_id: UUID, error_code: str, occurred_at: datetime,
            *, terminal_status: str | None = None) -> bool:
        values = {"last_error_code": error_code, "last_error_at": occurred_at}
        if terminal_status is not None:
            values["status"] = terminal_status
        result = await self.session.execute(update(PlatformConnection).where(
            PlatformConnection.id == connection_id,
            PlatformConnection.organization_id == self.organization_id,
            PlatformConnection.platform == "facebook",
        ).values(**values))
        return result.rowcount == 1

    async def insert_oauth_state(self, *, state_digest: str, nonce_digest: str,
                                 requested_page_id: str, expires_at: datetime) -> OAuthStateRecord:
        state_id = uuid4()
        await self.session.execute(insert(FacebookOAuthState.__table__).inline().values(
            id=state_id, organization_id=self.organization_id, actor_id=self.actor_id,
            state_digest=state_digest, nonce_digest=nonce_digest,
            requested_page_id=requested_page_id, expires_at=expires_at,
        ))
        return OAuthStateRecord(state_id, self.organization_id, self.actor_id, state_digest,
                                nonce_digest, requested_page_id, expires_at, None)

    async def get_oauth_state(self, digest: str, *, lock: bool = False) -> OAuthStateRecord | None:
        statement = select(
            FacebookOAuthState.id, FacebookOAuthState.organization_id, FacebookOAuthState.actor_id,
            FacebookOAuthState.state_digest, FacebookOAuthState.nonce_digest,
            FacebookOAuthState.requested_page_id, FacebookOAuthState.expires_at,
            FacebookOAuthState.consumed_at,
        ).where(
            FacebookOAuthState.state_digest == digest,
            FacebookOAuthState.organization_id == self.organization_id,
            FacebookOAuthState.actor_id == self.actor_id,
        )
        if lock:
            statement = statement.with_for_update()
        row = (await self.session.execute(statement)).one_or_none()
        return OAuthStateRecord(*row) if row is not None else None

    async def consume_oauth_state(self, state_id: UUID, consumed_at: datetime) -> bool:
        result = await self.session.execute(update(FacebookOAuthState).where(
            FacebookOAuthState.id == state_id,
            FacebookOAuthState.organization_id == self.organization_id,
            FacebookOAuthState.actor_id == self.actor_id,
            FacebookOAuthState.consumed_at.is_(None),
        ).values(consumed_at=consumed_at))
        return result.rowcount == 1
