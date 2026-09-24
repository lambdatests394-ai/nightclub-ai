"""AI generation, human apply, and minimal organization daily usage ledger.

Requires the exact frozen 0006 baseline. This migration creates no role,
changes no role attributes, grants no DELETE, and cannot be downgraded safely.
"""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import re

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "20260923_0007"
down_revision = "20260922_0006"
branch_labels = None
depends_on = None


def previous_snapshot():
    spec = spec_from_file_location(
        "prompt9_frozen_0006", Path(__file__).with_name("20260922_0006_asset_business_access.py")
    )
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BASE = previous_snapshot()
ROLE, USER, ORG, TENANT = BASE.ROLE, BASE.USER, BASE.ORG, BASE.TENANT
TABLES = BASE.TABLES + ("ai_daily_usage",)
GENERATION_OWNER = f"{TENANT} AND created_by = {USER}"
GENERATION_CONTENT = f"""EXISTS (
    SELECT 1 FROM public.content_items ci
    WHERE ci.id = ai_generation_requests.content_item_id
      AND ci.organization_id = {ORG}
      AND ci.status IN ('draft','changes_requested'))"""
AI_VERSION = f"""EXISTS (
    SELECT 1 FROM public.content_items ci
    JOIN public.ai_generation_requests g
      ON g.content_item_id = ci.id
     AND g.organization_id = ci.organization_id
    WHERE ci.id = content_versions.content_item_id
      AND ci.organization_id = {ORG}
      AND ci.status IN ('draft','changes_requested')
      AND content_versions.version_no = ci.current_version_no + 1
      AND g.id = content_versions.ai_generation_id
      AND g.created_by = {USER}
      AND g.status = 'succeeded'
      AND content_versions.body IN (
          SELECT variant.value->>'body'
          FROM jsonb_array_elements(g.output->'variants') variant(value))
) AND created_by = {USER} AND source = 'ai'
  AND ai_generation_id IS NOT NULL AND payload = '{{}}'::jsonb"""

POLICIES = {
    "ai_generation_business_select": (
        "ai_generation_requests", "SELECT", GENERATION_OWNER, None,
    ),
    "ai_generation_business_insert": (
        "ai_generation_requests", "INSERT", None,
        f"""{GENERATION_OWNER} AND {GENERATION_CONTENT}
        AND provider IN ('openai','gemini')
        AND prompt_template_key = 'facebook_event_v1' AND prompt_template_version = 1
        AND status = 'queued' AND output IS NULL AND provider_request_id IS NULL
        AND input_tokens IS NULL AND output_tokens IS NULL
        AND estimated_cost_usd IS NULL AND error_code IS NULL""",
    ),
    "ai_generation_business_update": (
        "ai_generation_requests", "UPDATE", GENERATION_OWNER,
        f"""{GENERATION_OWNER} AND status IN ('running','succeeded','failed')""",
    ),
    "content_versions_ai_insert": ("content_versions", "INSERT", None, AI_VERSION),
    "audit_ai_insert": (
        "audit_logs", "INSERT", None,
        f"""{TENANT} AND actor_type = 'user' AND actor_id = ({USER})::text
        AND entity_type = 'ai_generation'
        AND action IN ('ai.generation_requested','ai.generation_succeeded',
                       'ai.generation_failed','ai.generation_applied')""",
    ),
    "ai_daily_usage_business_all": (
        "ai_daily_usage", "ALL", TENANT, TENANT,
    ),
}

INSERT_COLUMNS = {
    "ai_generation_requests": {
        "id", "organization_id", "content_item_id", "provider", "model",
        "prompt_template_key", "prompt_template_version", "input_redacted", "output",
        "provider_request_id", "input_tokens", "output_tokens", "estimated_cost_usd",
        "status", "error_code", "created_by",
    },
    "ai_daily_usage": {"organization_id", "usage_date", "estimated_cost_usd"},
    "content_versions": {"ai_generation_id"},
}
UPDATE_COLUMNS = {
    "ai_generation_requests": {
        "status", "output", "provider_request_id", "input_tokens", "output_tokens",
        "estimated_cost_usd", "error_code", "updated_at",
    },
    "ai_daily_usage": {"estimated_cost_usd", "updated_at"},
}


def historical_policies():
    return {**BASE.historical_policies(), **BASE.POLICIES}


def normalized(expression):
    parts = re.split(r"('(?:[^']|'')*')", expression or "")
    for index in range(0, len(parts), 2):
        value = parts[index].replace("public.", "")
        value = re.sub(r"::(?:text|campaign_status|content_status|ai_status)\b", "", value)
        parts[index] = re.sub(r'[\s()"]', '', value).lower()
    return re.sub(r"=anyarray\[([^]]+)\]", r"in\1", "".join(parts))


