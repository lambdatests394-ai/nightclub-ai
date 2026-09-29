"""Tenant-local system automation authority for durable publication execution."""

import hashlib
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from alembic import context, op
import sqlalchemy as sa


revision = "20260928_0010"
down_revision = "20260926_0009"
branch_labels = None
depends_on = None

FROZEN_0009_SHA256 = "268160d5a38d0c7d4d8acab36af06d67e0973fa8efa691de6a3dd69c3e159450"
PREVIOUS_PATH = Path(__file__).with_name("20260926_0009_scheduler_discovery_access.py")


def verify_frozen_previous() -> None:
    if hashlib.sha256(PREVIOUS_PATH.read_bytes()).hexdigest() != FROZEN_0009_SHA256:
        raise RuntimeError("Frozen 0009 migration hash mismatch")


def previous_snapshot():
    verify_frozen_previous()
    spec = spec_from_file_location("prompt11_frozen_0009", PREVIOUS_PATH)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BASE = previous_snapshot()
RUNTIME_ROLE = BASE.RUNTIME_ROLE
SCHEDULER_ROLE = BASE.SCHEDULER_ROLE
TABLES = BASE.TABLES

SYSTEM_CONTEXT = """(
    current_setting('app.execution_context', true) = 'system_automation'
    AND NULLIF(current_setting('app.user_id', true), '') IS NULL
)"""
ORG = "NULLIF(current_setting('app.organization_id', true), '')::uuid"
SYSTEM_TENANT = f"{SYSTEM_CONTEXT} AND organization_id = {ORG}"
ORGANIZATION_TENANT = f"{SYSTEM_CONTEXT} AND id = {ORG}"
JOB_TENANT = f"""{SYSTEM_CONTEXT} AND EXISTS (
    SELECT 1 FROM public.content_items ci
    WHERE ci.id = publication_jobs.content_item_id
      AND ci.organization_id = {ORG}
)"""
ATTEMPT_TENANT = f"""{SYSTEM_CONTEXT} AND EXISTS (
    SELECT 1 FROM public.publication_jobs pj
    JOIN public.content_items ci ON ci.id = pj.content_item_id
    WHERE pj.id = publication_attempts.publication_job_id
      AND ci.organization_id = {ORG}
)"""
VERSION_TENANT = f"""{SYSTEM_CONTEXT} AND EXISTS (
    SELECT 1 FROM public.content_items ci
    WHERE ci.id = content_versions.content_item_id
      AND ci.organization_id = {ORG}
)"""
CONTENT_ASSET_TENANT = f"""{SYSTEM_CONTEXT} AND EXISTS (
    SELECT 1 FROM public.content_items ci
    WHERE ci.id = content_assets.content_item_id
      AND ci.organization_id = {ORG}
)"""
CONTENT_EXECUTION = f"""{SYSTEM_TENANT}
    AND platform = 'facebook'
    AND status IN ('scheduled', 'publishing', 'published', 'failed')"""
CONNECTION_EXECUTION = f"{SYSTEM_TENANT} AND platform = 'facebook'"
ATTEMPT_CREATION = f"{ATTEMPT_TENANT} AND outcome = 'in_progress' AND finished_at IS NULL"
AUDIT_EXECUTION = f"""{SYSTEM_TENANT}
    AND actor_type = 'system' AND actor_id IS NULL
    AND entity_type = 'publication'
    AND action IN (
        'publication.started',
        'publication.succeeded',
        'publication.retryable_failure',
        'publication.permanent_failure'
    )"""

POLICIES = {
    "organizations_system_automation_select": (
        "organizations", "SELECT", ORGANIZATION_TENANT, None,
    ),
    "publication_jobs_system_automation_select": (
        "publication_jobs", "SELECT", JOB_TENANT, None,
    ),
    "publication_jobs_system_automation_update": (
        "publication_jobs", "UPDATE", JOB_TENANT, JOB_TENANT,
    ),
    "publication_attempts_system_automation_select": (
        "publication_attempts", "SELECT", ATTEMPT_TENANT, None,
    ),
    "publication_attempts_system_automation_insert": (
        "publication_attempts", "INSERT", None, ATTEMPT_CREATION,
    ),
    "publication_attempts_system_automation_update": (
        "publication_attempts", "UPDATE", ATTEMPT_TENANT, ATTEMPT_TENANT,
    ),
    "content_items_system_automation_select": (
        "content_items", "SELECT", SYSTEM_TENANT, None,
    ),
    "content_items_system_automation_update": (
        "content_items", "UPDATE", CONTENT_EXECUTION, CONTENT_EXECUTION,
    ),
    "content_versions_system_automation_select": (
        "content_versions", "SELECT", VERSION_TENANT, None,
    ),
    "content_assets_system_automation_select": (
        "content_assets", "SELECT", CONTENT_ASSET_TENANT, None,
    ),
    "platform_connections_system_automation_select": (
        "platform_connections", "SELECT", CONNECTION_EXECUTION, None,
    ),
    "platform_connections_system_automation_update": (
        "platform_connections", "UPDATE", CONNECTION_EXECUTION, CONNECTION_EXECUTION,
    ),
    "audit_logs_system_automation_insert": (
        "audit_logs", "INSERT", None, AUDIT_EXECUTION,
    ),
}


def policy_catalog_snapshot(bind):
    return BASE.BASE.policy_catalog_snapshot(bind)


def expected_baseline_identities():
    identities = set(BASE.expected_historical_policy_identities())
    identities.update(
        (table, name) for name, (table, _, _, _) in BASE.POLICIES.items()
    )
    return frozenset(identities)


