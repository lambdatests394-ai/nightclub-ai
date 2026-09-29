"""LOCAL Prompt 11 B3 certification; never contacts remote infrastructure.

Credentials are accepted only from the caller environment. The retained
migration, runtime and scheduler roles are inspected and never changed. Only
nightclub_ai_prompt11_system_test may be created and dropped.
"""

import base64
from importlib.util import module_from_spec, spec_from_file_location
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import psycopg
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr
from sqlalchemy.engine import make_url

from backend.app.modules.integrations.credentials import (
    CredentialBinding,
    CredentialCipher,
    FacebookCredentials,
)


DATABASE = "nightclub_ai_prompt11_system_test"
REVISION = "20260928_0010"
ROLE_QUERY = """SELECT rolname,rolsuper,rolcreaterole,rolcreatedb,rolcanlogin,
    rolreplication,rolbypassrls,rolinherit FROM pg_roles WHERE rolname=%s"""
POLICY_QUERY = """SELECT schemaname,tablename,policyname,permissive,roles,cmd,qual,with_check
    FROM pg_policies WHERE schemaname='public' ORDER BY policyname,tablename"""
ACL_QUERY = """WITH target AS (SELECT oid FROM pg_roles WHERE rolname=%s)
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
    ORDER BY 1,2,3,4,5"""
APPLICATION_TABLES = (
    "organizations", "profiles", "organization_members", "platform_connections",
    "campaigns", "assets", "content_items", "content_versions", "content_assets",
    "review_decisions", "publication_jobs", "publication_attempts",
    "ai_generation_requests", "webhook_events", "whatsapp_conversations",
    "whatsapp_messages", "outbox_events", "automation_runs", "idempotency_keys",
    "audit_logs", "ai_daily_usage", "facebook_oauth_states",
)
USER_A = "10000000-0000-0000-0000-000000000001"
REMOVED_USER = "10000000-0000-0000-0000-000000000008"
ORG_A = "20000000-0000-0000-0000-000000000001"
ORG_B = "20000000-0000-0000-0000-000000000002"
ORG_INACTIVE = "20000000-0000-0000-0000-000000000003"


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


