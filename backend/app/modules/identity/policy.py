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
    CAMPAIGN_READ = "campaign:read"
    CAMPAIGN_WRITE = "campaign:write"
    CAMPAIGN_ARCHIVE = "campaign:archive"
    CONTENT_READ = "content:read"
    CONTENT_WRITE = "content:write"
    CONTENT_REVIEW = "content:review"
    ASSET_READ = "asset:read"
    ASSET_WRITE = "asset:write"


@dataclass(frozen=True)
class CurrentUser:
    user_id: UUID


@dataclass(frozen=True)
class OrganizationContext:
    organization_id: UUID
    user_id: UUID
    role: MemberRole


ROLE_POLICY = MappingProxyType({
    MemberRole.OWNER: frozenset({Permission.ORGANIZATION_READ, Permission.CAMPAIGN_READ, Permission.CAMPAIGN_WRITE, Permission.CAMPAIGN_ARCHIVE, Permission.CONTENT_READ, Permission.CONTENT_WRITE, Permission.CONTENT_REVIEW}),
    MemberRole.MANAGER: frozenset({Permission.ORGANIZATION_READ, Permission.CAMPAIGN_READ, Permission.CAMPAIGN_WRITE, Permission.CAMPAIGN_ARCHIVE, Permission.CONTENT_READ, Permission.CONTENT_WRITE, Permission.CONTENT_REVIEW}),
    MemberRole.EDITOR: frozenset({Permission.ORGANIZATION_READ, Permission.CAMPAIGN_READ, Permission.CAMPAIGN_WRITE, Permission.CONTENT_READ, Permission.CONTENT_WRITE}),
    MemberRole.REVIEWER: frozenset({Permission.ORGANIZATION_READ, Permission.CAMPAIGN_READ, Permission.CONTENT_READ, Permission.CONTENT_REVIEW}),
    MemberRole.OPERATOR: frozenset({Permission.ORGANIZATION_READ, Permission.CAMPAIGN_READ, Permission.CONTENT_READ}),
    MemberRole.VIEWER: frozenset({Permission.ORGANIZATION_READ, Permission.CAMPAIGN_READ, Permission.CONTENT_READ}),
})

# Preserve existing permissions; only the approved asset matrix is added.
ROLE_POLICY = MappingProxyType({
    role: permissions | {Permission.ASSET_READ} | (
        {Permission.ASSET_WRITE} if role in {MemberRole.OWNER, MemberRole.MANAGER, MemberRole.EDITOR} else set()
    ) for role, permissions in ROLE_POLICY.items()
})


def require_permission(context: OrganizationContext, permission: str) -> None:
    if permission not in ROLE_POLICY.get(context.role, frozenset()):
        raise Forbidden()