def policy_catalog_snapshot(bind):
    rows = bind.execute(sa.text(
        "SELECT tablename,policyname,roles,cmd,permissive,qual,with_check "
        "FROM pg_policies WHERE schemaname='public' ORDER BY policyname,tablename"
    )).all()
    return tuple(
        (table, name, tuple(roles), command, permissive, using, check)
        for table, name, roles, command, permissive, using, check in rows
    )


def verify_baseline(bind):
    if bind.scalar(sa.text("SELECT version_num FROM alembic_version")) != down_revision:
        raise RuntimeError("0007 requires exact 0006 baseline")
    role = bind.execute(sa.text("""SELECT oid,rolcanlogin,rolinherit,rolsuper,rolcreatedb,
        rolcreaterole,rolreplication,rolbypassrls FROM pg_roles WHERE rolname=:role"""),
        {"role": ROLE}).mappings().one_or_none()
    if (role is None or not role["rolcanlogin"] or any(role[key] for key in (
            "rolinherit", "rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls"))):
        raise RuntimeError("Unsafe runtime role")
    if bind.scalar(sa.text("SELECT EXISTS(SELECT 1 FROM pg_auth_members WHERE member=:oid)"), {"oid": role["oid"]}):
        raise RuntimeError("Runtime memberships forbidden")
    if bind.scalar(sa.text("SELECT has_schema_privilege(:role,'public','CREATE')"), {"role": ROLE}):
        raise RuntimeError("Runtime schema CREATE forbidden")
    tables = bind.execute(sa.text("""SELECT c.relname,c.relowner,c.relrowsecurity,c.relforcerowsecurity
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind='r'
          AND c.relname=ANY(CAST(:tables AS text[]))"""), {"tables": list(BASE.TABLES)}).all()
    if len(tables) != 20 or any(owner == role["oid"] or not rls or not force
                                for _, owner, rls, force in tables):
        raise RuntimeError("Expected 20 nonowned ENABLE/FORCE baseline tables")
    historical_snapshot = policy_catalog_snapshot(bind)
    if len(historical_snapshot) != 24:
        raise RuntimeError("Expected exactly 24 historical policies before 0007")
    BASE.verify_grants(bind)
    if bind.scalar(sa.text("""SELECT EXISTS (
        SELECT 1 FROM public.content_versions WHERE ai_generation_id IS NOT NULL
        GROUP BY ai_generation_id HAVING count(*) > 1)""")):
        raise RuntimeError("Duplicate AI generation applications require manual review")
    if bind.scalar(sa.text("SELECT to_regclass('public.ai_daily_usage') IS NOT NULL")):
        raise RuntimeError("Unexpected ai_daily_usage table before 0007")
    return historical_snapshot


def verify_policies(bind, historical_snapshot=None):
    rows = policy_catalog_snapshot(bind)
    prompt9_keys = {(table, name) for name, (table, _, _, _) in POLICIES.items()}
    prompt9_rows = tuple(row for row in rows if (row[0], row[1]) in prompt9_keys)
    if len(rows) != 30 or len(prompt9_rows) != 6 or {row[1] for row in prompt9_rows} != set(POLICIES):
        raise RuntimeError("Expected exactly 30 policies after 0007")
    if historical_snapshot is not None:
        if len(historical_snapshot) != 24:
            raise RuntimeError("Expected exactly 24 historical policies before 0007")
        historical_rows = tuple(row for row in rows if (row[0], row[1]) not in prompt9_keys)
        if historical_rows != historical_snapshot:
            raise RuntimeError("Historical policy catalog drift after 0007")
    for table, name, roles, command, permissive, using, check in prompt9_rows:
        et, ec, eu, ew = POLICIES[name]
        if (table != et or command != ec or roles != (ROLE,)
                or permissive != "PERMISSIVE"
                or normalized(using) != normalized(eu)
                or normalized(check) != normalized(ew)):
            raise RuntimeError("Policy drift after 0007")


