"""Campaign vertical slice only; immutable security snapshot, online validation.

No role provisioning, schema redesign, SECURITY DEFINER, DELETE or worker grants.
Identity RBAC remains in FastAPI. Automatic security downgrade is blocked.
"""
import re
from alembic import context, op
import sqlalchemy as sa

revision = "20260910_0004"
down_revision = "20260909_0003"
branch_labels = None
depends_on = None

ROLE = "nightclub_api"
TABLES = (
    "organizations", "profiles", "organization_members", "platform_connections",
    "campaigns", "assets", "content_items", "content_versions", "content_assets",
    "review_decisions", "publication_jobs", "publication_attempts", "ai_generation_requests",
    "webhook_events", "whatsapp_conversations", "whatsapp_messages", "outbox_events",
    "automation_runs", "idempotency_keys", "audit_logs",
)
USER = "NULLIF(current_setting('app.user_id', true), '')::uuid"
ORG = "NULLIF(current_setting('app.organization_id', true), '')::uuid"
BOOTSTRAP = {
    "profiles": f"id = {USER} AND is_active",
    "organization_members": f"user_id = {USER} AND EXISTS (SELECT 1 FROM public.profiles p WHERE p.id = organization_members.user_id AND p.is_active)",
    "organizations": "is_active AND EXISTS (SELECT 1 FROM public.organization_members m WHERE m.organization_id = organizations.id)",
}
TENANT = f"""organization_id = {ORG} AND EXISTS (
    SELECT 1 FROM public.profiles p WHERE p.id = {USER} AND p.is_active
) AND EXISTS (
    SELECT 1 FROM public.organizations o
    JOIN public.organization_members m ON m.organization_id = o.id
    WHERE o.id = {ORG} AND o.is_active AND m.user_id = {USER}
)"""
POLICIES = {
    "campaigns_business_select": ("campaigns", "SELECT", TENANT, None),
    "campaigns_business_insert": ("campaigns", "INSERT", None, f"{TENANT} AND created_by = {USER} AND status = 'draft'"),
    "campaigns_business_update": ("campaigns", "UPDATE", TENANT, TENANT),
    "idempotency_business_select": ("idempotency_keys", "SELECT", f"{TENANT} AND actor_id = {USER}", None),
    "idempotency_business_insert": ("idempotency_keys", "INSERT", None, f"{TENANT} AND actor_id = {USER}"),
    "idempotency_business_update": ("idempotency_keys", "UPDATE", f"{TENANT} AND actor_id = {USER}", f"{TENANT} AND actor_id = {USER}"),
    "audit_campaign_insert": ("audit_logs", "INSERT", None, f"{TENANT} AND actor_type = 'user' AND actor_id = ({USER})::text AND entity_type = 'campaign' AND action IN ('campaign.created', 'campaign.updated', 'campaign.archived')"),
}
UPDATE_COLUMNS = {
    "campaigns": {"name", "objective", "brief", "starts_at", "ends_at", "status", "updated_at"},
    "idempotency_keys": {"state", "response_status", "response_body", "updated_at"},
}


def normalized(expression):
    # pg_get_expr adds parentheses, namespace qualification and text casts.
    return re.sub(r"[\s()\"]", "", (expression or "").replace("::text", "").replace("public.", "")).lower()


def check_access(bind, *, expanded):
    for table in TABLES:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            expected = privilege == "SELECT" and table in BOOTSTRAP
            if expanded:
                expected |= (table in {"campaigns", "idempotency_keys"} and privilege in {"SELECT", "INSERT"})
                expected |= table == "audit_logs" and privilege == "INSERT"
            actual = bind.scalar(sa.text("SELECT has_table_privilege(:role, :table, :privilege)"),
                                 {"role": ROLE, "table": "public." + table, "privilege": privilege})
            if bool(actual) != bool(expected):
                raise RuntimeError("Unexpected effective table privileges; no automatic repair")
        columns = bind.execute(sa.text("SELECT attname FROM pg_attribute WHERE attrelid = to_regclass(:table) AND attnum > 0 AND NOT attisdropped"),
                               {"table": "public." + table}).scalars().all()
        for column in columns:
            for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
                expected = table in BOOTSTRAP and privilege == "SELECT"
                if expanded:
                    expected |= table in {"campaigns", "idempotency_keys"} and privilege in {"SELECT", "INSERT"}
                    expected |= table == "audit_logs" and privilege == "INSERT"
                    expected |= privilege == "UPDATE" and column in UPDATE_COLUMNS.get(table, set())
                actual = bind.scalar(sa.text("SELECT has_column_privilege(:role, :table, :column, :privilege)"),
                                     {"role": ROLE, "table": "public." + table, "column": column, "privilege": privilege})
                if bool(actual) != bool(expected):
                    raise RuntimeError("Unexpected effective column privileges; no automatic repair")
    # GENERATED ALWAYS AS IDENTITY uses its internal sequence without granting
    # direct nextval/setval access. The audit writer must not request RETURNING.
    for privilege in ("USAGE", "SELECT", "UPDATE"):
        if bind.scalar(sa.text("SELECT has_sequence_privilege(:role, 'public.audit_logs_id_seq', :privilege)"),
                       {"role": ROLE, "privilege": privilege}):
            raise RuntimeError("Unexpected direct audit sequence privilege")


