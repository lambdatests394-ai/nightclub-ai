"""Opt-in LOCAL RLS validation. Never provisions roles or uses an admin login.

The local nightclub_api role has already been provisioned administratively.
Credentials are read only from process environment, never printed or saved.
Creates/drops ONLY nightclub_ai_prompt5_test. Refuses preexisting databases.
Retain nightclub_api locally unless a future explicit lifecycle decision changes
that. Do not drop it automatically or as part of Prompt 5 validation cleanup.
"""
import os
from pathlib import Path
import subprocess
import sys

import psycopg
from psycopg.conninfo import make_conninfo
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]
DATABASE = "nightclub_ai_prompt5_test"
REVISION = "20260909_0003"
USER_A = "10000000-0000-0000-0000-000000000001"
USER_B = "10000000-0000-0000-0000-000000000002"
USER_INACTIVE = "10000000-0000-0000-0000-000000000003"
USER_REMOVED = "10000000-0000-0000-0000-000000000004"
ORG_A = "20000000-0000-0000-0000-000000000001"
ORG_B = "20000000-0000-0000-0000-000000000002"
ORG_INACTIVE = "20000000-0000-0000-0000-000000000003"
ORG_A2 = "20000000-0000-0000-0000-000000000004"


def local_url(name, role):
    try:
        url = make_url(os.environ.get(name, ""))
    except Exception:
        raise RuntimeError(f"{name}: explicit local URL required (value withheld)") from None
    if (url.host not in {"localhost", "127.0.0.1"} or url.database != DATABASE
            or url.username != role or not url.password or url.port != 5432 or url.query
            or url.drivername not in {"postgresql+psycopg", "postgresql+asyncpg"}):
        raise RuntimeError(f"{name}: unsafe local validation target (value withheld)")
    return url


def connection_info(url, database=DATABASE):
    return make_conninfo(host=url.host, port=url.port, dbname=database,
                         user=url.username, password=url.password, connect_timeout=5)


def seed(connection):
    # Seed ONLY before FORCE RLS. No owner/bypass test policies are introduced.
    for user, active in [(USER_A, True), (USER_B, True), (USER_INACTIVE, False), (USER_REMOVED, True)]:
        connection.execute("INSERT INTO auth.users (id) VALUES (%s)", (user,))
        connection.execute("INSERT INTO public.profiles (id, is_active) VALUES (%s, %s)", (user, active))
    for org, active in [(ORG_A, True), (ORG_B, True), (ORG_INACTIVE, False), (ORG_A2, True)]:
        connection.execute("INSERT INTO public.organizations (id, name, slug, is_active) VALUES (%s, %s, %s, %s)",
                           (org, "Local RLS fixture", org, active))
    for user, org in [(USER_A, ORG_A), (USER_A, ORG_A2), (USER_A, ORG_INACTIVE),
                      (USER_B, ORG_B), (USER_INACTIVE, ORG_A), (USER_REMOVED, ORG_B)]:
        connection.execute("INSERT INTO public.organization_members (user_id, organization_id, role) VALUES (%s, %s, 'viewer')",
                           (user, org))
    connection.execute("DELETE FROM public.organization_members WHERE user_id = %s", (USER_REMOVED,))