def verify_grants(bind):
    historical_inserts = {**BASE.BASE.INSERT_COLUMNS, **BASE.INSERT_COLUMNS}
    historical_updates = {
        **BASE.BASE.BASE.UPDATE_COLUMNS, **BASE.BASE.UPDATE_COLUMNS, **BASE.UPDATE_COLUMNS,
    }
    inserts = {name: set(values) for name, values in historical_inserts.items()}
    updates = {name: set(values) for name, values in historical_updates.items()}
    for table, columns in INSERT_COLUMNS.items():
        inserts.setdefault(table, set()).update(columns)
    for table, columns in UPDATE_COLUMNS.items():
        updates.setdefault(table, set()).update(columns)
    for table in TABLES:
        full = set()
        if table in BASE.BASE.BASE.BOOTSTRAP or table in {
            "campaigns", "idempotency_keys", "content_items", "content_versions",
            "assets", "content_assets", "ai_generation_requests", "ai_daily_usage",
        }:
            full.add("SELECT")
        if table in {"campaigns", "idempotency_keys", "audit_logs"}:
            full.add("INSERT")
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            actual = bind.scalar(sa.text(
                "SELECT has_table_privilege(:role,:table,:privilege)"
            ), {"role": ROLE, "table": "public." + table, "privilege": privilege})
            if bool(actual) != (privilege in full):
                raise RuntimeError("Unexpected table privilege after 0007")
        columns = bind.execute(sa.text(
            "SELECT attname FROM pg_attribute WHERE attrelid=to_regclass(:table) "
            "AND attnum>0 AND NOT attisdropped"
        ), {"table": "public." + table}).scalars().all()
        for column in columns:
            for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
                expected = privilege in full
                expected |= privilege == "INSERT" and column in inserts.get(table, set())
                expected |= privilege == "UPDATE" and column in updates.get(table, set())
                expected |= (privilege == "SELECT" and table == "platform_connections"
                             and column in BASE.BASE.CONNECTION_COLUMNS)
                actual = bind.scalar(sa.text(
                    "SELECT has_column_privilege(:role,:table,:column,:privilege)"
                ), {"role": ROLE, "table": "public." + table,
                    "column": column, "privilege": privilege})
                if bool(actual) != bool(expected):
                    raise RuntimeError("Unexpected column privilege after 0007")
    role_oid = bind.scalar(sa.text("SELECT oid FROM pg_roles WHERE rolname=:role"), {"role": ROLE})
    tables = bind.execute(sa.text("""SELECT c.relname,c.relowner,c.relrowsecurity,c.relforcerowsecurity
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind='r'
          AND c.relname=ANY(CAST(:tables AS text[]))"""), {"tables": list(TABLES)}).all()
    if len(tables) != 21 or any(owner == role_oid or not rls or not force
                                for _, owner, rls, force in tables):
        raise RuntimeError("Expected 21 nonowned ENABLE/FORCE application tables")
    if bind.scalar(sa.text("""SELECT EXISTS(SELECT 1 FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace CROSS JOIN LATERAL aclexplode(c.relacl) a
        WHERE n.nspname='public' AND c.relname=ANY(CAST(:tables AS text[])) AND a.grantee=0)
        OR EXISTS(SELECT 1 FROM pg_attribute at JOIN pg_class c ON c.oid=at.attrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace CROSS JOIN LATERAL aclexplode(at.attacl) a
        WHERE n.nspname='public' AND c.relname=ANY(CAST(:tables AS text[])) AND a.grantee=0)"""),
        {"tables": list(TABLES)}):
        raise RuntimeError("PUBLIC application grants forbidden")
    for principal in ("anon", "authenticated", "service_role"):
        if bind.scalar(sa.text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:role)"), {"role": principal}):
            for table in TABLES:
                if bind.scalar(sa.text("""SELECT has_any_column_privilege(:role,:table,'SELECT')
                    OR has_any_column_privilege(:role,:table,'INSERT')
                    OR has_any_column_privilege(:role,:table,'UPDATE')
                    OR has_table_privilege(:role,:table,'DELETE')"""),
                    {"role": principal, "table": "public." + table}):
                    raise RuntimeError("Supabase principal application access forbidden")
    if bind.scalar(sa.text("SELECT EXISTS(SELECT 1 FROM pg_proc WHERE pronamespace='public'::regnamespace AND prosecdef)")):
        raise RuntimeError("Unexpected SECURITY DEFINER")
    for privilege in ("USAGE", "SELECT", "UPDATE"):
        if bind.scalar(sa.text(
                "SELECT has_sequence_privilege(:role,'public.audit_logs_id_seq',:privilege)"),
                {"role": ROLE, "privilege": privilege}):
            raise RuntimeError("Unexpected audit sequence privilege")


