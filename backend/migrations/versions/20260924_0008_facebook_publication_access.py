"""Facebook publication persistence and least-privilege runtime access.

Requires the exact frozen 0007 baseline. The revision creates no role, changes
no role attributes, grants no destructive privilege, and verifies historical
policy rows byte-for-byte before accepting the forward-only security change.
"""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import re

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg


revision = "20260924_0008"
down_revision = "20260923_0007"
branch_labels = None
depends_on = None


def previous_snapshot():
    spec = spec_from_file_location(
        "prompt10_frozen_0007",
        Path(__file__).with_name("20260923_0007_ai_generation_business_access.py"),
    )
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BASE = previous_snapshot()
ROLE, USER, ORG, TENANT = BASE.ROLE, BASE.USER, BASE.ORG, BASE.TENANT
TABLES = BASE.TABLES + ("facebook_oauth_states",)

OAUTH_OWNER = f"{TENANT} AND actor_id = {USER}"
JOB_TENANT = f"""EXISTS (
    SELECT 1 FROM public.content_items ci
    WHERE ci.id = publication_jobs.content_item_id
      AND ci.organization_id = {ORG})"""
ATTEMPT_TENANT = f"""EXISTS (
    SELECT 1 FROM public.publication_jobs pj
    JOIN public.content_items ci ON ci.id = pj.content_item_id
    WHERE pj.id = publication_attempts.publication_job_id
      AND ci.organization_id = {ORG})"""
PUBLICATION_CONTENT = (
    f"{TENANT} AND platform = 'facebook' "
    "AND status IN ('approved','scheduled','publishing','published','failed')"
)

POLICIES = {
    "facebook_oauth_states_business_select": (
        "facebook_oauth_states", "SELECT", OAUTH_OWNER, None,
    ),
    "facebook_oauth_states_business_insert": (
        "facebook_oauth_states", "INSERT", None,
        f"{OAUTH_OWNER} AND consumed_at IS NULL",
    ),
    "facebook_oauth_states_business_update": (
        "facebook_oauth_states", "UPDATE", OAUTH_OWNER, OAUTH_OWNER,
    ),
    "platform_connections_facebook_insert": (
        "platform_connections", "INSERT", None,
        f"{TENANT} AND platform = 'facebook'",
    ),
    "platform_connections_facebook_update": (
        "platform_connections", "UPDATE",
        f"{TENANT} AND platform = 'facebook'",
        f"{TENANT} AND platform = 'facebook'",
    ),
    "publication_jobs_business_select": (
        "publication_jobs", "SELECT", JOB_TENANT, None,
    ),
    "publication_jobs_business_insert": (
        "publication_jobs", "INSERT", None,
        f"""{JOB_TENANT} AND created_by = {USER}
        AND status = 'pending' AND attempt_count = 0
        AND lease_token IS NULL AND lease_expires_at IS NULL
        AND published_external_id IS NULL""",
    ),
    "publication_jobs_business_update": (
        "publication_jobs", "UPDATE", JOB_TENANT, JOB_TENANT,
    ),
    "publication_attempts_business_select": (
        "publication_attempts", "SELECT", ATTEMPT_TENANT, None,
    ),
    "publication_attempts_business_insert": (
        "publication_attempts", "INSERT", None,
        f"{ATTEMPT_TENANT} AND outcome = 'in_progress' AND finished_at IS NULL",
    ),
    "publication_attempts_business_update": (
        "publication_attempts", "UPDATE", ATTEMPT_TENANT, ATTEMPT_TENANT,
    ),
    "content_items_publication_update": (
        "content_items", "UPDATE", TENANT, PUBLICATION_CONTENT,
    ),
    "audit_facebook_publication_insert": (
        "audit_logs", "INSERT", None,
        f"""{TENANT}
        AND entity_type IN ('facebook_connection','content','publication')
        AND action IN (
            'facebook.oauth.started','facebook.oauth.consumed',
            'facebook.connection.created','facebook.connection.updated',
            'content.scheduled','content.schedule_cancelled','content.publish_requested',
            'publication.started','publication.succeeded',
            'publication.retryable_failure','publication.permanent_failure')
        AND ((actor_type = 'user' AND actor_id = ({USER})::text)
             OR (actor_type = 'system' AND actor_id IS NULL))""",
    ),
}

