"""Asset access and same-transaction attachment closure; exact frozen 0005 preflight."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import re

from alembic import context, op
import sqlalchemy as sa

revision = "20260922_0006"
down_revision = "20260910_0005"
branch_labels = None
depends_on = None


def previous_snapshot():
    spec = spec_from_file_location("prompt8_frozen_0005", Path(__file__).with_name("20260910_0005_content_business_access.py"))
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BASE = previous_snapshot()
TABLES, ROLE, USER, ORG, TENANT = BASE.TABLES, BASE.ROLE, BASE.USER, BASE.ORG, BASE.TENANT
INSERT_COLUMNS = {
    "assets": {"id", "organization_id", "storage_bucket", "storage_key", "kind", "mime_type", "byte_size", "sha256", "original_filename", "status", "uploaded_by"},
    "content_assets": {"content_version_id", "content_item_id", "organization_id", "asset_id", "position"},
}
UPDATE_COLUMNS = {"assets": {"status", "width", "height", "updated_at"}}
ATTACHMENT_TENANT = f"""{TENANT} AND EXISTS (
    SELECT 1 FROM public.content_items ci JOIN public.content_versions cv ON cv.content_item_id = ci.id
    WHERE ci.id = content_assets.content_item_id AND ci.organization_id = {ORG}
    AND cv.id = content_assets.content_version_id)"""
ATTACHMENT_CREATION = f"""{ATTACHMENT_TENANT} AND position BETWEEN 0 AND 9 AND EXISTS (
    SELECT 1 FROM public.content_items ci JOIN public.content_versions cv ON cv.content_item_id = ci.id
    WHERE ci.id = content_assets.content_item_id AND ci.organization_id = {ORG}
      AND cv.id = content_assets.content_version_id AND cv.version_no = ci.current_version_no
      AND ci.status = 'draft' AND cv.created_by = {USER}
      AND cv.attachment_creation_xid = pg_current_xact_id()
      AND cv.created_at = transaction_timestamp()
) AND EXISTS (
    SELECT 1 FROM public.assets a WHERE a.id = content_assets.asset_id
    AND a.organization_id = {ORG} AND a.status = 'ready')"""
POLICIES = {
    "assets_business_select": ("assets", "SELECT", TENANT, None),
    "assets_business_insert": ("assets", "INSERT", None, f"""{TENANT}
        AND uploaded_by = {USER} AND status = 'pending' AND deleted_at IS NULL
        AND kind = 'image' AND mime_type IN ('image/jpeg', 'image/png', 'image/webp')
        AND byte_size BETWEEN 1 AND 10485760 AND sha256 ~ '^[0-9a-f]{{64}}$'
        AND storage_bucket = 'nightclub-assets'
        AND storage_key ~ ('^org/' || organization_id::text || '/assets/' || id::text
            || '/[a-z0-9_-]{{1,80}}[.]' || CASE mime_type WHEN 'image/jpeg' THEN 'jpg'
                WHEN 'image/png' THEN 'png' WHEN 'image/webp' THEN 'webp' END || '$')
        AND width IS NULL AND height IS NULL AND duration_ms IS NULL"""),
    "assets_business_update": ("assets", "UPDATE", f"{TENANT} AND status = 'pending'", f"""{TENANT}
        AND status IN ('ready', 'rejected') AND deleted_at IS NULL AND duration_ms IS NULL
        AND ((status = 'ready' AND width IS NOT NULL AND height IS NOT NULL
              AND width BETWEEN 1 AND 8192 AND height BETWEEN 1 AND 8192
              AND width::bigint * height::bigint <= 20000000)
             OR (status = 'rejected' AND width IS NULL AND height IS NULL))"""),
    "content_assets_business_select": ("content_assets", "SELECT", ATTACHMENT_TENANT, None),
    "content_assets_business_insert": ("content_assets", "INSERT", None, ATTACHMENT_CREATION),
    "audit_asset_insert": ("audit_logs", "INSERT", None, f"""{TENANT} AND actor_type = 'user'
        AND actor_id = ({USER})::text AND entity_type = 'asset'
        AND action IN ('asset.upload_intent_created', 'asset.ready', 'asset.rejected')"""),
}


def historical_policies():
    return {**BASE.BASE.POLICIES, **BASE.POLICIES,
        **{table + "_identity_select": (table, "SELECT", expr, None) for table, expr in BASE.BASE.BOOTSTRAP.items()}}


def normalized(expression):
    # Normalize deparser syntax outside literals only; never erase literal whitespace.
    parts = re.split(r"('(?:[^']|'')*')", expression or "")
    for index in range(0, len(parts), 2):
        value = parts[index].replace("public.", "")
        value = re.sub(r"::(?:text|campaign_status|content_status)\b", "", value)
        parts[index] = re.sub(r'[\s()"]', '', value).lower()
    return re.sub(r"=anyarray\[([^]]+)\]", r"in\1", "".join(parts))


def verify_historical_policies(bind, *, expanded=False):
    expected = historical_policies()
    rows = bind.execute(sa.text("SELECT policyname,tablename,roles,cmd,permissive,qual,with_check FROM pg_policies WHERE schemaname='public'")).all()
    names = set(expected) | (set(POLICIES) if expanded else set())
    if {row[0] for row in rows} != names or len(rows) != len(names):
        raise RuntimeError("Unexpected policy set; frozen baseline drift")
    for name, table, roles, command, permissive, using, check in rows:
        if name not in expected:
            continue
        et, ec, eu, ew = expected[name]
        if (table != et or command != ec or list(roles) != [ROLE] or permissive != "PERMISSIVE"
                or normalized(using) != normalized(eu) or normalized(check) != normalized(ew)):
            raise RuntimeError("Historical policy drift; no automatic repair")


def verify_baseline(bind):
    if bind.scalar(sa.text("SELECT version_num FROM alembic_version")) != down_revision:
        raise RuntimeError("0006 requires exact 0005 baseline")
    role = bind.execute(sa.text("""SELECT oid,rolcanlogin,rolinherit,rolsuper,rolcreatedb,
        rolcreaterole,rolreplication,rolbypassrls FROM pg_roles WHERE rolname=:role"""), {"role": ROLE}).mappings().one_or_none()
    if role is None or not role["rolcanlogin"] or any(role[k] for k in ("rolinherit", "rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls")):
        raise RuntimeError("Unsafe runtime role")
    if bind.scalar(sa.text("SELECT EXISTS(SELECT 1 FROM pg_auth_members WHERE member=:oid)"), {"oid": role["oid"]}):
        raise RuntimeError("Runtime memberships forbidden")
    if bind.scalar(sa.text("SELECT has_schema_privilege(:role,'public','CREATE')"), {"role": ROLE}):
        raise RuntimeError("Runtime schema CREATE forbidden")
    tables = bind.execute(sa.text("""SELECT c.relname,c.relowner,c.relrowsecurity,c.relforcerowsecurity
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind='r' AND c.relname=ANY(CAST(:tables AS text[]))"""), {"tables": list(TABLES)}).all()
    if len(tables) != 20 or any(owner == role["oid"] or not rls or not force for _, owner, rls, force in tables):
        raise RuntimeError("Expected 20 nonowned ENABLE/FORCE tables")
    if bind.scalar(sa.text("SELECT EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.prosecdef)")):
        raise RuntimeError("Unexpected SECURITY DEFINER")
    if bind.scalar(sa.text("""SELECT EXISTS(SELECT 1 FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace CROSS JOIN LATERAL aclexplode(c.relacl) a
        WHERE n.nspname='public' AND c.relname=ANY(CAST(:tables AS text[])) AND a.grantee=0)
        OR EXISTS(SELECT 1 FROM pg_attribute at JOIN pg_class c ON c.oid=at.attrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace CROSS JOIN LATERAL aclexplode(at.attacl) a
        WHERE n.nspname='public' AND c.relname=ANY(CAST(:tables AS text[])) AND a.grantee=0)"""), {"tables": list(TABLES)}):
        raise RuntimeError("PUBLIC application grants forbidden")
    for principal in ("anon", "authenticated", "service_role"):
        if bind.scalar(sa.text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:role)"), {"role": principal}):
            for table in TABLES:
                for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
                    if bind.scalar(sa.text("SELECT has_any_column_privilege(:role,:table,:privilege)"), {"role": principal, "table": "public." + table, "privilege": privilege}):
                        raise RuntimeError("Supabase principal application access forbidden")
                for privilege in ("DELETE", "TRUNCATE", "TRIGGER"):
                    if bind.scalar(sa.text("SELECT has_table_privilege(:role,:table,:privilege)"), {"role": principal, "table": "public." + table, "privilege": privilege}):
                        raise RuntimeError("Supabase principal application access forbidden")
    verify_historical_policies(bind)
    BASE.verify_grants(bind)


def verify_grants(bind):
    inserts = {**BASE.INSERT_COLUMNS, **INSERT_COLUMNS}
    updates = {**BASE.BASE.UPDATE_COLUMNS, **BASE.UPDATE_COLUMNS, **UPDATE_COLUMNS}
    for table in TABLES:
        full = set()
        if table in BASE.BASE.BOOTSTRAP or table in {"campaigns", "idempotency_keys", "content_items", "content_versions", "assets", "content_assets"}:
            full.add("SELECT")
        if table in {"campaigns", "idempotency_keys", "audit_logs"}:
            full.add("INSERT")
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            actual = bind.scalar(sa.text("SELECT has_table_privilege(:role,:table,:privilege)"), {"role": ROLE, "table": "public." + table, "privilege": privilege})
            if bool(actual) != (privilege in full):
                raise RuntimeError("Unexpected table privileges after 0006")
        columns = bind.execute(sa.text("SELECT attname FROM pg_attribute WHERE attrelid=to_regclass(:table) AND attnum>0 AND NOT attisdropped"), {"table": "public." + table}).scalars().all()
        for column in columns:
            for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
                expected = privilege in full
                expected |= privilege == "INSERT" and column in inserts.get(table, set())
                expected |= privilege == "UPDATE" and column in updates.get(table, set())
                expected |= privilege == "SELECT" and table == "platform_connections" and column in BASE.CONNECTION_COLUMNS
                actual = bind.scalar(sa.text("SELECT has_column_privilege(:role,:table,:column,:privilege)"), {"role": ROLE, "table": "public." + table, "column": column, "privilege": privilege})
                if bool(actual) != bool(expected):
                    raise RuntimeError("Unexpected column privileges after 0006")
    for privilege in ("USAGE", "SELECT", "UPDATE"):
        if bind.scalar(sa.text("SELECT has_sequence_privilege(:role,'public.audit_logs_id_seq',:privilege)"), {"role": ROLE, "privilege": privilege}):
            raise RuntimeError("Unexpected audit sequence privilege")


def upgrade():
    if context.is_offline_mode():
        raise RuntimeError("0006 requires online baseline verification")
    bind = op.get_bind()
    verify_baseline(bind)
    op.execute("ALTER TABLE public.content_versions ADD COLUMN attachment_creation_xid xid8")
    op.execute("ALTER TABLE public.content_versions ALTER COLUMN attachment_creation_xid SET DEFAULT pg_current_xact_id()")
    for name, (table, command, using, check) in POLICIES.items():
        ddl = f"CREATE POLICY {name} ON public.{table} FOR {command} TO {ROLE}"
        if using is not None:
            ddl += f" USING ({using})"
        if check is not None:
            ddl += f" WITH CHECK ({check})"
        op.execute(ddl)
    op.execute("GRANT SELECT ON public.assets, public.content_assets TO nightclub_api")
    for table, columns in INSERT_COLUMNS.items():
        op.execute(f"GRANT INSERT ({', '.join(sorted(columns))}) ON public.{table} TO {ROLE}")
    for table, columns in UPDATE_COLUMNS.items():
        op.execute(f"GRANT UPDATE ({', '.join(sorted(columns))}) ON public.{table} TO {ROLE}")
    verify_grants(bind)
    verify_historical_policies(bind, expanded=True)


def downgrade():
    raise RuntimeError("Unsafe security downgrade blocked; a reviewed compensating migration is required")
