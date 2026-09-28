"""Opt-in LOCAL B4 certification; never runs automatically or contacts Supabase/Meta.

Only nightclub_ai_prompt11_coordinator_test may be created and removed. Three
preprovisioned roles are checked but never created, altered, or dropped.
"""

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
from sqlalchemy.engine import make_url


DATABASE = "nightclub_ai_prompt11_coordinator_test"
REVISION = "20260928_0010"
ROLE_QUERY = """SELECT rolname,rolsuper,rolcreaterole,rolcreatedb,rolcanlogin,
    rolreplication,rolbypassrls,rolinherit FROM pg_roles WHERE rolname=%s"""
POLICY_QUERY = """SELECT schemaname,tablename,policyname,permissive,roles,cmd,qual,with_check
    FROM pg_policies WHERE schemaname='public' ORDER BY policyname,tablename"""


def local_url(name, role):
    try:
        url = make_url(os.environ.get(name, ""))
    except Exception:
        raise RuntimeError(f"{name}: local environment required (value withheld)") from None
    if (url.host not in {"localhost", "127.0.0.1"} or url.port != 5432
            or url.database != DATABASE or url.username != role or not url.password
            or url.query or url.drivername not in {"postgresql+psycopg", "postgresql+asyncpg"}):
        raise RuntimeError(f"{name}: unsafe target (value withheld)")
    return url


def conninfo(url, database=DATABASE):
    return make_conninfo(host=url.host, port=url.port, dbname=database,
                         user=url.username, password=url.password, connect_timeout=5)


