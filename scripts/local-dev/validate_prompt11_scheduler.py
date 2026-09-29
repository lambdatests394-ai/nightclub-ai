"""LOCAL Prompt 11 scheduler discovery certification; no remote services.

Credentials are accepted only from caller environment. The retained scheduler,
runtime and migration roles are inspected, never provisioned or altered here.
Only nightclub_ai_prompt11_scheduler_test may be created and dropped.
"""

from importlib.util import module_from_spec, spec_from_file_location
import os
from pathlib import Path
import subprocess
import sys

import psycopg
from psycopg.conninfo import make_conninfo
from sqlalchemy.engine import make_url


ROOT = Path(__file__).resolve().parents[2]
DATABASE = "nightclub_ai_prompt11_scheduler_test"
REVISION = "20260926_0009"
ROLE_QUERY = """SELECT rolname,rolsuper,rolcreaterole,rolcreatedb,rolcanlogin,
    rolreplication,rolbypassrls,rolinherit FROM pg_roles WHERE rolname=%s"""
POLICY_QUERY = """SELECT schemaname,tablename,policyname,permissive,roles,cmd,qual,with_check
    FROM pg_policies WHERE schemaname='public' ORDER BY policyname,tablename"""
RUNTIME_GRANTS = """SELECT 'table',table_name,'',privilege_type,is_grantable
    FROM information_schema.table_privileges
    WHERE table_schema='public' AND grantee='nightclub_api'
    UNION ALL
    SELECT 'column',table_name,column_name,privilege_type,is_grantable
    FROM information_schema.column_privileges
    WHERE table_schema='public' AND grantee='nightclub_api'
    ORDER BY 1,2,3,4,5"""

USER_A = "10000000-0000-0000-0000-000000000001"
USER_B = "10000000-0000-0000-0000-000000000002"
ORG_A = "20000000-0000-0000-0000-000000000001"
ORG_B = "20000000-0000-0000-0000-000000000002"

EXPECTED_MIGRATION_ROLE = (
    "alembic_test_user", False, False, True, True, False, False, True,
)

CASES = (
    (1, ORG_A, USER_A, "pending", "now", None, None, None),
    (2, ORG_A, USER_A, "pending", "future", None, None, None),
    (3, ORG_A, USER_A, "retryable_failure", "past", "past", None, None),
    (4, ORG_A, USER_A, "retryable_failure", "past", "future", None, None),
    (5, ORG_A, USER_A, "retryable_failure", "past", None, None, None),
    (6, ORG_A, USER_A, "leased", "past", None, "past", None),
    (7, ORG_A, USER_A, "leased", "past", None, "future", None),
    (8, ORG_A, USER_A, "leased", "past", None, None, None),
    (9, ORG_B, USER_B, "publishing", "past", None, "past", None),
    (10, ORG_B, USER_B, "publishing", "past", None, "future", None),
    (11, ORG_B, USER_B, "publishing", "past", None, None, None),
    (12, ORG_B, USER_B, "succeeded", "past", None, None, "synthetic-post"),
    (13, ORG_B, USER_B, "permanent_failure", "past", None, None, None),
    (14, ORG_B, USER_B, "cancelled", "past", None, None, None),
)


def local_url(name, role):
    try:
        url = make_url(os.environ.get(name, ""))
    except Exception:
        raise RuntimeError(f"{name}: local environment required (value withheld)") from None
    if (url.host not in {"localhost", "127.0.0.1"} or url.database != DATABASE
            or url.username != role or not url.password or url.port != 5432 or url.query
            or url.drivername not in {"postgresql+psycopg", "postgresql+asyncpg"}):
        raise RuntimeError(f"{name}: unsafe target (value withheld)")
    return url


def connection_info(url, database=DATABASE):
    return make_conninfo(
        host=url.host, port=url.port, dbname=database,
        user=url.username, password=url.password, connect_timeout=5,
    )


def role_memberships(connection):
    return connection.execute("""SELECT member.rolname,parent.rolname,m.admin_option,
        m.inherit_option,m.set_option FROM pg_auth_members m
        JOIN pg_roles member ON member.oid=m.member
        JOIN pg_roles parent ON parent.oid=m.roleid
        WHERE member.rolname IN ('alembic_test_user','nightclub_api','nightclub_scheduler')
        ORDER BY 1,2""").fetchall()


