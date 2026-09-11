"""Append-only content/review access; frozen 0004 security baseline required.

No schema redesign, role changes, helpers, DELETE, or historical SQL execution.
The previous revision is an immutable security snapshot, not application code.
"""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import re

from alembic import context, op
import sqlalchemy as sa

revision = "20260910_0005"
down_revision = "20260910_0004"
branch_labels = None
depends_on = None


def previous_snapshot():
    spec = spec_from_file_location("prompt7_frozen_0004", Path(__file__).with_name("20260910_0004_campaign_business_access.py"))
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BASE = previous_snapshot()
TABLES, ROLE, USER, ORG = BASE.TABLES, BASE.ROLE, BASE.USER, BASE.ORG
TENANT = BASE.TENANT
VERSION_TENANT = f"EXISTS (SELECT 1 FROM public.content_items ci WHERE ci.id = content_versions.content_item_id AND ci.organization_id = {ORG})"
REVIEW_TENANT = f"""EXISTS (SELECT 1 FROM public.content_items ci
    JOIN public.content_versions cv ON cv.content_item_id = ci.id
    WHERE ci.id = review_decisions.content_item_id AND ci.organization_id = {ORG}
    AND cv.id = review_decisions.content_version_id)"""
INSERT_COLUMNS = {
    "content_items": {"id", "organization_id", "campaign_id", "platform", "connection_id", "status", "current_version_no", "approved_version_no", "created_by"},
    "content_versions": {"id", "content_item_id", "version_no", "body", "title", "link_url", "payload", "source", "created_by"},
    "review_decisions": {"id", "content_item_id", "content_version_id", "decision", "comment", "decided_by", "decided_at"},
}
UPDATE_COLUMNS = {"content_items": {"status", "current_version_no", "approved_version_no", "updated_at"}}
CONNECTION_COLUMNS = {"id", "organization_id", "platform", "status"}
POLICIES = {
    "content_items_business_select": ("content_items", "SELECT", TENANT, None),
    "content_items_business_insert": ("content_items", "INSERT", None, f"""{TENANT}
        AND created_by = {USER} AND status = 'draft' AND current_version_no = 1
        AND approved_version_no IS NULL AND scheduled_for IS NULL AND published_at IS NULL
        AND external_post_id IS NULL AND last_error_code IS NULL AND last_error_message IS NULL
        AND (campaign_id IS NULL OR EXISTS (SELECT 1 FROM public.campaigns c
            WHERE c.id = content_items.campaign_id AND c.organization_id = {ORG}))"""),
    "content_items_business_update": ("content_items", "UPDATE", TENANT,
        f"{TENANT} AND status IN ('draft', 'in_review', 'changes_requested', 'approved')"),
    "content_versions_business_select": ("content_versions", "SELECT", VERSION_TENANT, None),
    "content_versions_business_insert": ("content_versions", "INSERT", None, f"{VERSION_TENANT} AND created_by = {USER} AND source = 'manual' AND ai_generation_id IS NULL"),
    "review_decisions_business_insert": ("review_decisions", "INSERT", None, f"{REVIEW_TENANT} AND decided_by = {USER}"),
    "platform_connections_content_select": ("platform_connections", "SELECT", TENANT, None),
    "audit_content_insert": ("audit_logs", "INSERT", None, f"""{TENANT} AND actor_type = 'user'
        AND actor_id = ({USER})::text AND entity_type = 'content'
        AND action IN ('content.created', 'content.version_created', 'content.submitted_for_review',
                       'content.approved', 'content.changes_requested')"""),
}


def normalized(expression):
    value = (expression or "").replace("public.", "")
    value = re.sub(r"::(?:text|campaign_status)\b", "", value)
    value = re.sub(r"[\s()\"]", "", value).lower()
    # PostgreSQL renders IN with string literals as = ANY (ARRAY[...]).
    return re.sub(r"=anyarray\[([^]]+)\]", r"in\1", value)


