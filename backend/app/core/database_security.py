"""Transaction-local identity, not JWT verification or business RBAC.

Only verified identity objects enter these setters. GUCs are not proof against
an attacker with arbitrary SQL execution or the runtime database credential.
No session-scoped SET, SET ROLE, claims propagation or privileged helper exists.
"""
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.modules.identity.errors import IdentityUnavailable
from backend.app.modules.identity.policy import CurrentUser, OrganizationContext

PROTECTED_TABLES = (
    "organizations", "profiles", "organization_members", "platform_connections",
    "campaigns", "assets", "content_items", "content_versions", "content_assets",
    "review_decisions", "publication_jobs", "publication_attempts", "ai_generation_requests",
    "webhook_events", "whatsapp_conversations", "whatsapp_messages", "outbox_events",
    "automation_runs", "idempotency_keys", "audit_logs",
)
BOOTSTRAP_TABLES = ("profiles", "organization_members", "organizations")

ROLE_CHECK = text("""
SELECT r.rolname, r.rolcanlogin, r.rolinherit, r.rolsuper, r.rolcreatedb,
       r.rolcreaterole, r.rolreplication, r.rolbypassrls,
       current_user = session_user AS direct_login,
       EXISTS (SELECT 1 FROM pg_auth_members m WHERE m.member = r.oid) AS memberships,
       has_schema_privilege(r.oid, 'public', 'CREATE') AS schema_create,
       (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r'
          AND c.relname = ANY(CAST(:tables AS text[]))) AS table_count,
       EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = ANY(CAST(:tables AS text[]))
          AND c.relowner = r.oid) AS owns_tables
FROM pg_roles r WHERE r.rolname = current_user
""")


async def verify_runtime_role(session: AsyncSession, expected_role: str) -> None:
    """Check each request; do not cache across engines or mutable test settings."""
    row = (await session.execute(ROLE_CHECK, {"tables": list(PROTECTED_TABLES)})).mappings().one_or_none()
    if (row is None or row["rolname"] != expected_role or not row["rolcanlogin"]
            or not row["direct_login"] or row["table_count"] != len(PROTECTED_TABLES)
            or any(row[key] for key in (
                "rolinherit", "rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication",
                "rolbypassrls", "memberships", "schema_create", "owns_tables",
            ))):
        raise IdentityUnavailable()


def _root(session: AsyncSession):
    transaction = session.get_transaction()
    if transaction is None or not transaction.is_active or session.in_nested_transaction():
        raise IdentityUnavailable()
    return transaction


async def establish_user_context(session: AsyncSession, user: CurrentUser) -> None:
    if not isinstance(user, CurrentUser) or not isinstance(user.user_id, UUID):
        raise IdentityUnavailable()
    transaction = _root(session)
    previous = (await session.execute(text("""
        SELECT NULLIF(current_setting('app.user_id', true), ''),
               NULLIF(current_setting('app.organization_id', true), '')
    """))).one()
    if previous != (None, None):
        # Persistent/session context is contamination, never a trusted fallback.
        await session.invalidate()
        raise IdentityUnavailable()
    await session.execute(text("SELECT set_config('app.user_id', :value, true)"),
                          {"value": str(user.user_id)})
    await session.execute(text("SELECT set_config('app.organization_id', :value, true)"), {"value": ""})
    session.info["security_context"] = (transaction, user.user_id)


async def establish_organization_context(session: AsyncSession, context: OrganizationContext) -> None:
    transaction = _root(session)
    if (not isinstance(context, OrganizationContext)
            or not isinstance(context.organization_id, UUID)
            or session.info.get("security_context") != (transaction, context.user_id)):
        raise IdentityUnavailable()
    await session.execute(text("SELECT set_config('app.organization_id', :value, true)"),
                          {"value": str(context.organization_id)})
