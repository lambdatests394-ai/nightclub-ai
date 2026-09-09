"""Pure identity values and deny-by-default organizational policy."""
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from backend.app.modules.identity.errors import Forbidden


class MemberRole(StrEnum):
    OWNER = "owner"
    MANAGER = "manager"
    EDITOR = "editor"
    REVIEWER = "reviewer"
    OPERATOR = "operator"
    VIEWER = "viewer"


class Permission(StrEnum):
    ORGANIZATION_READ = "organization:read"


@dataclass(frozen=True)
class CurrentUser:
    user_id: UUID


@dataclass(frozen=True)
class OrganizationContext:
    organization_id: UUID
    user_id: UUID
    role: MemberRole


ROLE_POLICY = MappingProxyType({
    MemberRole.OWNER: frozenset({Permission.ORGANIZATION_READ}),
    MemberRole.MANAGER: frozenset({Permission.ORGANIZATION_READ}),
    MemberRole.EDITOR: frozenset({Permission.ORGANIZATION_READ}),
    MemberRole.REVIEWER: frozenset({Permission.ORGANIZATION_READ}),
    MemberRole.OPERATOR: frozenset({Permission.ORGANIZATION_READ}),
    MemberRole.VIEWER: frozenset({Permission.ORGANIZATION_READ}),
})


def require_permission(context: OrganizationContext, permission: str) -> None:
    if permission not in ROLE_POLICY.get(context.role, frozenset()):
        raise Forbidden()
