"""Independent LOCAL Prompt 9 harness; credentials stay in caller environment.

Only nightclub_ai_prompt9_test is created/dropped. Retained validation roles are
inspected and must remain unchanged. Provider traffic is mocked by the tests.
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
DATABASE = "nightclub_ai_prompt9_test"
REVISION = "20260923_0007"
ROLE_QUERY = "SELECT rolname,rolsuper,rolcreaterole,rolcreatedb,rolcanlogin,rolreplication,rolbypassrls,rolinherit FROM pg_roles WHERE rolname=%s"
POLICY_QUERY = "SELECT tablename,policyname,roles,cmd,permissive,qual,with_check FROM pg_policies WHERE schemaname='public' ORDER BY policyname"


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
    return make_conninfo(host=url.host, port=url.port, dbname=database,
                         user=url.username, password=url.password, connect_timeout=5)


def seed(connection):
    spec = spec_from_file_location("prompt9_seed", ROOT / "scripts/local-dev/validate_prompt8.py")
    prior = module_from_spec(spec)
    spec.loader.exec_module(prior)
    prior.seed(connection)


def memberships(connection):
    return connection.execute("""SELECT r.rolname,m.roleid,m.admin_option,m.inherit_option,m.set_option
        FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member
        WHERE r.rolname IN ('nightclub_api','alembic_test_user') ORDER BY 1,2""").fetchall()


def run():
    migration = local_url("DATABASE_MIGRATION_URL", "alembic_test_user")
    runtime = local_url("PROMPT9_RUNTIME_URL", "nightclub_api")
    if migration.host != runtime.host:
        raise RuntimeError("Use the same explicit local hostname for both URLs")
    print("VALIDATED_LOCAL_HOSTS:", migration.host, runtime.host, flush=True)
    child = os.environ.copy()
    for name in ("DATABASE_URL", "PROMPT4_DATABASE_URL", "PROMPT5_RUNTIME_URL",
                 "PROMPT6_RUNTIME_URL", "PROMPT7_RUNTIME_URL", "PROMPT8_RUNTIME_URL",
                 "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_JWKS_URL",
                 "SUPABASE_JWT_ISSUER", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        child[name] = ""
    child["DATABASE_RUNTIME_EXPECTED_ROLE"] = "nightclub_api"
    with psycopg.connect(connection_info(migration, "postgres"), autocommit=True) as control:
        migration_role = control.execute(ROLE_QUERY, ("alembic_test_user",)).fetchone()
        runtime_role = control.execute(ROLE_QUERY, ("nightclub_api",)).fetchone()
        if migration_role is None or migration_role[1:7] != (False, False, True, True, False, False):
            raise RuntimeError("Unexpected migration role attributes")
        if runtime_role != ("nightclub_api", False, False, False, True, False, False, False):
            raise RuntimeError("Unsafe runtime role; no provisioning performed")
        before_memberships = memberships(control)
        if before_memberships:
            raise RuntimeError("Validation roles must not have role memberships")
        if int(control.execute("SHOW server_version_num").fetchone()[0]) < 160000:
            raise RuntimeError("PostgreSQL 16 or newer required")
        if control.execute("SELECT 1 FROM pg_database WHERE datname=%s", (DATABASE,)).fetchone():
            raise RuntimeError("Test database exists; stop for inspection")
        control.execute("CREATE DATABASE nightclub_ai_prompt9_test OWNER alembic_test_user")
        print("CREATE DATABASE nightclub_ai_prompt9_test: OK", flush=True)
        try:
            with psycopg.connect(connection_info(migration)) as connection:
                connection.execute((ROOT / "scripts/local-dev/auth_stub.sql").read_text(encoding="utf-8"))
            with psycopg.connect(connection_info(runtime)) as connection:
                if connection.execute("SELECT current_user").fetchone() != ("nightclub_api",):
                    raise RuntimeError("Runtime connection mismatch")
            print("RUNTIME CONNECTIVITY: PASS", flush=True)

            def command(*args, blocked=False):
                print("RUN: python", " ".join(args), flush=True)
                result = subprocess.run([sys.executable, *args], cwd=ROOT, env=child,
                                        capture_output=True, text=True, check=False)
                output = result.stdout + result.stderr
                for url in (migration, runtime):
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
            for revision in ("20260909_0003", "20260910_0004", "20260910_0005", "20260922_0006"):
                command("-m", "alembic", "upgrade", revision)
            with psycopg.connect(connection_info(migration)) as connection:
                before = connection.execute(POLICY_QUERY).fetchall()
                if len(before) != 24:
                    raise RuntimeError("Expected 24 baseline policies at 0006")
            command("-m", "alembic", "upgrade", REVISION)
            with psycopg.connect(connection_info(migration)) as connection:
                after = connection.execute(POLICY_QUERY).fetchall()
                if len(after) != 30 or not all(row in after for row in before):
                    raise RuntimeError("Historical policy drift or unexpected policy count")
                application_tables = [
                    "organizations", "profiles", "organization_members", "platform_connections",
                    "campaigns", "assets", "content_items", "content_versions", "content_assets",
                    "review_decisions", "publication_jobs", "publication_attempts",
                    "ai_generation_requests", "webhook_events", "whatsapp_conversations",
                    "whatsapp_messages", "outbox_events", "automation_runs", "idempotency_keys",
                    "audit_logs", "ai_daily_usage",
                ]
                tables = connection.execute("""SELECT count(*) FROM pg_class
                    WHERE relnamespace='public'::regnamespace AND relkind='r'
                    AND relname=ANY(%s) AND relrowsecurity AND relforcerowsecurity""",
                    (application_tables,)).fetchone()[0]
                if tables != 21:
                    raise RuntimeError("Expected 21 ENABLE/FORCE application tables")
                print("HISTORICAL_POLICIES_UNCHANGED: PASS; TOTAL_POLICIES: 30; APPLICATION_TABLES: 21", flush=True)
            command("-m", "pytest", "backend/tests", "--prompt9-postgres", "-q", "-ra", "--tb=short")
            command("-m", "alembic", "downgrade", "20260922_0006", blocked=True)
            command("-m", "pytest", "backend/tests/test_ai_postgres.py", "--prompt9-postgres",
                    "-q", "-k", "catalog", "--tb=short")
            with psycopg.connect(connection_info(migration)) as connection:
                head = connection.execute("SELECT version_num FROM alembic_version").fetchone()
                if head != (REVISION,):
                    raise RuntimeError("Unexpected final head")
                print("ALEMBIC_HEAD:", head[0], flush=True)
        finally:
            sessions = control.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=%s", (DATABASE,)).fetchone()[0]
            if sessions:
                raise RuntimeError("Cleanup blocked by sessions; none terminated")
            control.execute("DROP DATABASE nightclub_ai_prompt9_test")
            remaining = control.execute("SELECT datname FROM pg_database WHERE datname=%s", (DATABASE,)).fetchall()
            print("TEST_DATABASE_REMAINING:", remaining, flush=True)
            if remaining:
                raise RuntimeError("Test database cleanup failed")
            for role, expected in (("nightclub_api", runtime_role), ("alembic_test_user", migration_role)):
                if control.execute(ROLE_QUERY, (role,)).fetchone() != expected:
                    raise RuntimeError("Retained role changed")
                print("ROLE_RETAINED_UNCHANGED:", role, flush=True)
            if memberships(control) != before_memberships:
                raise RuntimeError("Retained role memberships changed")


if __name__ == "__main__":
    try:
        run()
    except Exception:
        print("VALIDATION FAILED OR BLOCKED; inspect last safe output. No credentials printed.", flush=True)
        sys.exit(1)
    finally:
        for name in ("DATABASE_MIGRATION_URL", "PROMPT9_RUNTIME_URL"):
            os.environ.pop(name, None)
        print("CHILD_PROCESS_URLS_REMOVED; remove parent PowerShell variables separately.", flush=True)