def seed(connection):
    spec = spec_from_file_location(
        "prompt11_system_seed", ROOT / "scripts/local-dev/validate_prompt8.py",
    )
    prior = module_from_spec(spec)
    spec.loader.exec_module(prior)
    prior.seed(connection)

    key = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
    cipher = CredentialCipher(key, 1)
    connection_a = fixture_id(30000000, 1)
    envelope_a = cipher.encrypt(
        FacebookCredentials(SecretStr("synthetic-system-token")),
        CredentialBinding(ORG_A, "facebook", "local-1", 1),
    )
    connection.execute("""UPDATE public.platform_connections
        SET credentials_ciphertext=%s,token_expires_at='2100-01-01 00:00:00+00',
            last_verified_at='2026-09-28 17:00:00+00'
        WHERE id=%s""", (envelope_a, connection_a))
    connection_inactive = fixture_id(30000000, 5)
    envelope_inactive = cipher.encrypt(
        FacebookCredentials(SecretStr("unused-inactive-token")),
        CredentialBinding(ORG_INACTIVE, "facebook", "local-inactive", 1),
    )
    connection.execute("""INSERT INTO public.platform_connections
        (id,organization_id,platform,external_account_id,display_name,
         credentials_ciphertext,credential_key_version,token_expires_at,status,last_verified_at)
        VALUES (%s,%s,'facebook','local-inactive','Inactive local fixture',
                %s,1,'2100-01-01 00:00:00+00','active','2026-09-28 17:00:00+00')""",
        (connection_inactive, ORG_INACTIVE, envelope_inactive))

    # Two tenant-matrix rows with attachments; these are not executor jobs.
    for suffix, organization, actor, connection_id in (
        (1, ORG_A, USER_A, connection_a),
        (2, ORG_B, "10000000-0000-0000-0000-000000000002", fixture_id(30000000, 3)),
    ):
        content = fixture_id(96000000, suffix)
        version = fixture_id(96100000, suffix)
        job = fixture_id(96200000, suffix)
        asset = fixture_id(96300000, suffix)
        digest = f"{suffix:064x}"
        connection.execute("""INSERT INTO public.content_items
            (id,organization_id,platform,connection_id,status,current_version_no,
             approved_version_no,scheduled_for,created_by)
            VALUES (%s,%s,'facebook',%s,'scheduled',1,1,
                    '2026-09-28 17:30:00+00',%s)""",
            (content, organization, connection_id, actor))
        connection.execute("""INSERT INTO public.content_versions
            (id,content_item_id,version_no,body,source,created_by)
            VALUES (%s,%s,1,'Synthetic tenant matrix body','manual',%s)""",
            (version, content, actor))
        connection.execute("""INSERT INTO public.assets
            (id,organization_id,storage_bucket,storage_key,kind,mime_type,
             byte_size,sha256,original_filename,width,height,status,uploaded_by)
            VALUES (%s,%s,'nightclub-assets',%s,'image','image/png',32,%s,
                    'matrix.png',10,10,'ready',%s)""", (
                asset, organization,
                f"org/{organization}/assets/{asset}/matrix.png", digest, actor,
            ))
        connection.execute("""INSERT INTO public.content_assets
            (content_version_id,content_item_id,organization_id,asset_id,position)
            VALUES (%s,%s,%s,%s,0)""", (version, content, organization, asset))
        connection.execute("""INSERT INTO public.publication_jobs
            (id,content_item_id,content_version_id,idempotency_key,scheduled_for,
             status,created_by) VALUES (%s,%s,%s,gen_random_uuid(),
             '2026-09-28 17:30:00+00','pending',%s)""", (job, content, version, actor))
        connection.execute("""INSERT INTO public.publication_attempts
            (id,publication_job_id,attempt_no,started_at,finished_at,
             request_fingerprint,outcome)
            VALUES (%s,%s,1,'2026-09-28 17:00:00+00','2026-09-28 17:01:00+00',
                    repeat(%s,64),'succeeded')""", (
                fixture_id(96400000, suffix), job, str(suffix),
            ))

    execution_cases = (
        (3, ORG_A, REMOVED_USER, connection_a, "scheduled", "pending"),
        (4, ORG_INACTIVE, USER_A, connection_inactive, "scheduled", "pending"),
        (5, ORG_A, USER_A, connection_a, "approved", "cancelled"),
    )
    for suffix, organization, actor, connection_id, content_status, job_status in execution_cases:
        content = fixture_id(96000000, suffix)
        version = fixture_id(96100000, suffix)
        job = fixture_id(96200000, suffix)
        scheduled = "2026-09-28 17:30:00+00" if content_status == "scheduled" else None
        connection.execute("""INSERT INTO public.content_items
            (id,organization_id,platform,connection_id,status,current_version_no,
             approved_version_no,scheduled_for,created_by)
            VALUES (%s,%s,'facebook',%s,%s,1,1,%s,%s)""",
            (content, organization, connection_id, content_status, scheduled, actor))
        connection.execute("""INSERT INTO public.content_versions
            (id,content_item_id,version_no,body,source,created_by)
            VALUES (%s,%s,1,'Synthetic durable command','manual',%s)""",
            (version, content, actor))
        connection.execute("""INSERT INTO public.publication_jobs
            (id,content_item_id,content_version_id,idempotency_key,scheduled_for,
             status,created_by) VALUES (%s,%s,%s,gen_random_uuid(),
             '2026-09-28 17:30:00+00',%s,%s)""",
            (job, content, version, job_status, actor))