OAUTH_SELECT_COLUMNS = {
    "id", "organization_id", "actor_id", "state_digest", "nonce_digest",
    "requested_page_id", "expires_at", "consumed_at", "created_at",
}
CONNECTION_SELECT_COLUMNS = {
    "id", "organization_id", "platform", "external_account_id", "display_name",
    "capabilities", "credentials_ciphertext", "credential_key_version",
    "token_expires_at", "status", "last_verified_at", "last_error_code", "last_error_at",
    "created_at", "updated_at",
}
INSERT_COLUMNS = {
    "facebook_oauth_states": {
        "id", "organization_id", "actor_id", "state_digest", "nonce_digest",
        "requested_page_id", "expires_at",
    },
    "platform_connections": {
        "id", "organization_id", "platform", "external_account_id", "display_name",
        "capabilities", "credentials_ciphertext", "credential_key_version",
        "token_expires_at", "status", "last_verified_at", "last_error_code", "last_error_at",
    },
    "publication_jobs": {
        "id", "content_item_id", "content_version_id", "idempotency_key",
        "scheduled_for", "status", "attempt_count", "created_by", "next_attempt_at",
    },
    "publication_attempts": {
        "id", "publication_job_id", "attempt_no", "started_at",
        "request_fingerprint", "outcome",
    },
}
UPDATE_COLUMNS = {
    "facebook_oauth_states": {"consumed_at", "updated_at"},
    "platform_connections": {
        "display_name", "capabilities", "credentials_ciphertext", "credential_key_version",
        "token_expires_at", "status", "last_verified_at", "last_error_code",
        "last_error_at", "updated_at",
    },
    "publication_jobs": {
        "status", "attempt_count", "lease_token", "lease_expires_at",
        "published_external_id", "next_attempt_at", "updated_at",
    },
    "publication_attempts": {
        "finished_at", "provider_request_id", "provider_response", "outcome",
        "error_code", "error_message", "updated_at",
    },
    "content_items": {
        "status", "scheduled_for", "published_at", "external_post_id",
        "last_error_code", "updated_at",
    },
}


def historical_policies():
    return {**BASE.historical_policies(), **BASE.POLICIES}


def normalized(expression):
    parts = re.split(r"('(?:[^']|'')*')", expression or "")
    for index in range(0, len(parts), 2):
        value = parts[index].replace("public.", "")
        value = re.sub(
            r"::(?:text|campaign_status|content_status|ai_status|platform_type|publication_status)\b",
            "",
            value,
        )
        parts[index] = re.sub(r'[\s()"]', "", value).lower()
    return re.sub(r"=anyarray\[([^]]+)\]", r"in\1", "".join(parts))


def policy_catalog_snapshot(bind):
    rows = bind.execute(sa.text(
        "SELECT schemaname,tablename,policyname,permissive,roles,cmd,qual,with_check "
        "FROM pg_policies WHERE schemaname='public' ORDER BY policyname,tablename"
    )).all()
    return tuple(
        (schema, table, name, permissive, tuple(roles), command, using, check)
        for schema, table, name, permissive, roles, command, using, check in rows
    )


def _role_row(bind):
    return bind.execute(sa.text("""SELECT oid,rolcanlogin,rolinherit,rolsuper,rolcreatedb,
        rolcreaterole,rolreplication,
        (to_jsonb(pg_roles)->>('rol' || 'bypass' || 'rls'))::boolean AS bypass_flag
        FROM pg_roles WHERE rolname=:role"""), {"role": ROLE}).mappings().one_or_none()


