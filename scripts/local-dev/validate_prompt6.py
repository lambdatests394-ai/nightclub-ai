"""Independent LOCAL Prompt 6 validation. Credentials are environment-only.

Creates/drops only nightclub_ai_prompt6_test; refuses an existing database.
Never provisions, alters, grants membership to, or drops either retained role.
No remote services. Frozen SQL is never executed; auth_stub is local-only.
"""
import os
from pathlib import Path
import subprocess
import sys

import psycopg
from psycopg.conninfo import make_conninfo
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]
DATABASE = "nightclub_ai_prompt6_test"
REVISION = "20260910_0004"
USER_A = "10000000-0000-0000-0000-000000000001"
USER_B = "10000000-0000-0000-0000-000000000002"
ORG_A = "20000000-0000-0000-0000-000000000001"
ORG_B = "20000000-0000-0000-0000-000000000002"


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
    # All identity fixtures before FORCE; no test bypass policies or role changes.
    for index, role in enumerate(("owner", "manager", "editor", "reviewer", "operator", "viewer"), 1):
        user = f"10000000-0000-0000-0000-{index:012d}"
        connection.execute("INSERT INTO auth.users(id) VALUES (%s)", (user,))
        connection.execute("INSERT INTO public.profiles(id) VALUES (%s)", (user,))
    for org in (ORG_A, ORG_B):
        connection.execute("INSERT INTO public.organizations(id,name,slug) VALUES (%s,%s,%s)",
                           (org, "Local campaign fixture", org))
    for index, role in enumerate(("owner", "manager", "editor", "reviewer", "operator", "viewer"), 1):
        user = f"10000000-0000-0000-0000-{index:012d}"
        connection.execute("INSERT INTO public.organization_members(organization_id,user_id,role) VALUES (%s,%s,%s)",
                           (ORG_A, user, role))
    connection.execute("INSERT INTO public.organization_members(organization_id,user_id,role) VALUES (%s,%s,'manager')", (ORG_B, USER_B))
    # Invalid profile / inactive organization / removed membership fixtures.
    for index in (7, 8):
        user = f"10000000-0000-0000-0000-{index:012d}"
        connection.execute("INSERT INTO auth.users(id) VALUES (%s)", (user,))
        connection.execute("INSERT INTO public.profiles(id,is_active) VALUES (%s,%s)", (user, index != 7))
        connection.execute("INSERT INTO public.organization_members(organization_id,user_id,role) VALUES (%s,%s,'viewer')", (ORG_A, user))
    connection.execute("DELETE FROM public.organization_members WHERE user_id = '10000000-0000-0000-0000-000000000008'")
    connection.execute("INSERT INTO public.organizations(id,name,slug,is_active) VALUES ('20000000-0000-0000-0000-000000000003','Inactive local','inactive-local',false)")
    connection.execute("INSERT INTO public.organization_members(organization_id,user_id,role) VALUES ('20000000-0000-0000-0000-000000000003',%s,'owner')", (USER_A,))


def run():
    migration = local_url("DATABASE_MIGRATION_URL", "alembic_test_user")
    runtime = local_url("PROMPT6_RUNTIME_URL", "nightclub_api")
    if migration.host != runtime.host:
        raise RuntimeError("Use the same explicit local hostname for both URLs")
    print("VALIDATED_LOCAL_HOSTS:", migration.host, runtime.host, flush=True)
    child = os.environ.copy()
    child.pop("DATABASE_URL", None)
    child.pop("PROMPT5_RUNTIME_URL", None)
    child["DATABASE_RUNTIME_EXPECTED_ROLE"] = "nightclub_api"
    role_query = "SELECT rolname,rolsuper,rolcreaterole,rolcreatedb,rolcanlogin,rolreplication,rolbypassrls,rolinherit FROM pg_roles WHERE rolname=%s"
    with psycopg.connect(connection_info(migration, "postgres"), autocommit=True) as control:
        migration_role = control.execute(role_query, ("alembic_test_user",)).fetchone()
        runtime_role = control.execute(role_query, ("nightclub_api",)).fetchone()
        if migration_role is None or migration_role[1:7] != (False, False, True, True, False, False):
            raise RuntimeError("Unexpected migration role attributes")
        if runtime_role != ("nightclub_api", False, False, False, True, False, False, False):
            raise RuntimeError("Unsafe runtime role; no provisioning performed")
        if control.execute("SELECT 1 FROM pg_database WHERE datname=%s", (DATABASE,)).fetchone():
            raise RuntimeError("Test database exists; stop for inspection")
        control.execute("CREATE DATABASE nightclub_ai_prompt6_test OWNER alembic_test_user")
        print("CREATE DATABASE nightclub_ai_prompt6_test: OK", flush=True)
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
                # Defense against an unexpected driver/pytest diagnostic echoing a DSN.
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
            command("-m", "alembic", "upgrade", "20260909_0003")
            with psycopg.connect(connection_info(migration)) as connection:
                before = connection.execute("SELECT tablename,policyname,roles,cmd,qual,with_check FROM pg_policies WHERE schemaname='public' ORDER BY policyname").fetchall()
                if len(before) != 3:
                    raise RuntimeError("Expected three bootstrap policies at 0003")
            command("-m", "alembic", "upgrade", REVISION)
            with psycopg.connect(connection_info(migration)) as connection:
                after = connection.execute("SELECT tablename,policyname,roles,cmd,qual,with_check FROM pg_policies WHERE schemaname='public' ORDER BY policyname").fetchall()
                if len(after) != 10 or not all(row in after for row in before):
                    raise RuntimeError("Bootstrap changed or unexpected campaign policies")
                print("BOOTSTRAP_UNCHANGED: PASS; TOTAL_POLICIES: 10", flush=True)
            command("-m", "pytest", "backend/tests", "--prompt6-postgres", "-q", "-ra", "--tb=short")
            command("-m", "alembic", "downgrade", "20260909_0003", blocked=True)
            command("-m", "pytest", "backend/tests/test_campaigns_postgres.py", "--prompt6-postgres", "-q", "-k", "catalog", "--tb=short")
            with psycopg.connect(connection_info(migration)) as connection:
                head = connection.execute("SELECT version_num FROM alembic_version").fetchone()
                if head != (REVISION,):
                    raise RuntimeError("Unexpected final head")
                print("ALEMBIC_HEAD:", head[0], flush=True)
        finally:
            sessions = control.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=%s", (DATABASE,)).fetchone()[0]
            if sessions:
                raise RuntimeError("Cleanup blocked by sessions; none terminated")
            control.execute("DROP DATABASE nightclub_ai_prompt6_test")
            remaining = control.execute("SELECT datname FROM pg_database WHERE datname=%s", (DATABASE,)).fetchall()
            print("TEST_DATABASE_REMAINING:", remaining, flush=True)
            if remaining:
                raise RuntimeError("Test database cleanup failed")
            for role, expected in (("nightclub_api", runtime_role), ("alembic_test_user", migration_role)):
                actual = control.execute(role_query, (role,)).fetchone()
                if actual != expected:
                    raise RuntimeError("Retained role changed")
                print("ROLE_RETAINED_UNCHANGED:", role, flush=True)


if __name__ == "__main__":
    try:
        run()
    except Exception:
        print("VALIDATION FAILED OR BLOCKED; inspect last safe output. No credentials printed.", flush=True)
        sys.exit(1)
    finally:
        for name in ("DATABASE_MIGRATION_URL", "PROMPT6_RUNTIME_URL"):
            os.environ.pop(name, None)
        print("CHILD_PROCESS_URLS_REMOVED; remove parent PowerShell variables separately.", flush=True)