def upgrade():
    if context.is_offline_mode():
        raise RuntimeError("0007 requires online baseline verification")
    bind = op.get_bind()
    historical_snapshot = verify_baseline(bind)
    op.create_table(
        "ai_daily_usage",
        sa.Column("organization_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("estimated_cost_usd", sa.Numeric(12, 6), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("organization_id", "usage_date", name="pk_ai_daily_usage"),
        sa.ForeignKeyConstraint(["organization_id"], ["public.organizations.id"],
                                ondelete="RESTRICT", onupdate="RESTRICT",
                                name="fk_ai_daily_usage_organization"),
        sa.CheckConstraint("estimated_cost_usd >= 0", name="ai_daily_usage_nonnegative_cost"),
        schema="public",
    )
    op.execute("ALTER TABLE public.ai_daily_usage ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.ai_daily_usage FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE FUNCTION public.enforce_ai_daily_usage_monotonic()
        RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        IF NEW.organization_id IS DISTINCT FROM OLD.organization_id
           OR NEW.usage_date IS DISTINCT FROM OLD.usage_date
           OR NEW.estimated_cost_usd < OLD.estimated_cost_usd THEN
            RAISE EXCEPTION 'AI daily usage is immutable and monotonic' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
        END; $$""")
    op.execute("""CREATE TRIGGER ai_daily_usage_monotonic
        BEFORE UPDATE ON public.ai_daily_usage FOR EACH ROW
        EXECUTE FUNCTION public.enforce_ai_daily_usage_monotonic()""")
    op.execute("""CREATE TRIGGER ai_daily_usage_set_updated_at
        BEFORE UPDATE ON public.ai_daily_usage FOR EACH ROW
        EXECUTE FUNCTION public.set_updated_at()""")
    op.execute("""CREATE FUNCTION public.enforce_ai_generation_transition()
        RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        IF OLD.status = NEW.status OR NOT (
            (OLD.status = 'queued' AND NEW.status = 'running') OR
            (OLD.status = 'running' AND NEW.status IN ('succeeded','failed'))
        ) THEN
            RAISE EXCEPTION 'Invalid AI generation transition' USING ERRCODE='23514';
        END IF;
        IF NEW.status = 'running' AND (NEW.output IS NOT NULL OR NEW.provider_request_id IS NOT NULL
            OR NEW.input_tokens IS NOT NULL OR NEW.output_tokens IS NOT NULL
            OR NEW.estimated_cost_usd IS NOT NULL OR NEW.error_code IS NOT NULL) THEN
            RAISE EXCEPTION 'Invalid running AI generation' USING ERRCODE='23514';
        END IF;
        IF NEW.status = 'succeeded' AND (NEW.output IS NULL OR NEW.provider_request_id IS NULL
            OR btrim(NEW.provider_request_id) = '' OR NEW.input_tokens IS NULL OR NEW.input_tokens < 0
            OR NEW.output_tokens IS NULL OR NEW.output_tokens < 0
            OR NEW.estimated_cost_usd IS NULL OR NEW.estimated_cost_usd < 0
            OR NEW.error_code IS NOT NULL) THEN
            RAISE EXCEPTION 'Invalid successful AI generation' USING ERRCODE='23514';
        END IF;
        IF NEW.status = 'failed' AND (NEW.output IS NOT NULL OR NEW.error_code IS NULL
            OR NEW.error_code NOT IN ('AI_PROVIDER_TIMEOUT','AI_PROVIDER_UNAVAILABLE',
                'AI_PROVIDER_RATE_LIMITED','AI_PROVIDER_AUTH_FAILED','AI_PROVIDER_PROTOCOL_ERROR',
                'AI_OUTPUT_INVALID','AI_OUTPUT_BLOCKED')) THEN
            RAISE EXCEPTION 'Invalid failed AI generation' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
        END; $$""")
    op.execute("""CREATE TRIGGER ai_generation_transition_guard
        BEFORE UPDATE OF status, output, provider_request_id, input_tokens, output_tokens,
                         estimated_cost_usd, error_code
        ON public.ai_generation_requests FOR EACH ROW
        EXECUTE FUNCTION public.enforce_ai_generation_transition()""")
    op.execute("""CREATE UNIQUE INDEX uq_content_versions_ai_generation_once
        ON public.content_versions(ai_generation_id) WHERE ai_generation_id IS NOT NULL""")
    for name, (table, command, using, check) in POLICIES.items():
        ddl = f"CREATE POLICY {name} ON public.{table} FOR {command} TO {ROLE}"
        if using is not None:
            ddl += f" USING ({using})"
        if check is not None:
            ddl += f" WITH CHECK ({check})"
        op.execute(ddl)
    op.execute("GRANT SELECT ON public.ai_generation_requests, public.ai_daily_usage TO nightclub_api")
    for table, columns in INSERT_COLUMNS.items():
        op.execute(f"GRANT INSERT ({', '.join(sorted(columns))}) ON public.{table} TO {ROLE}")
    for table, columns in UPDATE_COLUMNS.items():
        op.execute(f"GRANT UPDATE ({', '.join(sorted(columns))}) ON public.{table} TO {ROLE}")
    verify_policies(bind, historical_snapshot)
    verify_grants(bind)


def downgrade():
    raise RuntimeError("Unsafe security downgrade blocked; a reviewed compensating migration is required")