def _verify_role(bind, expected_tables):
    role = _role_row(bind)
    if (role is None or not role["rolcanlogin"] or any(role[key] for key in (
            "rolinherit", "rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "bypass_flag"))):
        raise RuntimeError("Unsafe runtime role")
    if bind.scalar(sa.text(
            "SELECT EXISTS(SELECT 1 FROM pg_auth_members WHERE member=:oid)"), {"oid": role["oid"]}):
        raise RuntimeError("Runtime memberships forbidden")
    if bind.scalar(sa.text(
            "SELECT has_schema_privilege(:role,'public','CREATE')"), {"role": ROLE}):
        raise RuntimeError("Runtime schema CREATE forbidden")
    rows = bind.execute(sa.text("""SELECT c.relname,c.relowner,c.relrowsecurity,c.relforcerowsecurity
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind='r'
          AND c.relname=ANY(CAST(:tables AS text[]))"""), {"tables": list(expected_tables)}).all()
    if len(rows) != len(expected_tables) or any(
            owner == role["oid"] or not rls or not force for _, owner, rls, force in rows):
        raise RuntimeError("Expected nonowned ENABLE/FORCE application tables")
    return role


def _verify_active_index(bind, expected_retryable=False):
    row = bind.execute(sa.text("""SELECT i.indisunique,
        ARRAY(SELECT a.attname FROM unnest(i.indkey) WITH ORDINALITY k(attnum,ord)
              JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum
              ORDER BY k.ord),
        pg_get_expr(i.indpred,i.indrelid)
        FROM pg_index i WHERE i.indexrelid=to_regclass(
            'public.uq_publication_jobs_one_active_job_per_content_item')""")).one_or_none()
    expected = ["pending", "leased", "publishing"]
    if expected_retryable:
        expected.append("retryable_failure")
    if row is None or not row[0] or list(row[1]) != ["content_item_id"]:
        raise RuntimeError("Publication active-job index drift")
    predicate = re.sub(r"[\s()]", "", (row[2] or "").replace("public.", ""))
    rendered = "status=ANYARRAY[" + ",".join(
        f"'{value}'::publication_status" for value in expected
    ) + "]"
    if predicate != rendered:
        raise RuntimeError("Publication active-job predicate drift")


def verify_baseline(bind):
    if bind.scalar(sa.text("SELECT version_num FROM alembic_version")) != down_revision:
        raise RuntimeError("0008 requires exact 0007 baseline")
    _verify_role(bind, BASE.TABLES)
    snapshot = policy_catalog_snapshot(bind)
    if len(snapshot) != 30:
        raise RuntimeError("Expected exactly 30 historical policies before 0008")
    BASE.verify_grants(bind)
    _verify_active_index(bind)
    if bind.scalar(sa.text("SELECT to_regclass('public.facebook_oauth_states') IS NOT NULL")):
        raise RuntimeError("Unexpected facebook_oauth_states table before 0008")
    return snapshot


def verify_policies(bind, historical_snapshot=None):
    rows = policy_catalog_snapshot(bind)
    keys = {(table, name) for name, (table, _, _, _) in POLICIES.items()}
    prompt10 = tuple(row for row in rows if (row[1], row[2]) in keys)
    if len(rows) != 43 or len(prompt10) != 13 or {row[2] for row in prompt10} != set(POLICIES):
        raise RuntimeError("Expected exactly 43 policies after 0008")
    if historical_snapshot is not None:
        historical = tuple(row for row in rows if (row[1], row[2]) not in keys)
        if len(historical) != 30 or historical != historical_snapshot:
            raise RuntimeError("Historical policy catalog drift after 0008")
    for schema, table, name, permissive, roles, command, using, check in prompt10:
        expected_table, expected_command, expected_using, expected_check = POLICIES[name]
        if (schema != "public" or table != expected_table or command != expected_command
                or roles != (ROLE,) or permissive != "PERMISSIVE"
                or normalized(using) != normalized(expected_using)
                or normalized(check) != normalized(expected_check)):
            raise RuntimeError(f"Prompt 10 policy drift: {name}")


def _historical_grants():
    inserts = {}
    updates = {}
    for source in (
        BASE.BASE.BASE.INSERT_COLUMNS,
        BASE.BASE.INSERT_COLUMNS,
        BASE.INSERT_COLUMNS,
    ):
        for table, columns in source.items():
            inserts.setdefault(table, set()).update(columns)
    for source in (
        BASE.BASE.BASE.BASE.UPDATE_COLUMNS,
        BASE.BASE.BASE.UPDATE_COLUMNS,
        BASE.BASE.UPDATE_COLUMNS,
        BASE.UPDATE_COLUMNS,
    ):
        for table, columns in source.items():
            updates.setdefault(table, set()).update(columns)
    return inserts, updates


def verify_grants(bind):
    historical_inserts, historical_updates = _historical_grants()
    inserts = {table: set(columns) for table, columns in historical_inserts.items()}
    updates = {table: set(columns) for table, columns in historical_updates.items()}
    for table, columns in INSERT_COLUMNS.items():
        inserts.setdefault(table, set()).update(columns)
    for table, columns in UPDATE_COLUMNS.items():
        updates.setdefault(table, set()).update(columns)
    full_select = set(BASE.BASE.BASE.BASE.BOOTSTRAP) | {
        "campaigns", "idempotency_keys", "content_items", "content_versions",
        "assets", "content_assets", "ai_generation_requests", "ai_daily_usage",
        "publication_jobs", "publication_attempts",
    }
    full_insert = {"campaigns", "idempotency_keys", "audit_logs"}
    historical_connection_columns = set(BASE.BASE.BASE.CONNECTION_COLUMNS)
    for table in TABLES:
        full = set()
        if table in full_select:
            full.add("SELECT")
        if table in full_insert:
            full.add("INSERT")
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            actual = bind.scalar(sa.text(
                "SELECT has_table_privilege(:role,:table,:privilege)"
            ), {"role": ROLE, "table": "public." + table, "privilege": privilege})
            if bool(actual) != (privilege in full):
                raise RuntimeError(f"Unexpected table privilege after 0008: {table}.{privilege}")
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
                             and column in historical_connection_columns | CONNECTION_SELECT_COLUMNS)
                expected |= (privilege == "SELECT" and table == "facebook_oauth_states"
                             and column in OAUTH_SELECT_COLUMNS)
                actual = bind.scalar(sa.text(
                    "SELECT has_column_privilege(:role,:table,:column,:privilege)"
                ), {"role": ROLE, "table": "public." + table,
                    "column": column, "privilege": privilege})
                if bool(actual) != bool(expected):
                    raise RuntimeError(
                        f"Unexpected column privilege after 0008: {table}.{column}.{privilege}"
                    )
    _verify_role(bind, TABLES)
    if bind.scalar(sa.text("""SELECT EXISTS(SELECT 1 FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace CROSS JOIN LATERAL aclexplode(c.relacl) a
        WHERE n.nspname='public' AND c.relname=ANY(CAST(:tables AS text[])) AND a.grantee=0)
        OR EXISTS(SELECT 1 FROM pg_attribute at JOIN pg_class c ON c.oid=at.attrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace CROSS JOIN LATERAL aclexplode(at.attacl) a
        WHERE n.nspname='public' AND c.relname=ANY(CAST(:tables AS text[])) AND a.grantee=0)"""),
        {"tables": list(TABLES)}):
        raise RuntimeError("PUBLIC application grants forbidden")
    for principal in ("anon", "authenticated", "service_role"):
        if bind.scalar(sa.text(
                "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:role)"), {"role": principal}):
            for table in TABLES:
                if bind.scalar(sa.text("""SELECT has_any_column_privilege(:role,:table,'SELECT')
                    OR has_any_column_privilege(:role,:table,'INSERT')
                    OR has_any_column_privilege(:role,:table,'UPDATE')
                    OR has_table_privilege(:role,:table,'DELETE')"""),
                    {"role": principal, "table": "public." + table}):
                    raise RuntimeError("Supabase principal application access forbidden")
    if bind.scalar(sa.text(
            "SELECT EXISTS(SELECT 1 FROM pg_proc WHERE pronamespace='public'::regnamespace AND prosecdef)")):
        raise RuntimeError("Unexpected privileged public function")
    for privilege in ("USAGE", "SELECT", "UPDATE"):
        if bind.scalar(sa.text(
                "SELECT has_sequence_privilege(:role,'public.audit_logs_id_seq',:privilege)"),
                {"role": ROLE, "privilege": privilege}):
            raise RuntimeError("Unexpected audit sequence privilege")


def _create_oauth_table():
    op.create_table(
        "facebook_oauth_states",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("state_digest", sa.CHAR(64), nullable=False),
        sa.Column("nonce_digest", sa.CHAR(64), nullable=False),
        sa.Column("requested_page_id", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["public.organizations.id"],
            onupdate="RESTRICT", ondelete="RESTRICT",
            name="fk_facebook_oauth_states_organization",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"], ["public.profiles.id"],
            onupdate="RESTRICT", ondelete="RESTRICT",
            name="fk_facebook_oauth_states_actor",
        ),
        sa.UniqueConstraint("state_digest", name="uq_facebook_oauth_states_state_digest"),
        sa.CheckConstraint(
            "state_digest ~ '^[0-9a-f]{64}$'",
            name="facebook_oauth_states_valid_state_digest",
        ),
        sa.CheckConstraint(
            "nonce_digest ~ '^[0-9a-f]{64}$'",
            name="facebook_oauth_states_valid_nonce_digest",
        ),
        sa.CheckConstraint(
            "btrim(requested_page_id) <> ''",
            name="facebook_oauth_states_page_not_blank",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="facebook_oauth_states_valid_expiry",
        ),
        sa.CheckConstraint(
            "consumed_at IS NULL OR consumed_at >= created_at",
            name="facebook_oauth_states_valid_consumption",
        ),
        schema="public",
    )
    op.execute("ALTER TABLE public.facebook_oauth_states ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.facebook_oauth_states FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE TRIGGER facebook_oauth_states_set_updated_at
        BEFORE UPDATE ON public.facebook_oauth_states FOR EACH ROW
        EXECUTE FUNCTION public.set_updated_at()""")
    op.execute("""CREATE FUNCTION public.enforce_facebook_oauth_state_lifecycle()
        RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        IF NEW.id IS DISTINCT FROM OLD.id
           OR NEW.organization_id IS DISTINCT FROM OLD.organization_id
           OR NEW.actor_id IS DISTINCT FROM OLD.actor_id
           OR NEW.state_digest IS DISTINCT FROM OLD.state_digest
           OR NEW.nonce_digest IS DISTINCT FROM OLD.nonce_digest
           OR NEW.requested_page_id IS DISTINCT FROM OLD.requested_page_id
           OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
           OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'OAuth state binding is immutable' USING ERRCODE='23514';
        END IF;
        IF OLD.consumed_at IS NOT NULL
           AND NEW.consumed_at IS DISTINCT FROM OLD.consumed_at THEN
            RAISE EXCEPTION 'OAuth state may be consumed exactly once' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
        END; $$""")
    op.execute("""CREATE TRIGGER facebook_oauth_state_lifecycle_guard
        BEFORE UPDATE ON public.facebook_oauth_states FOR EACH ROW
        EXECUTE FUNCTION public.enforce_facebook_oauth_state_lifecycle()""")


def _create_attempt_guards():
    op.drop_constraint(
        "publication_attempts_outcome_check", "publication_attempts",
        schema="public", type_="check",
    )
    op.create_check_constraint(
        "publication_attempts_valid_outcome", "publication_attempts",
        "outcome IN ('in_progress','succeeded','retryable_failure','permanent_failure')",
        schema="public",
    )
    op.create_check_constraint(
        "publication_attempts_outcome_finish_shape", "publication_attempts",
        "(outcome = 'in_progress' AND finished_at IS NULL) OR "
        "(outcome IN ('succeeded','retryable_failure','permanent_failure') AND finished_at IS NOT NULL)",
        schema="public",
    )
    op.execute("""CREATE FUNCTION public.enforce_publication_attempt_lifecycle()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE unexpected_key text;
        BEGIN
        IF TG_OP = 'UPDATE' THEN
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.publication_job_id IS DISTINCT FROM OLD.publication_job_id
               OR NEW.attempt_no IS DISTINCT FROM OLD.attempt_no
               OR NEW.request_fingerprint IS DISTINCT FROM OLD.request_fingerprint
               OR NEW.started_at IS DISTINCT FROM OLD.started_at
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'Publication attempt identity is immutable' USING ERRCODE='23514';
            END IF;
            IF OLD.outcome <> 'in_progress'
               OR NEW.outcome NOT IN ('succeeded','retryable_failure','permanent_failure') THEN
                RAISE EXCEPTION 'Invalid publication attempt transition' USING ERRCODE='23514';
            END IF;
        END IF;
        IF NEW.provider_response IS NOT NULL THEN
            IF jsonb_typeof(NEW.provider_response) <> 'object'
               OR pg_column_size(NEW.provider_response) > 4096 THEN
                RAISE EXCEPTION 'Invalid provider response shape' USING ERRCODE='23514';
            END IF;
            SELECT key INTO unexpected_key
            FROM jsonb_object_keys(NEW.provider_response) AS keys(key)
            WHERE key <> ALL(ARRAY[
                'status_category','http_status','provider_request_id',
                'normalized_error_code','reconciliation_result','retry_after_seconds'])
            LIMIT 1;
            IF unexpected_key IS NOT NULL THEN
                RAISE EXCEPTION 'Unapproved provider response key' USING ERRCODE='23514';
            END IF;
        END IF;
        RETURN NEW;
        END; $$""")
    op.execute("""CREATE TRIGGER publication_attempt_lifecycle_guard
        BEFORE INSERT OR UPDATE ON public.publication_attempts FOR EACH ROW
        EXECUTE FUNCTION public.enforce_publication_attempt_lifecycle()""")


def _replace_active_index_and_create_job_guard():
    op.drop_index(
        "uq_publication_jobs_one_active_job_per_content_item",
        table_name="publication_jobs", schema="public",
    )
    op.create_index(
        "uq_publication_jobs_one_active_job_per_content_item",
        "publication_jobs", ["content_item_id"], unique=True, schema="public",
        postgresql_where=sa.text(
            "status IN ('pending','leased','publishing','retryable_failure')"
        ),
    )
    op.execute("""CREATE FUNCTION public.enforce_publication_job_lifecycle()
        RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        IF TG_OP = 'UPDATE' THEN
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.content_item_id IS DISTINCT FROM OLD.content_item_id
               OR NEW.content_version_id IS DISTINCT FROM OLD.content_version_id
               OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
               OR NEW.scheduled_for IS DISTINCT FROM OLD.scheduled_for
               OR NEW.created_by IS DISTINCT FROM OLD.created_by
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'Publication job identity is immutable' USING ERRCODE='23514';
            END IF;
            IF NOT (
                (OLD.status = 'pending' AND NEW.status IN ('leased','cancelled')) OR
                (OLD.status = 'retryable_failure' AND NEW.status IN ('leased','cancelled')) OR
                (OLD.status = 'leased' AND NEW.status IN ('publishing','retryable_failure')) OR
                (OLD.status = 'publishing' AND NEW.status IN
                    ('succeeded','retryable_failure','permanent_failure'))
            ) THEN
                RAISE EXCEPTION 'Invalid publication job transition' USING ERRCODE='23514';
            END IF;
            IF NEW.attempt_count < OLD.attempt_count THEN
                RAISE EXCEPTION 'Publication attempt count is monotonic' USING ERRCODE='23514';
            END IF;
        END IF;
        IF NEW.status IN ('leased','publishing') AND
           (NEW.lease_token IS NULL OR NEW.lease_expires_at IS NULL
            OR NEW.published_external_id IS NOT NULL) THEN
            RAISE EXCEPTION 'Invalid leased publication job shape' USING ERRCODE='23514';
        ELSIF NEW.status = 'retryable_failure' AND
              (NEW.lease_token IS NOT NULL OR NEW.lease_expires_at IS NOT NULL
               OR NEW.next_attempt_at IS NULL OR NEW.published_external_id IS NOT NULL) THEN
            RAISE EXCEPTION 'Invalid retryable publication job shape' USING ERRCODE='23514';
        ELSIF NEW.status = 'succeeded' AND
              (NEW.lease_token IS NOT NULL OR NEW.lease_expires_at IS NOT NULL
               OR NEW.published_external_id IS NULL) THEN
            RAISE EXCEPTION 'Invalid succeeded publication job shape' USING ERRCODE='23514';
        ELSIF NEW.status = 'permanent_failure' AND
              (NEW.lease_token IS NOT NULL OR NEW.lease_expires_at IS NOT NULL) THEN
            RAISE EXCEPTION 'Invalid permanent publication job shape' USING ERRCODE='23514';
        ELSIF NEW.status IN ('pending','cancelled') AND
              (NEW.lease_token IS NOT NULL OR NEW.lease_expires_at IS NOT NULL
               OR NEW.published_external_id IS NOT NULL) THEN
            RAISE EXCEPTION 'Invalid terminal or pending publication job shape' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
        END; $$""")
    op.execute("""CREATE TRIGGER publication_job_lifecycle_guard
        BEFORE INSERT OR UPDATE ON public.publication_jobs FOR EACH ROW
        EXECUTE FUNCTION public.enforce_publication_job_lifecycle()""")


def _create_connection_guard():
    op.execute("""CREATE FUNCTION public.enforce_facebook_connection_invariants()
        RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        IF TG_OP = 'UPDATE' AND (
            NEW.id IS DISTINCT FROM OLD.id
            OR NEW.organization_id IS DISTINCT FROM OLD.organization_id
            OR NEW.platform IS DISTINCT FROM OLD.platform
            OR NEW.external_account_id IS DISTINCT FROM OLD.external_account_id
            OR NEW.created_at IS DISTINCT FROM OLD.created_at) THEN
            RAISE EXCEPTION 'Connection identity is immutable' USING ERRCODE='23514';
        END IF;
        IF NEW.platform = 'facebook' AND (
            btrim(NEW.external_account_id) = '' OR btrim(NEW.display_name) = ''
            OR NEW.credential_key_version <= 0 OR octet_length(NEW.credentials_ciphertext) = 0
            OR (NEW.status = 'active' AND NEW.last_verified_at IS NULL)) THEN
            RAISE EXCEPTION 'Invalid Facebook connection shape' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
        END; $$""")
    op.execute("""CREATE TRIGGER facebook_connection_invariants_guard
        BEFORE INSERT OR UPDATE ON public.platform_connections FOR EACH ROW
        EXECUTE FUNCTION public.enforce_facebook_connection_invariants()""")


def _create_content_publication_guard():
    op.execute("""CREATE FUNCTION public.enforce_content_publication_lifecycle()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE publication_change boolean;
        BEGIN
        publication_change :=
            OLD.status IN ('scheduled','publishing','published','failed')
            OR NEW.status IN ('scheduled','publishing','published','failed')
            OR NEW.scheduled_for IS DISTINCT FROM OLD.scheduled_for
            OR NEW.published_at IS DISTINCT FROM OLD.published_at
            OR NEW.external_post_id IS DISTINCT FROM OLD.external_post_id
            OR NEW.last_error_code IS DISTINCT FROM OLD.last_error_code;
        IF NOT publication_change THEN
            RETURN NEW;
        END IF;
        IF NOT (
            (OLD.status = 'approved' AND NEW.status = 'scheduled') OR
            (OLD.status = 'scheduled' AND NEW.status = 'approved') OR
            (OLD.status = 'scheduled' AND NEW.status = 'publishing') OR
            (OLD.status = 'publishing' AND NEW.status IN ('published','failed'))
        ) THEN
            RAISE EXCEPTION 'Invalid content publication transition' USING ERRCODE='23514';
        END IF;
        IF NEW.current_version_no IS DISTINCT FROM OLD.current_version_no
           OR NEW.approved_version_no IS DISTINCT FROM OLD.approved_version_no THEN
            RAISE EXCEPTION 'Publication transition cannot change version pointers' USING ERRCODE='23514';
        END IF;
        IF NEW.status = 'scheduled' AND (
            NEW.platform <> 'facebook' OR NEW.connection_id IS NULL
            OR NEW.approved_version_no IS NULL
            OR NEW.approved_version_no <> NEW.current_version_no
            OR NEW.scheduled_for IS NULL OR NEW.published_at IS NOT NULL
            OR NEW.external_post_id IS NOT NULL) THEN
            RAISE EXCEPTION 'Invalid scheduled content shape' USING ERRCODE='23514';
        ELSIF OLD.status = 'scheduled' AND NEW.status = 'approved' AND (
            NEW.scheduled_for IS NOT NULL OR NEW.published_at IS NOT NULL
            OR NEW.external_post_id IS NOT NULL) THEN
            RAISE EXCEPTION 'Invalid schedule cancellation shape' USING ERRCODE='23514';
        ELSIF NEW.status = 'publishing' AND (
            NEW.platform <> 'facebook' OR NEW.connection_id IS NULL
            OR NEW.approved_version_no IS NULL
            OR NEW.approved_version_no <> NEW.current_version_no
            OR NEW.scheduled_for IS NULL OR NEW.published_at IS NOT NULL
            OR NEW.external_post_id IS NOT NULL) THEN
            RAISE EXCEPTION 'Invalid publishing content shape' USING ERRCODE='23514';
        ELSIF NEW.status = 'published' AND (
            NEW.platform <> 'facebook' OR NEW.published_at IS NULL
            OR NEW.external_post_id IS NULL
            OR NEW.approved_version_no IS NULL
            OR NEW.approved_version_no <> NEW.current_version_no) THEN
            RAISE EXCEPTION 'Invalid published content shape' USING ERRCODE='23514';
        ELSIF NEW.status = 'failed' AND (
            NEW.platform <> 'facebook' OR NEW.last_error_code IS NULL
            OR NEW.external_post_id IS NOT NULL OR NEW.published_at IS NOT NULL) THEN
            RAISE EXCEPTION 'Invalid failed content shape' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
        END; $$""")
    op.execute("""CREATE TRIGGER content_publication_lifecycle_guard
        BEFORE UPDATE OF status,scheduled_for,published_at,external_post_id,last_error_code,
                         current_version_no,approved_version_no
        ON public.content_items FOR EACH ROW
        EXECUTE FUNCTION public.enforce_content_publication_lifecycle()""")


def _create_policies_and_grants():
    for name, (table, command, using, check) in POLICIES.items():
        ddl = f"CREATE POLICY {name} ON public.{table} FOR {command} TO {ROLE}"
        if using is not None:
            ddl += f" USING ({using})"
        if check is not None:
            ddl += f" WITH CHECK ({check})"
        op.execute(ddl)
    op.execute("GRANT SELECT ON public.publication_jobs, public.publication_attempts TO nightclub_api")
    op.execute(
        "GRANT SELECT (" + ", ".join(sorted(OAUTH_SELECT_COLUMNS))
        + ") ON public.facebook_oauth_states TO " + ROLE
    )
    op.execute(
        "GRANT SELECT (" + ", ".join(sorted(CONNECTION_SELECT_COLUMNS))
        + ") ON public.platform_connections TO " + ROLE
    )
    for table, columns in INSERT_COLUMNS.items():
        op.execute(f"GRANT INSERT ({', '.join(sorted(columns))}) ON public.{table} TO {ROLE}")
    for table, columns in UPDATE_COLUMNS.items():
        op.execute(f"GRANT UPDATE ({', '.join(sorted(columns))}) ON public.{table} TO {ROLE}")


def upgrade():
    if context.is_offline_mode():
        raise RuntimeError("0008 requires online baseline verification")
    bind = op.get_bind()
    historical_snapshot = verify_baseline(bind)
    _create_oauth_table()
    _create_attempt_guards()
    _replace_active_index_and_create_job_guard()
    _create_connection_guard()
    _create_content_publication_guard()
    _create_policies_and_grants()
    _verify_active_index(bind, expected_retryable=True)
    verify_policies(bind, historical_snapshot)
    verify_grants(bind)


def downgrade():
    raise RuntimeError(
        "Unsafe security downgrade blocked; a reviewed compensating migration is required"
    )