def role_acl_snapshot(bind, role):
    return tuple(bind.execute(sa.text("""WITH target AS (
        SELECT oid FROM pg_roles WHERE rolname=:role
    )
        SELECT 'schema',n.nspname,'',a.privilege_type,a.is_grantable
        FROM pg_namespace n
        CROSS JOIN LATERAL aclexplode(COALESCE(n.nspacl,acldefault('n',n.nspowner))) a
        WHERE n.nspname='public' AND a.grantee=(SELECT oid FROM target)
        UNION ALL
        SELECT CASE WHEN c.relkind='S' THEN 'sequence' ELSE 'relation' END,
               c.relname,'',a.privilege_type,a.is_grantable
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        CROSS JOIN LATERAL aclexplode(COALESCE(
            c.relacl,acldefault(CASE WHEN c.relkind='S' THEN 'S' ELSE 'r' END::"char",c.relowner)
        )) a
        WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','f','S')
          AND a.grantee=(SELECT oid FROM target)
        UNION ALL
        SELECT 'column',c.relname,at.attname,a.privilege_type,a.is_grantable
        FROM pg_attribute at JOIN pg_class c ON c.oid=at.attrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        CROSS JOIN LATERAL aclexplode(COALESCE(at.attacl,acldefault('c',c.relowner))) a
        WHERE n.nspname='public' AND at.attnum>0 AND NOT at.attisdropped
          AND a.grantee=(SELECT oid FROM target)
        UNION ALL
        SELECT 'function',p.oid::regprocedure::text,'',a.privilege_type,a.is_grantable
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
        WHERE n.nspname='public' AND a.grantee=(SELECT oid FROM target)
        UNION ALL
        SELECT 'type',t.oid::regtype::text,'',a.privilege_type,a.is_grantable
        FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
        CROSS JOIN LATERAL aclexplode(COALESCE(t.typacl,acldefault('T',t.typowner))) a
        WHERE n.nspname='public' AND a.grantee=(SELECT oid FROM target)
        ORDER BY 1,2,3,4,5"""), {"role": role}).all())


def verify_baseline(bind):
    verify_frozen_previous()
    if bind.scalar(sa.text("SELECT version_num FROM alembic_version")) != down_revision:
        raise RuntimeError("0010 requires exact 0009 baseline")
    policies = policy_catalog_snapshot(bind)
    actual = frozenset((table, name) for _, table, name, *_ in policies)
    expected = expected_baseline_identities()
    if (len(policies) != 45 or len(expected) != 45 or actual != expected
            or any(schema != "public" for schema, *_ in policies)):
        raise RuntimeError("Expected exact 45-policy 0009 baseline")
    scheduler_keys = {
        (table, name) for name, (table, _, _, _) in BASE.POLICIES.items()
    }
    historical = tuple(
        row for row in policies if (row[1], row[2]) not in scheduler_keys
    )
    BASE.verify_policies(bind, historical)
    BASE.BASE._verify_active_index(bind, expected_retryable=True)
    BASE.BASE.verify_grants(bind)
    scheduler = BASE._scheduler_role(bind)
    BASE.verify_table_posture(bind, scheduler["oid"])
    function_snapshot = BASE.function_execute_snapshot(bind)
    BASE.verify_scheduler_grants(bind, function_snapshot=function_snapshot)
    return (
        policies,
        role_acl_snapshot(bind, RUNTIME_ROLE),
        role_acl_snapshot(bind, SCHEDULER_ROLE),
        function_snapshot,
        scheduler,
    )


def _apply_system_policies():
    for name, (table, command, using, check) in POLICIES.items():
        ddl = f"CREATE POLICY {name} ON public.{table} FOR {command} TO {RUNTIME_ROLE}"
        if using is not None:
            ddl += f" USING ({using})"
        if check is not None:
            ddl += f" WITH CHECK ({check})"
        op.execute(ddl)


def verify_policies(bind, historical_snapshot):
    rows = policy_catalog_snapshot(bind)
    new_keys = {(table, name) for name, (table, _, _, _) in POLICIES.items()}
    system_rows = tuple(row for row in rows if (row[1], row[2]) in new_keys)
    historical = tuple(row for row in rows if (row[1], row[2]) not in new_keys)
    if (len(rows) != 58 or len(system_rows) != 13
            or {(row[1], row[2]) for row in system_rows} != new_keys
            or historical != historical_snapshot):
        raise RuntimeError("Policy drift after 0010")
    for schema, table, name, permissive, roles, command, using, check in system_rows:
        expected_table, expected_command, expected_using, expected_check = POLICIES[name]
        if (schema != "public" or table != expected_table
                or command != expected_command or roles != (RUNTIME_ROLE,)
                or permissive != "PERMISSIVE"
                or BASE.BASE.normalized(using) != BASE.BASE.normalized(expected_using)
                or BASE.BASE.normalized(check) != BASE.BASE.normalized(expected_check)):
            raise RuntimeError(f"System automation policy drift: {name}")


def upgrade():
    if context.is_offline_mode():
        raise RuntimeError("0010 requires online baseline verification")
    bind = op.get_bind()
    historical, runtime_acl, scheduler_acl, function_snapshot, scheduler = verify_baseline(bind)
    _apply_system_policies()
    verify_policies(bind, historical)
    BASE.BASE.verify_grants(bind)
    if role_acl_snapshot(bind, RUNTIME_ROLE) != runtime_acl:
        raise RuntimeError("Runtime ACL drift after 0010")
    if role_acl_snapshot(bind, SCHEDULER_ROLE) != scheduler_acl:
        raise RuntimeError("Scheduler ACL drift after 0010")
    BASE.verify_scheduler_grants(bind, function_snapshot=function_snapshot)
    BASE.verify_table_posture(bind, scheduler["oid"])


def downgrade():
    raise RuntimeError(
        "Unsafe security downgrade blocked; a reviewed compensating migration is required"
    )