def run():
    migration = local_url("DATABASE_MIGRATION_URL", "alembic_test_user")
    runtime = local_url("PROMPT5_RUNTIME_URL", "nightclub_api")
    print("VALIDATED_LOCAL_HOSTS:", migration.host, runtime.host, flush=True)
    child_env = os.environ.copy()
    child_env.pop("DATABASE_URL", None)
    child_env["DATABASE_RUNTIME_EXPECTED_ROLE"] = "nightclub_api"
    role_query = ("SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolcanlogin, "
                  "rolreplication, rolbypassrls, rolinherit FROM pg_roles WHERE rolname = %s")
    with psycopg.connect(connection_info(migration, "postgres"), autocommit=True) as control:
        role = control.execute(role_query, ("alembic_test_user",)).fetchone()
        if role is None or role[1:7] != (False, False, True, True, False, False):
            raise RuntimeError("Unexpected permanent migration-validation role attributes")
        runtime_role = control.execute(role_query, ("nightclub_api",)).fetchone()
        if runtime_role != ("nightclub_api", False, False, False, True, False, False, False):
            raise RuntimeError("STOP: administrator must provision the safe disposable runtime role")
        print("LOCAL_MIGRATION_ROLE:", role, flush=True)
        print("LOCAL_RUNTIME_ROLE:", runtime_role, flush=True)
        if control.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DATABASE,)).fetchone():
            raise RuntimeError("Test database already exists; inspect it before any deletion")
        control.execute("CREATE DATABASE nightclub_ai_prompt5_test OWNER alembic_test_user")
        print("CREATE DATABASE nightclub_ai_prompt5_test: OK", flush=True)
        try:
            with psycopg.connect(connection_info(migration)) as connection:
                connection.execute((ROOT / "scripts/local-dev/auth_stub.sql").read_text(encoding="utf-8"))
            print("APPLY LOCAL auth_stub.sql: OK", flush=True)
            with psycopg.connect(connection_info(runtime)) as connection:
                assert connection.execute("SELECT current_user").fetchone() == ("nightclub_api",)
            print("RUNTIME CONNECTIVITY: OK", flush=True)

            def command(*args, expected_success=True):
                print("RUN: python", " ".join(args), flush=True)
                result = subprocess.run([sys.executable, *args], cwd=ROOT, env=child_env, check=False,
                                        capture_output=not expected_success, text=True)
                print("EXIT_STATUS:", result.returncode, flush=True)
                if expected_success and result.returncode != 0:
                    raise RuntimeError("Validation command failed; no retry or silent repair")
                if not expected_success and result.returncode == 0:
                    raise RuntimeError("Unsafe downgrade unexpectedly succeeded")
                if not expected_success:
                    output = result.stdout + result.stderr
                    marker = "Unsafe security downgrade blocked; a reviewed compensating migration is required"
                    if marker not in output:
                        raise RuntimeError("Unexpected downgrade failure; refusal was NOT validated")
                    print(output, end="", flush=True)

            command("-m", "alembic", "upgrade", "20260907_0002")
            with psycopg.connect(connection_info(migration)) as connection:
                seed(connection)
            command("-m", "alembic", "upgrade", "head")
            with psycopg.connect(connection_info(migration)) as connection:
                if connection.execute("SELECT count(*) FROM public.profiles").fetchone() != (0,):
                    raise RuntimeError("FORCE owner isolation failed")
            print("FORCE_OWNER_READ_DENIED: OK (zero visible rows, seeded profiles exist)", flush=True)
            command("-m", "pytest", "backend/tests", "--prompt5-postgres", "-q", "-ra")
            command("-m", "alembic", "downgrade", "20260907_0002", expected_success=False)
            # The test below also asserts the guard's exact error in-process.
            # Recheck head/policies/FORCE after the refused downgrade.
            command("-m", "pytest", "backend/tests/test_rls_postgres.py", "--prompt5-postgres",
                    "-q", "-k", "catalog")
            with psycopg.connect(connection_info(migration)) as connection:
                head = connection.execute("SELECT version_num FROM alembic_version").fetchone()
                if head != (REVISION,): raise RuntimeError("Unexpected Alembic head")
                print("ALEMBIC_HEAD:", head[0], flush=True)
                for row in connection.execute("SELECT tablename, policyname, roles, cmd, qual FROM pg_policies WHERE schemaname = 'public' ORDER BY tablename"):
                    print("POLICY:", row, flush=True)
                for row in connection.execute("SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, pg_get_userbyid(c.relowner) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY c.relname"):
                    print("CATALOG_TABLE:", row, flush=True)
        finally:
            sessions = control.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = %s", (DATABASE,)).fetchone()[0]
            if sessions:
                raise RuntimeError("Cleanup blocked: test sessions remain; no sessions terminated")
            control.execute("DROP DATABASE nightclub_ai_prompt5_test")
            remaining = control.execute("SELECT datname FROM pg_database WHERE datname = %s", (DATABASE,)).fetchall()
            print("TEST_DATABASE_REMAINING:", remaining, flush=True)
            print("PERMANENT_ROLE_RETAINED:", control.execute(role_query, ("alembic_test_user",)).fetchone(), flush=True)
            print("LOCAL_RUNTIME_ROLE_RETAINED: nightclub_api must NOT be dropped automatically or as part of Prompt 5 validation.", flush=True)


if __name__ == "__main__":
    try:
        run()
    except Exception:
        # Connection exceptions can contain DSNs. Never print their text.
        print("VALIDATION FAILED OR BLOCKED; inspect the last safe step output. No credentials printed.", flush=True)
        sys.exit(1)
    finally:
        for key in ("DATABASE_MIGRATION_URL", "PROMPT5_RUNTIME_URL"):
            os.environ.pop(key, None)
        print("CHILD_PROCESS_URLS_REMOVED; remove parent PowerShell variables separately.", flush=True)
