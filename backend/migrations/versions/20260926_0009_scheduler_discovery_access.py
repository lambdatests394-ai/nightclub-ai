"""Read-only cross-tenant discovery surface for the automation scheduler.

The scheduler login is provisioned operationally. This revision verifies its
posture, grants only due-job metadata reads, and preserves the frozen 0008
catalog. Reversal requires a reviewed compensating migration.
"""

import hashlib
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from alembic import context, op
import sqlalchemy as sa


revision = "20260926_0009"
down_revision = "20260924_0008"
branch_labels = None
depends_on = None

FROZEN_0008_SHA256 = "6a0962655398513d75cd8fd6bb4a562789d59e492f54f14e191169209e62f5dd"
PREVIOUS_PATH = Path(__file__).with_name("20260924_0008_facebook_publication_access.py")


def verify_frozen_previous() -> None:
    if hashlib.sha256(PREVIOUS_PATH.read_bytes()).hexdigest() != FROZEN_0008_SHA256:
        raise RuntimeError("Frozen 0008 migration hash mismatch")


def previous_snapshot():
    verify_frozen_previous()
    spec = spec_from_file_location("prompt11_frozen_0008", PREVIOUS_PATH)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BASE = previous_snapshot()
RUNTIME_ROLE = BASE.ROLE
SCHEDULER_ROLE = "nightclub_scheduler"
TABLES = BASE.TABLES

SCHEDULER_SELECT_COLUMNS = {
    "publication_jobs": (
        "id", "content_item_id", "status", "scheduled_for",
        "next_attempt_at", "lease_expires_at",
    ),
    "content_items": ("id", "organization_id"),
}

JOB_DUE = """(
    status = 'pending'
    AND scheduled_for <= statement_timestamp()
) OR (
    status = 'retryable_failure'
    AND next_attempt_at IS NOT NULL
    AND next_attempt_at <= statement_timestamp()
) OR (
    status IN ('leased', 'publishing')
    AND lease_expires_at IS NOT NULL
    AND lease_expires_at <= statement_timestamp()
)"""

CONTENT_DUE = """EXISTS (
    SELECT 1 FROM public.publication_jobs pj
    WHERE pj.content_item_id = content_items.id
      AND ((
          pj.status = 'pending'
          AND pj.scheduled_for <= statement_timestamp()
      ) OR (
          pj.status = 'retryable_failure'
          AND pj.next_attempt_at IS NOT NULL
          AND pj.next_attempt_at <= statement_timestamp()
      ) OR (
          pj.status IN ('leased', 'publishing')
          AND pj.lease_expires_at IS NOT NULL
          AND pj.lease_expires_at <= statement_timestamp()
      ))
)"""

POLICIES = {
    "publication_jobs_scheduler_due_select": (
        "publication_jobs", "SELECT", JOB_DUE, None,
    ),
    "content_items_scheduler_due_select": (
        "content_items", "SELECT", CONTENT_DUE, None,
    ),
}

TABLE_PRIVILEGES = (
    "SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER",
)
COLUMN_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "REFERENCES")
SEQUENCE_PRIVILEGES = ("USAGE", "SELECT", "UPDATE")


def expected_historical_policy_identities():
    policies = {**BASE.historical_policies(), **BASE.POLICIES}
    return frozenset((table, name) for name, (table, _, _, _) in policies.items())


def verify_historical_catalog(bind):
    rows = BASE.policy_catalog_snapshot(bind)
    expected = expected_historical_policy_identities()
    actual = frozenset((table, name) for _, table, name, *_ in rows)
    if (len(rows) != 43 or len(expected) != 43 or actual != expected
            or any(schema != "public" for schema, *_ in rows)):
        raise RuntimeError("Expected exact 43-policy 0008 baseline")
    return rows


def _scheduler_role(bind):
    role = bind.execute(sa.text("""SELECT oid,rolname,rolcanlogin,rolinherit,rolsuper,
        rolcreatedb,rolcreaterole,rolreplication,rolbypassrls
        FROM pg_roles WHERE rolname=:role"""), {"role": SCHEDULER_ROLE}).mappings().one_or_none()
    if (role is None or role["rolname"] != SCHEDULER_ROLE or not role["rolcanlogin"]
            or any(role[key] for key in (
                "rolinherit", "rolsuper", "rolcreatedb", "rolcreaterole",
                "rolreplication", "rolbypassrls",
            ))):
        raise RuntimeError("Unsafe or absent scheduler role")
    if bind.scalar(sa.text(
            "SELECT EXISTS(SELECT 1 FROM pg_auth_members WHERE member=:oid)"),
            {"oid": role["oid"]}):
        raise RuntimeError("Scheduler role memberships forbidden")
    if bind.scalar(sa.text("""SELECT EXISTS(
        SELECT 1 FROM pg_namespace n
        WHERE n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
          AND n.nspname <> 'information_schema'
          AND has_schema_privilege(:role,n.oid,'CREATE'))"""), {"role": SCHEDULER_ROLE}):
        raise RuntimeError("Scheduler schema CREATE forbidden")
    return role