def upgrade():
    if context.is_offline_mode():
        raise RuntimeError("0004 requires online security baseline verification")
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT version_num FROM alembic_version")) != down_revision:
        raise RuntimeError("0004 requires exactly 0003")
    role = bind.execute(sa.text("""SELECT oid, rolcanlogin, rolinherit, rolsuper, rolcreatedb,
        rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname = :role"""), {"role": ROLE}).mappings().one_or_none()
    if role is None or not role["rolcanlogin"] or any(role[k] for k in ("rolinherit", "rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls")):
        raise RuntimeError("Unsafe runtime role")
    if bind.scalar(sa.text("SELECT EXISTS(SELECT 1 FROM pg_auth_members WHERE member = :oid)"), {"oid": role["oid"]}):
        raise RuntimeError("Runtime memberships are forbidden")
    if bind.scalar(sa.text("SELECT has_schema_privilege(:role, 'public', 'CREATE')"), {"role": ROLE}):
        raise RuntimeError("Runtime schema CREATE is forbidden")
    rows = bind.execute(sa.text("""SELECT c.relname, c.relowner, c.relrowsecurity, c.relforcerowsecurity
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind='r' AND c.relname=ANY(CAST(:tables AS text[]))"""), {"tables": list(TABLES)}).all()
    if len(rows) != 20 or any(owner == role["oid"] or not rls or not force for _, owner, rls, force in rows):
        raise RuntimeError("Expected 20 nonowned ENABLE/FORCE tables")
    policies = bind.execute(sa.text("SELECT tablename, policyname, roles, cmd, permissive, qual, with_check FROM pg_policies WHERE schemaname='public'")).all()
    if len(policies) != 3 or {r[0] for r in policies} != set(BOOTSTRAP):
        raise RuntimeError("Expected exactly three bootstrap policies")
    for table, name, roles, cmd, permissive, qual, check in policies:
        if (name != table + "_identity_select" or list(roles) != [ROLE] or cmd != "SELECT"
                or permissive != "PERMISSIVE" or check is not None or normalized(qual) != normalized(BOOTSTRAP[table])):
            raise RuntimeError("Bootstrap policy differs; no automatic repair")
    if bind.scalar(sa.text("SELECT EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.prosecdef)")):
        raise RuntimeError("Unexpected SECURITY DEFINER in public")
    # Refuse inherited/PUBLIC/optional Supabase access instead of adding a bypass.
    for principal in ("anon", "authenticated", "service_role"):
        if bind.scalar(sa.text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:role)"), {"role": principal}):
            for table in TABLES:
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    if bind.scalar(sa.text("SELECT has_any_column_privilege(:role, :table, :privilege)" if privilege != "DELETE" else "SELECT has_table_privilege(:role, :table, :privilege)"),
                                   {"role": principal, "table": "public." + table, "privilege": privilege}):
                        raise RuntimeError("Unexpected Supabase-principal business grants")
    check_access(bind, expanded=False)
    for name, (table, command, using, check) in POLICIES.items():
        ddl = f"CREATE POLICY {name} ON public.{table} FOR {command} TO {ROLE}"
        if using is not None:
            ddl += f" USING ({using})"
        if check is not None:
            ddl += f" WITH CHECK ({check})"
        op.execute(ddl)
    op.execute("GRANT SELECT, INSERT ON public.campaigns, public.idempotency_keys TO nightclub_api")
    for table, columns in UPDATE_COLUMNS.items():
        op.execute(f"GRANT UPDATE ({', '.join(sorted(columns))}) ON public.{table} TO nightclub_api")
    op.execute("GRANT INSERT ON public.audit_logs TO nightclub_api")
    check_access(bind, expanded=True)


def downgrade():
    raise RuntimeError("Unsafe security downgrade blocked; a reviewed compensating migration is required")