def prior_harness():
    spec = spec_from_file_location(
        "prompt11_system_seed", ROOT / "scripts/local-dev/validate_prompt11_system_automation.py",
    )
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run():
    migration = local_url("DATABASE_MIGRATION_URL", "alembic_test_user")
    runtime = local_url("PROMPT11_COORDINATOR_RUNTIME_URL", "nightclub_api")
    scheduler = local_url("PROMPT11_COORDINATOR_SCHEDULER_URL", "nightclub_scheduler")
    if len({migration.host, runtime.host, scheduler.host}) != 1:
        raise RuntimeError("All roles must target the same explicit loopback host")
    prior = prior_harness()
    child = os.environ.copy()
    for name in ("DATABASE_URL", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY",
                 "SUPABASE_JWKS_URL", "OPENAI_API_KEY", "GEMINI_API_KEY",
                 "META_APP_SECRET", "DATABASE_SCHEDULER_URL"):
        child[name] = ""
    child["DATABASE_RUNTIME_EXPECTED_ROLE"] = "nightclub_api"

    def command(*args):
        result = subprocess.run([sys.executable, *args], cwd=ROOT, env=child,
                                capture_output=True, text=True, check=False)
        output = result.stdout + result.stderr
        for url in (migration, runtime, scheduler):
            output = output.replace(url.render_as_string(hide_password=False), "[DSN REDACTED]")
            output = output.replace(url.password, "[REDACTED]")
        print("RUN: python", " ".join(args), flush=True)
        print(output, end="", flush=True)
        print("EXIT_STATUS:", result.returncode, flush=True)
        if result.returncode:
            raise RuntimeError("B4 validation failed; no retry or silent repair")

    with psycopg.connect(conninfo(migration, "postgres"), autocommit=True) as control:
        roles = {name: control.execute(ROLE_QUERY, (name,)).fetchone()
                 for name in ("alembic_test_user", "nightclub_api", "nightclub_scheduler")}
        if roles["alembic_test_user"] != (
                "alembic_test_user", False, False, True, True, False, False, True):
            raise RuntimeError("Unsafe migration role")
        safe = (False, False, False, True, False, False, False)
        if any(roles[name] is None or roles[name][1:] != safe
               for name in ("nightclub_api", "nightclub_scheduler")):
            raise RuntimeError("Unsafe execution/discovery role")
        memberships = prior.role_memberships(control)
        if memberships:
            raise RuntimeError("Validation roles must have no role memberships")
        if int(control.execute("SHOW server_version_num").fetchone()[0]) < 160000:
            raise RuntimeError("PostgreSQL 16 or newer required")
        if control.execute("SELECT 1 FROM pg_database WHERE datname=%s", (DATABASE,)).fetchone():
            raise RuntimeError("Disposable database already exists; stop for inspection")
        control.execute("CREATE DATABASE nightclub_ai_prompt11_coordinator_test OWNER alembic_test_user")
        print("CREATE DATABASE nightclub_ai_prompt11_coordinator_test: OK", flush=True)
        try:
            with psycopg.connect(conninfo(migration)) as connection:
                connection.execute((ROOT / "scripts/local-dev/auth_stub.sql").read_text(encoding="utf-8"))
            for url, role in ((runtime, "nightclub_api"), (scheduler, "nightclub_scheduler")):
                with psycopg.connect(conninfo(url)) as connection:
                    if connection.execute("SELECT current_user,session_user").fetchone() != (role, role):
                        raise RuntimeError("Role direct-login check failed")
            command("-m", "alembic", "upgrade", "20260907_0002")
            with psycopg.connect(conninfo(migration)) as connection:
                prior.seed(connection)
                # Seed B4-only cases before RLS/FORCE is installed. The retained
                # migration role must never bypass RLS for post-migration writes.
                connection.execute("""UPDATE public.publication_jobs
                    SET scheduled_for=statement_timestamp()-interval '10 minutes'
                    WHERE id IN (%s,%s,%s,%s)""", tuple(
                    prior.fixture_id(96200000, n) for n in (1, 2, 3, 4)
                ))
                connection.execute("""UPDATE public.publication_jobs
                    SET status='leased', lease_token=gen_random_uuid(),
                        lease_expires_at=statement_timestamp()-interval '5 minutes'
                    WHERE id=%s""", (prior.fixture_id(96200000, 3),))
                future_content = prior.fixture_id(96000000, 6)
                future_version = prior.fixture_id(96100000, 6)
                future_job = prior.fixture_id(96200000, 6)
                connection.execute("""INSERT INTO public.content_items
                    (id,organization_id,platform,connection_id,status,current_version_no,
                     approved_version_no,scheduled_for,created_by)
                    VALUES (%s,%s,'facebook',%s,'scheduled',1,1,
                            statement_timestamp()+interval '1 day',%s)""",
                    (future_content, prior.ORG_A, prior.fixture_id(30000000, 1), prior.USER_A))
                connection.execute("""INSERT INTO public.content_versions
                    (id,content_item_id,version_no,body,source,created_by)
                    VALUES (%s,%s,1,'Synthetic future body','manual',%s)""",
                    (future_version, future_content, prior.USER_A))
                connection.execute("""INSERT INTO public.publication_jobs
                    (id,content_item_id,content_version_id,idempotency_key,
                     scheduled_for,status,created_by)
                    VALUES (%s,%s,%s,gen_random_uuid(),
                            statement_timestamp()+interval '1 day','pending',%s)""",
                    (future_job, future_content, future_version, prior.USER_A))
                durable_content = prior.fixture_id(96000000, 7)
                durable_version = prior.fixture_id(96100000, 7)
                durable_job = prior.fixture_id(96200000, 7)
                connection.execute("""INSERT INTO public.content_items
                    (id,organization_id,platform,connection_id,status,current_version_no,
                     approved_version_no,scheduled_for,created_by)
                    VALUES (%s,%s,'facebook',%s,'scheduled',1,1,
                            statement_timestamp()-interval '1 minute',%s)""",
                    (durable_content, prior.ORG_A, prior.fixture_id(30000000, 1), prior.USER_A))
                connection.execute("""INSERT INTO public.content_versions
                    (id,content_item_id,version_no,body,source,created_by)
                    VALUES (%s,%s,1,'Synthetic durable B4 body','manual',%s)""",
                    (durable_version, durable_content, prior.USER_A))
                connection.execute("""INSERT INTO public.publication_jobs
                    (id,content_item_id,content_version_id,idempotency_key,
                     scheduled_for,status,created_by)
                    VALUES (%s,%s,%s,gen_random_uuid(),
                            statement_timestamp()-interval '1 minute','pending',%s)""",
                    (durable_job, durable_content, durable_version, prior.USER_A))
            command("-m", "alembic", "upgrade", "20260926_0009")
            with psycopg.connect(conninfo(migration)) as connection:
                before_policies = connection.execute(POLICY_QUERY).fetchall()
                before_runtime_acl = connection.execute(prior.ACL_QUERY, ("nightclub_api",)).fetchall()
                before_scheduler_acl = connection.execute(prior.ACL_QUERY, ("nightclub_scheduler",)).fetchall()
                if len(before_policies) != 45:
                    raise RuntimeError("Expected exact B2 policy baseline")
            command("-m", "alembic", "upgrade", REVISION)
            with psycopg.connect(conninfo(migration)) as connection:
                after = connection.execute(POLICY_QUERY).fetchall()
                if (len(after) != 58 or sum("_system_automation_" in row[2] for row in after) != 13
                        or not all(row in after for row in before_policies)):
                    raise RuntimeError("Historical or system policy matrix drift")
                if (connection.execute(prior.ACL_QUERY, ("nightclub_api",)).fetchall()
                        != before_runtime_acl or connection.execute(prior.ACL_QUERY, (
                        "nightclub_scheduler",)).fetchall() != before_scheduler_acl):
                    raise RuntimeError("Runtime or scheduler ACL drift")
                force = connection.execute("""SELECT count(*) FROM pg_class
                    WHERE relnamespace='public'::regnamespace AND relkind='r'
                      AND relname=ANY(%s) AND relrowsecurity AND relforcerowsecurity""",
                    (list(prior.APPLICATION_TABLES),)).fetchone()[0]
                if force != 22:
                    raise RuntimeError("Expected 22 ENABLE/FORCE application tables")
                if connection.execute("SELECT version_num FROM alembic_version").fetchone() != (REVISION,):
                    raise RuntimeError("Unexpected B4 Alembic head")
                print("ALEMBIC_HEAD: 20260928_0010; POLICIES: 58; SYSTEM_POLICIES: 13; FORCE: 22", flush=True)
            command("-m", "pytest", "backend/tests/test_publish_due_coordinator_postgres.py",
                    "--prompt11-coordinator-postgres", "-q", "-ra", "--tb=short")
            print("B4 POSTGRESQL CERTIFICATION: PASS", flush=True)
        finally:
            sessions = control.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=%s",
                                       (DATABASE,)).fetchone()[0]
            if sessions:
                raise RuntimeError("Cleanup blocked by sessions; none terminated")
            control.execute("DROP DATABASE nightclub_ai_prompt11_coordinator_test")
            remaining = control.execute("SELECT 1 FROM pg_database WHERE datname=%s",
                                        (DATABASE,)).fetchall()
            print("TEST_DATABASE_REMAINING:", remaining, flush=True)
            if remaining:
                raise RuntimeError("Disposable database cleanup failed")
            if any(control.execute(ROLE_QUERY, (name,)).fetchone() != expected
                   for name, expected in roles.items()):
                raise RuntimeError("A retained role changed")
            if prior.role_memberships(control) != memberships:
                raise RuntimeError("Retained role memberships changed")


if __name__ == "__main__":
    try:
        run()
    except Exception:
        print("B4 VALIDATION FAILED OR BLOCKED; inspect safe output. No credentials printed.", flush=True)
        sys.exit(1)