def verify_table_posture(bind, scheduler_oid):
    runtime = BASE._role_row(bind)
    rows = bind.execute(sa.text("""SELECT c.relname,c.relowner,c.relrowsecurity,c.relforcerowsecurity
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind='r'
          AND c.relname=ANY(CAST(:tables AS text[]))"""), {"tables": list(TABLES)}).all()
    if (runtime is None or len(rows) != len(TABLES)
            or any(owner in {runtime["oid"], scheduler_oid} or not rls or not force
                   for _, owner, rls, force in rows)):
        raise RuntimeError("Expected nonowned ENABLE/FORCE application tables")


def _columns(bind, table):
    return bind.execute(sa.text("""SELECT attname FROM pg_attribute
        WHERE attrelid=to_regclass(:table) AND attnum>0 AND NOT attisdropped
        ORDER BY attnum"""), {"table": "public." + table}).scalars().all()


def verify_scheduler_has_no_application_access(bind):
    for table in TABLES:
        for privilege in TABLE_PRIVILEGES:
            if bind.scalar(sa.text(
                    "SELECT has_table_privilege(:role,:table,:privilege)"), {
                        "role": SCHEDULER_ROLE,
                        "table": "public." + table,
                        "privilege": privilege,
                    }):
                raise RuntimeError("Unexpected preexisting scheduler table privilege")
        for privilege in COLUMN_PRIVILEGES:
            if bind.scalar(sa.text(
                    "SELECT has_any_column_privilege(:role,:table,:privilege)"), {
                        "role": SCHEDULER_ROLE,
                        "table": "public." + table,
                        "privilege": privilege,
                    }):
                raise RuntimeError("Unexpected preexisting scheduler column privilege")


def function_execute_snapshot(bind):
    rows = bind.execute(sa.text("""SELECT p.oid::text,
        has_function_privilege(:role,p.oid,'EXECUTE')
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='public' ORDER BY p.oid"""), {"role": SCHEDULER_ROLE}).all()
    return tuple(rows)


def runtime_acl_snapshot(bind):
    return tuple(bind.execute(sa.text("""WITH target AS (
        SELECT oid FROM pg_roles WHERE rolname=:role
    )
    SELECT 'table',c.relname,'',a.privilege_type,a.is_grantable
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    CROSS JOIN LATERAL aclexplode(
        COALESCE(c.relacl,acldefault('r',c.relowner))
    ) a
    WHERE n.nspname='public' AND c.relname=ANY(CAST(:tables AS text[]))
      AND a.grantee=(SELECT oid FROM target)
    UNION ALL
    SELECT 'column',c.relname,at.attname,a.privilege_type,a.is_grantable
    FROM pg_attribute at JOIN pg_class c ON c.oid=at.attrelid
    JOIN pg_namespace n ON n.oid=c.relnamespace
    CROSS JOIN LATERAL aclexplode(
        COALESCE(at.attacl,acldefault('c',c.relowner))
    ) a
    WHERE n.nspname='public' AND c.relname=ANY(CAST(:tables AS text[]))
      AND at.attnum>0 AND NOT at.attisdropped AND a.grantee=(SELECT oid FROM target)
    ORDER BY 1,2,3,4,5"""), {
        "role": RUNTIME_ROLE, "tables": list(TABLES),
    }).all())