def fixture_id(prefix, suffix):
    return f"{prefix:08d}-0000-0000-0000-{suffix:012d}"


def relative_time(value):
    return {
        None: None,
        "past": "'2000-01-01 00:00:00+00'",
        "future": "'2100-01-01 00:00:00+00'",
        "now": "CURRENT_TIMESTAMP",
    }[value]


def seed(connection):
    spec = spec_from_file_location("prompt11_identity_seed", ROOT / "scripts/local-dev/validate_prompt9.py")
    prior = module_from_spec(spec)
    spec.loader.exec_module(prior)
    prior.seed(connection)
    for suffix, organization, actor, status, scheduled, retry, lease, external in CASES:
        content_id = fixture_id(92000000, suffix)
        version_id = fixture_id(93000000, suffix)
        job_id = fixture_id(91000000, suffix)
        connection.execute("""INSERT INTO public.content_items
            (id,organization_id,platform,status,current_version_no,approved_version_no,created_by)
            VALUES (%s,%s,'facebook','approved',1,1,%s)""", (content_id, organization, actor))
        connection.execute("""INSERT INTO public.content_versions
            (id,content_item_id,version_no,body,payload,source,created_by)
            VALUES (%s,%s,1,'Synthetic scheduler isolation fixture','{}','manual',%s)""",
            (version_id, content_id, actor))
        scheduled_sql = relative_time(scheduled)
        retry_sql = relative_time(retry)
        lease_sql = relative_time(lease)
        connection.execute(f"""INSERT INTO public.publication_jobs
            (id,content_item_id,content_version_id,idempotency_key,scheduled_for,status,
             attempt_count,lease_token,lease_expires_at,published_external_id,next_attempt_at,created_by)
            VALUES (%s,%s,%s,gen_random_uuid(),{scheduled_sql},%s,0,
                    CASE WHEN %s::text IS NULL THEN NULL ELSE gen_random_uuid() END,
                    {lease_sql or 'NULL'},%s,{retry_sql or 'NULL'},%s)""",
            (job_id, content_id, version_id, status, lease, external, actor))


