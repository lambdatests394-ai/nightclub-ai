"""Async persistence boundary. Every organization read is membership-scoped."""
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.modules.identity.models import Organization, OrganizationMember, Profile
from backend.app.modules.identity.policy import MemberRole, OrganizationContext
from backend.app.core.database_security import establish_organization_context


@dataclass(frozen=True)
class Membership:
    user_id: UUID
    organization: Organization
    role: str


class IdentityRepository(Protocol):
    async def establish_organization_context(self, context: OrganizationContext) -> None: ...
    async def get_profile(self, user_id: UUID) -> Profile | None: ...
    async def get_membership(self, user_id: UUID, organization_id: UUID) -> Membership | None: ...
    async def list_memberships(self, user_id: UUID, after: UUID | None, limit: int) -> list[Membership]: ...


class SQLAlchemyIdentityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def establish_organization_context(self, context: OrganizationContext) -> None:
        await establish_organization_context(self._session, context)

    async def get_profile(self, user_id: UUID) -> Profile | None:
        return await self._session.scalar(select(Profile).where(Profile.id == user_id))

    @staticmethod
    def _membership_query(user_id: UUID):
        return (
            select(Organization, OrganizationMember.role)
            .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
            .join(Profile, Profile.id == OrganizationMember.user_id)
            .where(
                OrganizationMember.user_id == user_id,
                Profile.is_active.is_(True), Organization.is_active.is_(True),
                OrganizationMember.role.in_([role.value for role in MemberRole]),
            )
        )

    async def get_membership(self, user_id: UUID, organization_id: UUID) -> Membership | None:
        row = (await self._session.execute(
            self._membership_query(user_id).where(Organization.id == organization_id)
        )).one_or_none()
        return Membership(user_id, row[0], row[1]) if row else None

    async def list_memberships(self, user_id: UUID, after: UUID | None, limit: int) -> list[Membership]:
        statement = self._membership_query(user_id)
        if after is not None:
            statement = statement.where(Organization.id > after)
        rows = await self._session.execute(statement.order_by(Organization.id).limit(limit))
        return [Membership(user_id, row[0], row[1]) for row in rows]
