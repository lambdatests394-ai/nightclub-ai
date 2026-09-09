"""Application identity and validated organization context; no token handling."""
import base64
import binascii
from uuid import UUID

from backend.app.modules.identity.errors import Forbidden, InvalidCursor
from backend.app.modules.identity.models import Organization, Profile
from backend.app.modules.identity.policy import CurrentUser, MemberRole, OrganizationContext, Permission, require_permission
from backend.app.modules.identity.repository import IdentityRepository, Membership


def encode_cursor(value: UUID) -> str:
    return base64.urlsafe_b64encode(value.bytes).decode().rstrip("=")


def decode_cursor(value: str | None) -> UUID | None:
    if value is None:
        return None
    try:
        if len(value) != 22:
            raise ValueError()
        decoded = UUID(bytes=base64.b64decode(value + "==", altchars=b"-_", validate=True))
        if encode_cursor(decoded) != value:
            raise ValueError()
        return decoded
    except (ValueError, binascii.Error):
        raise InvalidCursor() from None


class IdentityService:
    def __init__(self, repository: IdentityRepository) -> None:
        self._repository = repository

    async def active_profile(self, user: CurrentUser) -> Profile:
        profile = await self._repository.get_profile(user.user_id)
        if profile is None or profile.id != user.user_id or not profile.is_active:
            raise Forbidden()
        return profile

    @staticmethod
    def _context(user: CurrentUser, membership: Membership, organization_id: UUID) -> OrganizationContext:
        if (membership.user_id != user.user_id or membership.organization.id != organization_id
                or not membership.organization.is_active):
            raise Forbidden()
        try:
            role = MemberRole(membership.role)
        except (TypeError, ValueError):
            raise Forbidden() from None
        return OrganizationContext(organization_id, user.user_id, role)

    async def organization(self, user: CurrentUser, organization_id: UUID,
                           header_organization: str | None = None) -> tuple[OrganizationContext, Organization]:
        if header_organization is not None:
            try:
                if UUID(header_organization) != organization_id:
                    raise ValueError()
            except ValueError:
                raise Forbidden() from None
        await self.active_profile(user)
        membership = await self._repository.get_membership(user.user_id, organization_id)
        if membership is None:
            raise Forbidden()
        context = self._context(user, membership, organization_id)
        require_permission(context, Permission.ORGANIZATION_READ)
        return context, membership.organization

    async def organizations(self, user: CurrentUser, cursor: str | None, limit: int) -> tuple[list[Membership], str | None]:
        await self.active_profile(user)
        if not 1 <= limit <= 100:
            raise InvalidCursor()
        rows = await self._repository.list_memberships(user.user_id, decode_cursor(cursor), limit + 1)
        for row in rows:
            context = self._context(user, row, row.organization.id)
            require_permission(context, Permission.ORGANIZATION_READ)
        page = rows[:limit]
        next_cursor = encode_cursor(page[-1].organization.id) if len(rows) > limit else None
        return page, next_cursor