def run():
    migration = local_url("DATABASE_MIGRATION_URL", "alembic_test_user")
    runtime = local_url("PROMPT11_RUNTIME_URL", "nightclub_api")
    if migration.host != runtime.host:
        raise RuntimeError("Use the same explicit local hostname for all URLs")
    with psycopg.connect(connection_info(migration, "postgres"), autocommit=True) as control:
        migration_role = control.execute(ROLE_QUERY, ("alembic_test_user",)).fetchone()
        runtime_role = control.execute(ROLE_QUERY, ("nightclub_api",)).fetchone()
        scheduler_role = control.execute(ROLE_QUERY, ("nightclub_scheduler",)).fetchone()
        if scheduler_role is None:
            print("POSTGRES CERTIFICATION: BLOCKED - LOCAL nightclub_scheduler ROLE REQUIRES EXTERNAL PROVISIONING", flush=True)
            print("REQUIRED POSTURE: LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS; no memberships", flush=True)
            raise RuntimeError("Scheduler role is not provisioned")
        scheduler = local_url("PROMPT11_SCHEDULER_URL", "nightclub_scheduler")
        if len({migration.host, runtime.host, scheduler.host}) != 1:
            raise RuntimeError("Use the same explicit local hostname for all URLs")
        print("VALIDATED_LOCAL_HOST:", migration.host, flush=True)
        if migration_role != EXPECTED_MIGRATION_ROLE:
            raise RuntimeError("Unexpected migration role attributes")
        safe_application_role = (False, False, False, True, False, False, False)
        if runtime_role is None or runtime_role[1:] != safe_application_role:
            raise RuntimeError("Unsafe runtime role; no provisioning performed")
        if scheduler_role[1:] != safe_application_role:
            raise RuntimeError("Unsafe scheduler role; no provisioning performed")
        before_memberships = role_memberships(control)
        if before_memberships:
            raise RuntimeError("Validation roles must not have role memberships")
        retained = {
            "alembic_test_user": migration_role,
            "nightclub_api": runtime_role,
            "nightclub_scheduler": scheduler_role,
        }
        if int(control.execute("SHOW server_version_num").fetchone()[0]) < 160000:
            raise RuntimeError("PostgreSQL 16 or newer required")
        if control.execute("SELECT 1 FROM pg_database WHERE datname=%s", (DATABASE,)).fetchone():
            raise RuntimeError("Test database exists; stop for inspection")
        control.execute("CREATE DATABASE nightclub_ai_prompt11_scheduler_test OWNER alembic_test_user")
        print("CREATE DATABASE nightclub_ai_prompt11_scheduler_test: OK", flush=True)
        child = os.environ.copy()
        for name in (
            "DATABASE_URL", "PROMPT4_DATABASE_URL", "PROMPT5_RUNTIME_URL",
            "PROMPT6_RUNTIME_URL", "PROMPT7_RUNTIME_URL", "PROMPT8_RUNTIME_URL",
            "PROMPT9_RUNTIME_URL", "PROMPT10_RUNTIME_URL", "SUPABASE_URL",
            "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_JWKS_URL", "SUPABASE_JWT_ISSUER",
            "OPENAI_API_KEY", "GEMINI_API_KEY", "META_APP_SECRET",
        ):
            child[name] = ""
        child["PROMPT11_RUNTIME_URL"] = runtime.render_as_string(hide_password=False)
        child["PROMPT11_SCHEDULER_URL"] = scheduler.render_as_string(hide_password=False)
        child["DATABASE_SCHEDULER_URL"] = scheduler.set(
            drivername="postgresql+asyncpg",
        ).render_as_string(hide_password=False)
        child["DATABASE_SCHEDULER_EXPECTED_ROLE"] = "nightclub_scheduler"
        try:
            with psycopg.connect(connection_info(migration)) as connection:
                connection.execute((ROOT / "scripts/local-dev/auth_stub.sql").read_text(encoding="utf-8"))
            for url, expected in ((runtime, "nightclub_api"), (scheduler, "nightclub_scheduler")):
                with psycopg.connect(connection_info(url)) as connection:
                    if connection.execute("SELECT current_user,session_user").fetchone() != (expected, expected):
                        raise RuntimeError("Direct application connection mismatch")
            print("DIRECT RUNTIME/SCHEDULER CONNECTIVITY: PASS", flush=True)

            def command(*args, blocked=False):
                print("RUN: python", " ".join(args), flush=True)
                result = subprocess.run(
                    [sys.executable, *args], cwd=ROOT, env=child,
                    capture_output=True, text=True, check=False,
                )
                output = result.stdout + result.stderr
                for url in (migration, runtime, scheduler):
                    output = output.replace(url.render_as_string(hide_password=False), "[DSN REDACTED]")
                    output = output.replace(url.password, "[REDACTED]")
                print(output, end="", flush=True)
                print("EXIT_STATUS:", result.returncode, flush=True)
                marker = "Unsafe security downgrade blocked; a reviewed compensating migration is required"
                if blocked:
                    if result.returncode == 0 or marker not in output:
                        raise RuntimeError("Security downgrade guard not validated")
                elif result.returncode:
                    raise RuntimeError("Validation failed; no retry or silent repair")

            command("-m", "alembic", "upgrade", "20260907_0002")
            with psycopg.connect(connection_info(migration)) as connection:
                seed(connection)
            for target in (
                "20260909_0003", "20260910_0004", "20260910_0005",
                "20260922_0006", "20260923_0007", "20260924_0008",
            ):
                command("-m", "alembic", "upgrade", target)
            with psycopg.connect(connection_info(migration)) as connection:
                before_policies = connection.execute(POLICY_QUERY).fetchall()
                before_runtime_grants = connection.execute(RUNTIME_GRANTS).fetchall()
                if len(before_policies) != 43:
                    raise RuntimeError("Expected 43-policy 0008 baseline")
                head = connection.execute("SELECT version_num FROM alembic_version").fetchone()
                if head != ("20260924_0008",):
                    raise RuntimeError("Expected exact 0008 head before scheduler migration")
                print("ALEMBIC_BASELINE: 20260924_0008; POLICIES: 43", flush=True)
            command("-m", "alembic", "upgrade", REVISION)
            with psycopg.connect(connection_info(migration)) as connection:
                after_policies = connection.execute(POLICY_QUERY).fetchall()
                after_runtime_grants = connection.execute(RUNTIME_GRANTS).fetchall()
                scheduler_policies = [row for row in after_policies if row[2].endswith("scheduler_due_select")]
                if (len(after_policies) != 45 or len(scheduler_policies) != 2
                        or not all(row in after_policies for row in before_policies)):
                    raise RuntimeError("Historical policy drift or unexpected scheduler policies")
                if after_runtime_grants != before_runtime_grants:
                    raise RuntimeError("nightclub_api grants changed")
                force_count = connection.execute("""SELECT count(*) FROM pg_class
                    WHERE relnamespace='public'::regnamespace AND relkind='r'
                      AND relname=ANY(%s) AND relrowsecurity AND relforcerowsecurity""",
                    ([
                        "organizations", "profiles", "organization_members", "platform_connections",
                        "campaigns", "assets", "content_items", "content_versions", "content_assets",
                        "review_decisions", "publication_jobs", "publication_attempts",
                        "ai_generation_requests", "webhook_events", "whatsapp_conversations",
                        "whatsapp_messages", "outbox_events", "automation_runs", "idempotency_keys",
                        "audit_logs", "ai_daily_usage", "facebook_oauth_states",
                    ],)).fetchone()[0]
                if force_count != 22:
                    raise RuntimeError("Expected 22 ENABLE/FORCE application tables")
                print("HISTORICAL_POLICIES_UNCHANGED: PASS; TOTAL_POLICIES: 45; SCHEDULER_POLICIES: 2", flush=True)
                print("NIGHTCLUB_API_GRANTS_UNCHANGED: PASS; ENABLE_FORCE_TABLES: 22", flush=True)
            command(
                "-m", "pytest", "backend/tests/test_scheduler_discovery_postgres.py",
                "--prompt11-scheduler-postgres", "-q", "-ra", "--tb=short",
            )
            command("-m", "alembic", "downgrade", "20260924_0008", blocked=True)
            command(
                "-m", "pytest", "backend/tests/test_scheduler_discovery_postgres.py",
                "--prompt11-scheduler-postgres", "-q", "-k", "direct_login",
                "--tb=short",
            )
            with psycopg.connect(connection_info(migration)) as connection:
                head = connection.execute("SELECT version_num FROM alembic_version").fetchone()
                if head != (REVISION,):
                    raise RuntimeError("Unexpected final Alembic head")
                print("ALEMBIC_HEAD:", head[0], flush=True)
            print("POSTGRES CERTIFICATION: PASS", flush=True)
        finally:
            sessions = control.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname=%s", (DATABASE,),
            ).fetchone()[0]
            if sessions:
                raise RuntimeError("Cleanup blocked by sessions; none terminated")
            control.execute("DROP DATABASE nightclub_ai_prompt11_scheduler_test")
            remaining = control.execute(
                "SELECT datname FROM pg_database WHERE datname=%s", (DATABASE,),
            ).fetchall()
            print("TEST_DATABASE_REMAINING:", remaining, flush=True)
            if remaining:
                raise RuntimeError("Test database cleanup failed")
            for role, expected in retained.items():
                if control.execute(ROLE_QUERY, (role,)).fetchone() != expected:
                    raise RuntimeError("Retained role changed")
                print("ROLE_RETAINED_UNCHANGED:", role, flush=True)
            if role_memberships(control) != before_memberships:
                raise RuntimeError("Retained role memberships changed")


if __name__ == "__main__":
    try:
        run()
    except Exception:
        print("VALIDATION FAILED OR BLOCKED; inspect last safe output. No credentials printed.", flush=True)
        sys.exit(1)
    finally:
        for name in (
            "DATABASE_MIGRATION_URL", "PROMPT11_RUNTIME_URL", "PROMPT11_SCHEDULER_URL",
            "DATABASE_SCHEDULER_URL",
        ):
            os.environ.pop(name, None)
        print("CHILD_PROCESS_URLS_REMOVED; remove parent PowerShell variables separately.", flush=True)
