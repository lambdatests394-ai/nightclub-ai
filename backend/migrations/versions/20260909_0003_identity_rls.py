"""Identity-only RLS with least-privilege runtime access.

This revision does NOT execute historical sql/003_rls.sql, provision cluster
roles, set passwords, or create business/worker policies. Online catalog
verification is mandatory. Reversal requires a reviewed compensating migration.
"""
from alembic import context, op
import sqlalchemy as sa

revision = "20260909_0003"
down_revision = "20260907_0002"
branch_labels = None
depends_on = None

# Immutable migration snapshot; do not import mutable application metadata.
TABLES = (
    "organizations", "profiles", "organization_members", "platform_connections",
    "campaigns", "assets", "content_items", "content_versions", "content_assets",
    "review_decisions", "publication_jobs", "publication_attempts", "ai_generation_requests",
    "webhook_events", "whatsapp_conversations", "whatsapp_messages", "outbox_events",
    "automation_runs", "idempotency_keys", "audit_logs",
)
ROLE = "nightclub_api"
USER_ID = "NULLIF(current_setting('app.user_id', true), '')::uuid"
POLICIES = {
    "profiles": f"id = {USER_ID} AND is_active",
    "organization_members": f"""user_id = {USER_ID} AND EXISTS (
        SELECT 1 FROM public.profiles p WHERE p.id = organization_members.user_id AND p.is_active
    )""",
    "organizations": """is_active AND EXISTS (
        SELECT 1 FROM public.organization_members m WHERE m.organization_id = organizations.id
    )""",
}


def upgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("0003 requires online role, ownership and policy verification")
    bind = op.get_bind()
    role = bind.execute(sa.text("""
        SELECT oid, rolcanlogin, rolinherit, rolsuper, rolcreatedb,
               rolcreaterole, rolreplication, rolbypassrls
        FROM pg_roles WHERE rolname = :role
    """), {"role": ROLE}).mappings().one_or_none()
    if (role is None or not role["rolcanlogin"] or any(role[key] for key in (
            "rolinherit", "rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls"))):
        raise RuntimeError("0003 requires an externally provisioned, least-privilege nightclub_api LOGIN")
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM pg_auth_members WHERE member = :oid)"),
                   {"oid": role["oid"]}):
        raise RuntimeError("0003 refuses runtime role memberships; review externally")
    tables = bind.execute(sa.text("""
        SELECT c.oid, c.relname, c.relowner FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r'
          AND c.relname = ANY(CAST(:tables AS text[]))
    """), {"tables": list(TABLES)}).mappings().all()
    if len(tables) != 20 or any(row["relowner"] == role["oid"] for row in tables):
        raise RuntimeError("0003 requires all 20 tables, none owned by nightclub_api")
    if bind.scalar(sa.text("""
        SELECT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'public'
                       AND tablename = ANY(CAST(:tables AS text[])))
    """), {"tables": list(TABLES)}):
        raise RuntimeError("0003 refuses preexisting policies; do not apply historical sql/003")

    # Only object-scoped revocation. Never REVOKE schema public FROM PUBLIC.
    op.execute("REVOKE CREATE ON SCHEMA public FROM nightclub_api")
    if bind.scalar(sa.text("SELECT has_schema_privilege(:role, 'public', 'CREATE')"), {"role": ROLE}):
        raise RuntimeError("Runtime still has effective public CREATE; external review required")
    principals = ["PUBLIC", '"nightclub_api"']
    for name in ("anon", "authenticated", "service_role"):
        if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :name)"), {"name": name}):
            principals.append('"' + name + '"')
    recipients = ", ".join(principals)
    quote = bind.dialect.identifier_preparer.quote
    for table in TABLES:
        target = "public." + quote(table)
        op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {target} FROM {recipients}")
        # PostgreSQL table REVOKE also removes the corresponding column grants.
        op.execute(f"ALTER TABLE {target} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {target} FORCE ROW LEVEL SECURITY")
    op.execute(f"REVOKE ALL PRIVILEGES ON SEQUENCE public.audit_logs_id_seq FROM {recipients}")
    op.execute("GRANT USAGE ON SCHEMA public TO nightclub_api")
    for table, expression in POLICIES.items():
        op.execute(f"CREATE POLICY {table}_identity_select ON public.{table} "
                   f"FOR SELECT TO nightclub_api USING ({expression})")
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO nightclub_api")
    # REVOKE is not a negative ACL: fail if effective privileges remain through
    # another grant path. Do not silently claim least privilege from DDL alone.
    for table in TABLES:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            allowed = bind.scalar(sa.text("SELECT has_table_privilege(:role, :table, :privilege)"),
                                  {"role": ROLE, "table": "public." + table, "privilege": privilege})
            if allowed != (table in POLICIES and privilege == "SELECT"):
                raise RuntimeError("Unexpected effective runtime table privileges after revocation")
        for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
            allowed = bind.scalar(sa.text("SELECT has_any_column_privilege(:role, :table, :privilege)"),
                                  {"role": ROLE, "table": "public." + table, "privilege": privilege})
            if allowed != (table in POLICIES and privilege == "SELECT"):
                raise RuntimeError("Unexpected effective runtime column privileges after revocation")


def downgrade() -> None:
    raise RuntimeError("Unsafe security downgrade blocked; a reviewed compensating migration is required")