def verify_scheduler_grants(bind, *, function_snapshot):
    if not bind.scalar(sa.text(
            "SELECT has_schema_privilege(:role,'public','USAGE')"), {"role": SCHEDULER_ROLE}):
        raise RuntimeError("Scheduler schema USAGE missing")
    if bind.scalar(sa.text(
            "SELECT has_schema_privilege(:role,'public','CREATE')"), {"role": SCHEDULER_ROLE}):
        raise RuntimeError("Scheduler schema CREATE forbidden")
    for table in TABLES:
        for privilege in TABLE_PRIVILEGES:
            if bind.scalar(sa.text(
                    "SELECT has_table_privilege(:role,:table,:privilege)"), {
                        "role": SCHEDULER_ROLE, "table": "public." + table,
                        "privilege": privilege,
                    }):
                raise RuntimeError("Scheduler table-level privileges forbidden")
        allowed = set(SCHEDULER_SELECT_COLUMNS.get(table, ()))
        for column in _columns(bind, table):
            for privilege in COLUMN_PRIVILEGES:
                actual = bool(bind.scalar(sa.text(
                    "SELECT has_column_privilege(:role,:table,:column,:privilege)"), {
                        "role": SCHEDULER_ROLE, "table": "public." + table,
                        "column": column, "privilege": privilege,
                    }))
                expected = privilege == "SELECT" and column in allowed
                if actual != expected:
                    raise RuntimeError("Unexpected scheduler column privilege")
    sequences = bind.execute(sa.text("""SELECT c.oid::regclass::text
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind='S' ORDER BY c.oid""")).scalars().all()
    for sequence in sequences:
        for privilege in SEQUENCE_PRIVILEGES:
            if bind.scalar(sa.text(
                    "SELECT has_sequence_privilege(:role,:sequence,:privilege)"), {
                        "role": SCHEDULER_ROLE, "sequence": sequence,
                        "privilege": privilege,
                    }):
                raise RuntimeError("Scheduler sequence privileges forbidden")
    if function_execute_snapshot(bind) != function_snapshot:
        raise RuntimeError("Scheduler function privileges changed")
    if bind.scalar(sa.text("""SELECT EXISTS(SELECT 1 FROM pg_proc
        WHERE pronamespace='public'::regnamespace AND prosecdef)""")):
        raise RuntimeError("Unexpected privileged public function")


def verify_policies(bind, historical_snapshot):
    rows = BASE.policy_catalog_snapshot(bind)
    new_keys = {(table, name) for name, (table, _, _, _) in POLICIES.items()}
    scheduler_rows = tuple(row for row in rows if (row[1], row[2]) in new_keys)
    historical = tuple(row for row in rows if (row[1], row[2]) not in new_keys)
    if (len(rows) != 45 or len(scheduler_rows) != 2
            or {(row[1], row[2]) for row in scheduler_rows} != new_keys
            or historical != historical_snapshot):
        raise RuntimeError("Policy drift after 0009")
    for schema, table, name, permissive, roles, command, using, check in scheduler_rows:
        expected_table, expected_command, expected_using, expected_check = POLICIES[name]
        if (schema != "public" or table != expected_table or command != expected_command
                or roles != (SCHEDULER_ROLE,) or permissive != "PERMISSIVE"
                or BASE.normalized(using) != BASE.normalized(expected_using)
                or BASE.normalized(check) != BASE.normalized(expected_check)):
            raise RuntimeError(f"Scheduler policy drift: {name}")


def verify_baseline(bind):
    verify_frozen_previous()
    if bind.scalar(sa.text("SELECT version_num FROM alembic_version")) != down_revision:
        raise RuntimeError("0009 requires exact 0008 baseline")
    BASE.verify_policies(bind)
    BASE._verify_active_index(bind, expected_retryable=True)
    BASE.verify_grants(bind)
    scheduler = _scheduler_role(bind)
    verify_table_posture(bind, scheduler["oid"])
    verify_scheduler_has_no_application_access(bind)
    runtime_acl = runtime_acl_snapshot(bind)
    function_snapshot = function_execute_snapshot(bind)
    historical_snapshot = verify_historical_catalog(bind)
    return (
        historical_snapshot,
        runtime_acl,
        function_snapshot,
        scheduler,
    )


def _apply_scheduler_access():
    op.execute("GRANT USAGE ON SCHEMA public TO nightclub_scheduler")
    for table, columns in SCHEDULER_SELECT_COLUMNS.items():
        op.execute(
            f"GRANT SELECT ({', '.join(columns)}) ON public.{table} TO {SCHEDULER_ROLE}"
        )
    for name, (table, command, using, check) in POLICIES.items():
        if check is not None:
            raise RuntimeError("Scheduler SELECT policies must not contain WITH CHECK")
        op.execute(
            f"CREATE POLICY {name} ON public.{table} FOR {command} "
            f"TO {SCHEDULER_ROLE} USING ({using})"
        )


def upgrade():
    if context.is_offline_mode():
        raise RuntimeError("0009 requires online baseline verification")
    bind = op.get_bind()
    historical, runtime_acl, function_snapshot, scheduler = verify_baseline(bind)
    _apply_scheduler_access()
    verify_policies(bind, historical)
    BASE.verify_grants(bind)
    if runtime_acl_snapshot(bind) != runtime_acl:
        raise RuntimeError("Runtime ACL drift after 0009")
    verify_scheduler_grants(bind, function_snapshot=function_snapshot)
    verify_table_posture(bind, scheduler["oid"])


def downgrade():
    raise RuntimeError(
        "Unsafe security downgrade blocked; a reviewed compensating migration is required"
    )