def run():
    migration = local_url("DATABASE_MIGRATION_URL", "alembic_test_user")
    runtime = local_url("PROMPT11_RUNTIME_URL", "nightclub_api")
    scheduler = local_url("PROMPT11_SCHEDULER_URL", "nightclub_scheduler")
    if len({migration.host, runtime.host, scheduler.host}) != 1:
        raise RuntimeError("Use the same explicit local hostname for all URLs")
    print("VALIDATED_LOCAL_HOST:", migration.host, flush=True)
    child = os.environ.copy()
    for name in (
        "DATABASE_URL", "PROMPT4_DATABASE_URL", "PROMPT5_RUNTIME_URL",
        "PROMPT6_RUNTIME_URL", "PROMPT7_RUNTIME_URL", "PROMPT8_RUNTIME_URL",
        "PROMPT9_RUNTIME_URL", "PROMPT10_RUNTIME_URL", "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_JWKS_URL", "SUPABASE_JWT_ISSUER",
        "OPENAI_API_KEY", "GEMINI_API_KEY", "META_APP_SECRET",
    ):
        child[name] = ""
    child["DATABASE_RUNTIME_EXPECTED_ROLE"] = "nightclub_api"
    child["PROMPT11_RUNTIME_URL"] = runtime.render_as_string(hide_password=False)
    child["PROMPT11_SCHEDULER_URL"] = scheduler.render_as_string(hide_password=False)

    with psycopg.connect(connection_info(migration, "postgres"), autocommit=True) as control:
        retained = {
            role: control.execute(ROLE_QUERY, (role,)).fetchone()
            for role in ("alembic_test_user", "nightclub_api", "nightclub_scheduler")
        }
        if retained["alembic_test_user"] != (
                "alembic_test_user", False, False, True, True, False, False, True):
            raise RuntimeError("Unexpected migration role attributes")
        safe = (False, False, False, True, False, False, False)
        if retained["nightclub_api"] is None or retained["nightclub_api"][1:] != safe:
            raise RuntimeError("Unsafe runtime role; no provisioning performed")
        if retained["nightclub_scheduler"] is None or retained["nightclub_scheduler"][1:] != safe:
            raise RuntimeError("Unsafe scheduler role; no provisioning performed")
        before_memberships = role_memberships(control)
        if before_memberships:
            raise RuntimeError("Validation roles must not have role memberships")
        if int(control.execute("SHOW server_version_num").fetchone()[0]) < 160000:
            raise RuntimeError("PostgreSQL 16 or newer required")
        if control.execute("SELECT 1 FROM pg_database WHERE datname=%s", (DATABASE,)).fetchone():
            raise RuntimeError("Test database exists; stop for inspection")
        control.execute("CREATE DATABASE nightclub_ai_prompt11_system_test OWNER alembic_test_user")
        print("CREATE DATABASE nightclub_ai_prompt11_system_test: OK", flush=True)
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
                "20260926_0009",
            ):
                command("-m", "alembic", "upgrade", target)
            with psycopg.connect(connection_info(migration)) as connection:
                before_policies = connection.execute(POLICY_QUERY).fetchall()
                before_runtime_acl = connection.execute(
                    ACL_QUERY, ("nightclub_api",),
                ).fetchall()
                before_scheduler_acl = connection.execute(
                    ACL_QUERY, ("nightclub_scheduler",),
                ).fetchall()
                if len(before_policies) != 45:
                    raise RuntimeError("Expected exact 45-policy B2 baseline")
                if connection.execute("SELECT version_num FROM alembic_version").fetchone() != ("20260926_0009",):
                    raise RuntimeError("Expected exact 0009 head")
                print("ALEMBIC_BASELINE: 20260926_0009; POLICIES: 45", flush=True)
            command("-m", "alembic", "upgrade", REVISION)
            with psycopg.connect(connection_info(migration)) as connection:
                after_policies = connection.execute(POLICY_QUERY).fetchall()
                system = [row for row in after_policies if "_system_automation_" in row[2]]
                if (len(after_policies) != 58 or len(system) != 13
                        or not all(row in after_policies for row in before_policies)):
                    raise RuntimeError("Historical policy drift or unexpected B3 policy matrix")
                if connection.execute(ACL_QUERY, ("nightclub_api",)).fetchall() != before_runtime_acl:
                    raise RuntimeError("nightclub_api ACL changed")
                if connection.execute(ACL_QUERY, ("nightclub_scheduler",)).fetchall() != before_scheduler_acl:
                    raise RuntimeError("nightclub_scheduler ACL changed")
                force_count = connection.execute("""SELECT count(*) FROM pg_class
                    WHERE relnamespace='public'::regnamespace AND relkind='r'
                      AND relname=ANY(%s) AND relrowsecurity AND relforcerowsecurity""",
                    (list(APPLICATION_TABLES),)).fetchone()[0]
                if force_count != 22:
                    raise RuntimeError("Expected 22 ENABLE/FORCE application tables")
                print("HISTORICAL_POLICIES_UNCHANGED: PASS; TOTAL_POLICIES: 58; SYSTEM_POLICIES: 13", flush=True)
                print("RUNTIME_SCHEDULER_ACL_UNCHANGED: PASS; ENABLE_FORCE_TABLES: 22", flush=True)
            command(
                "-m", "pytest", "backend/tests/test_system_automation_postgres.py",
                "--prompt11-system-postgres", "-q", "-ra", "--tb=short",
            )
            command("-m", "alembic", "downgrade", "20260926_0009", blocked=True)
            with psycopg.connect(connection_info(migration)) as connection:
                if connection.execute("SELECT version_num FROM alembic_version").fetchone() != (REVISION,):
                    raise RuntimeError("Unexpected final Alembic head")
                print("ALEMBIC_HEAD:", REVISION, flush=True)
            print("POSTGRES CERTIFICATION: PASS", flush=True)
        finally:
            sessions = control.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname=%s", (DATABASE,),
            ).fetchone()[0]
            if sessions:
                raise RuntimeError("Cleanup blocked by sessions; none terminated")
            control.execute("DROP DATABASE nightclub_ai_prompt11_system_test")
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
        ):
            os.environ.pop(name, None)
        print("CHILD_PROCESS_URLS_REMOVED; remove parent PowerShell variables separately.", flush=True)