def verify_baseline(bind):
    if bind.scalar(sa.text("SELECT version_num FROM alembic_version")) != down_revision:
        raise RuntimeError("0005 requires exact 0004 baseline")
    role = bind.execute(sa.text("""SELECT oid,rolcanlogin,rolinherit,rolsuper,rolcreatedb,
        rolcreaterole,rolreplication,rolbypassrls FROM pg_roles WHERE rolname=:role"""), {"role": ROLE}).mappings().one_or_none()
    if role is None or not role["rolcanlogin"] or any(role[k] for k in ("rolinherit", "rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls")):
        raise RuntimeError("Unsafe runtime role")
    if bind.scalar(sa.text("SELECT EXISTS(SELECT 1 FROM pg_auth_members WHERE member=:oid)"), {"oid": role["oid"]}):
        raise RuntimeError("Runtime role membership forbidden")
    if bind.scalar(sa.text("SELECT has_schema_privilege(:role,'public','CREATE')"), {"role": ROLE}):
        raise RuntimeError("Runtime schema CREATE forbidden")
    tables = bind.execute(sa.text("""SELECT c.relname,c.relowner,c.relrowsecurity,c.relforcerowsecurity
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind='r' AND c.relname=ANY(CAST(:tables AS text[]))"""), {"tables": list(TABLES)}).all()
    if len(tables) != 20 or any(owner == role["oid"] or not rls or not force for _, owner, rls, force in tables):
        raise RuntimeError("Expected 20 nonowned ENABLE/FORCE tables")
    expected = dict(BASE.POLICIES)
    expected.update({table + "_identity_select": (table, "SELECT", expression, None) for table, expression in BASE.BOOTSTRAP.items()})
    rows = bind.execute(sa.text("SELECT policyname,tablename,roles,cmd,permissive,qual,with_check FROM pg_policies WHERE schemaname='public'")).all()
    if len(rows) != 10 or {row[0] for row in rows} != set(expected):
        raise RuntimeError("Expected exactly ten 0004 policies")
    for name, table, roles, command, permissive, using, check in rows:
        exp_table, exp_cmd, exp_using, exp_check = expected[name]
        if (table != exp_table or command != exp_cmd or list(roles) != [ROLE] or permissive != "PERMISSIVE"
                or normalized(using) != normalized(exp_using) or normalized(check) != normalized(exp_check)):
            raise RuntimeError("0004 policy drift; no automatic repair")
    if bind.scalar(sa.text("SELECT EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.prosecdef)")):
        raise RuntimeError("Unexpected SECURITY DEFINER")
    # PUBLIC column/table grants and optional Supabase principals must stay closed.
    if bind.scalar(sa.text("""SELECT EXISTS(SELECT 1 FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        CROSS JOIN LATERAL aclexplode(c.relacl) a WHERE n.nspname='public'
        AND c.relname=ANY(CAST(:tables AS text[])) AND a.grantee=0)
        OR EXISTS(SELECT 1 FROM pg_attribute at JOIN pg_class c ON c.oid=at.attrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace CROSS JOIN LATERAL aclexplode(at.attacl) a
        WHERE n.nspname='public' AND c.relname=ANY(CAST(:tables AS text[])) AND a.grantee=0)"""), {"tables": list(TABLES)}):
        raise RuntimeError("PUBLIC application grant forbidden")
    for principal in ("anon", "authenticated", "service_role"):
        if bind.scalar(sa.text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:role)"), {"role": principal}):
            for table in TABLES:
                for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
                    if bind.scalar(sa.text("SELECT has_any_column_privilege(:role,:table,:privilege)"), {"role": principal, "table": "public." + table, "privilege": privilege}):
                        raise RuntimeError("Supabase principal application access forbidden")
                for privilege in ("DELETE", "TRUNCATE", "TRIGGER"):
                    if bind.scalar(sa.text("SELECT has_table_privilege(:role,:table,:privilege)"), {"role": principal, "table": "public." + table, "privilege": privilege}):
                        raise RuntimeError("Supabase principal application access forbidden")
    BASE.check_access(bind, expanded=True)


def verify_grants(bind):
    for table in TABLES:
        full = set()
        if table in BASE.BOOTSTRAP or table in {"campaigns", "idempotency_keys", "content_items", "content_versions"}:
            full.add("SELECT")
        if table in {"campaigns", "idempotency_keys", "audit_logs"}:
            full.add("INSERT")
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            actual = bind.scalar(sa.text("SELECT has_table_privilege(:role,:table,:privilege)"), {"role": ROLE, "table": "public." + table, "privilege": privilege})
            if bool(actual) != (privilege in full):
                raise RuntimeError("Unexpected table privileges after 0005")
        columns = bind.execute(sa.text("SELECT attname FROM pg_attribute WHERE attrelid=to_regclass(:table) AND attnum>0 AND NOT attisdropped"), {"table": "public." + table}).scalars().all()
        for column in columns:
            for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
                expected = privilege in full
                expected |= privilege == "INSERT" and column in INSERT_COLUMNS.get(table, set())
                expected |= privilege == "UPDATE" and column in {**BASE.UPDATE_COLUMNS, **UPDATE_COLUMNS}.get(table, set())
                expected |= privilege == "SELECT" and table == "platform_connections" and column in CONNECTION_COLUMNS
                actual = bind.scalar(sa.text("SELECT has_column_privilege(:role,:table,:column,:privilege)"), {"role": ROLE, "table": "public." + table, "column": column, "privilege": privilege})
                if bool(actual) != bool(expected):
                    raise RuntimeError("Unexpected column privileges after 0005")
    for privilege in ("USAGE", "SELECT", "UPDATE"):
        if bind.scalar(sa.text("SELECT has_sequence_privilege(:role,'public.audit_logs_id_seq',:privilege)"), {"role": ROLE, "privilege": privilege}):
            raise RuntimeError("Unexpected audit sequence grant")


def upgrade():
    if context.is_offline_mode():
        raise RuntimeError("0005 requires online baseline verification")
    bind = op.get_bind()
    verify_baseline(bind)
    for name, (table, command, using, check) in POLICIES.items():
        ddl = f"CREATE POLICY {name} ON public.{table} FOR {command} TO {ROLE}"
        if using is not None:
            ddl += f" USING ({using})"
        if check is not None:
            ddl += f" WITH CHECK ({check})"
        op.execute(ddl)
    op.execute("GRANT SELECT ON public.content_items, public.content_versions TO nightclub_api")
    op.execute("GRANT SELECT (id, organization_id, platform, status) ON public.platform_connections TO nightclub_api")
    for table, columns in INSERT_COLUMNS.items():
        op.execute(f"GRANT INSERT ({', '.join(sorted(columns))}) ON public.{table} TO {ROLE}")
    for table, columns in UPDATE_COLUMNS.items():
        op.execute(f"GRANT UPDATE ({', '.join(sorted(columns))}) ON public.{table} TO {ROLE}")
    verify_grants(bind)


def downgrade():
    raise RuntimeError("Unsafe security downgrade blocked; a reviewed compensating migration is required")
