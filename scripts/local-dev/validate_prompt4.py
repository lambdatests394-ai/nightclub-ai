"""Explicit LOCAL validation harness, not a migration or application entrypoint.

Requires session-only DATABASE_MIGRATION_URL with a nonempty password.
Creates only nightclub_ai_prompt4_test; refuses a preexisting database.
Always removes the database it created. Keeps alembic_test_user intact.
"""
import os
from pathlib import Path
import subprocess
import sys

import psycopg
from psycopg.conninfo import make_conninfo
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]
DATABASE = "nightclub_ai_prompt4_test"


def run() -> None:
    url = make_url(os.environ.get("DATABASE_MIGRATION_URL", ""))
    if (url.host not in {"localhost", "127.0.0.1"} or url.database != DATABASE
            or url.username != "alembic_test_user" or not url.password or url.query
            or url.drivername != "postgresql+psycopg" or url.port != 5432):
        raise RuntimeError("Only the approved loopback test database/role/port is allowed")
    print(f"VALIDATED_HOST: {url.host}", flush=True)
    parameters = dict(host=url.host, port=url.port, user=url.username, password=url.password, connect_timeout=5)
    with psycopg.connect(make_conninfo(dbname="postgres", **parameters), autocommit=True) as admin:
        roles = admin.execute(
            "SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolcanlogin, rolreplication, rolbypassrls "
            "FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
        if roles != ("alembic_test_user", False, False, True, True, False, False):
            raise RuntimeError("Unexpected local validation-role attributes")
        print("VALIDATION_ROLE:", roles, flush=True)
        if admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DATABASE,)).fetchone():
            raise RuntimeError("Disposable database already exists; inspect before deleting")
        admin.execute("CREATE DATABASE nightclub_ai_prompt4_test OWNER alembic_test_user")
        print("CREATE DATABASE nightclub_ai_prompt4_test: OK", flush=True)
        try:
            with psycopg.connect(make_conninfo(dbname=DATABASE, **parameters)) as database:
                database.execute((ROOT / "scripts/local-dev/auth_stub.sql").read_text(encoding="utf-8"))
            print("APPLY LOCAL auth_stub.sql: OK", flush=True)
            child_env = os.environ.copy()
            child_env.pop("DATABASE_URL", None)
            for args in (["-m", "alembic", "upgrade", "head"],
                         ["-m", "pytest", "backend/tests", "--local-postgres", "-q"]):
                print("RUN:", sys.executable, *args, flush=True)
                subprocess.run([sys.executable, *args], cwd=ROOT, env=child_env, check=True)
            with psycopg.connect(make_conninfo(dbname=DATABASE, **parameters)) as database:
                print("ALEMBIC_HEAD:", database.execute("SELECT version_num FROM alembic_version").fetchone(), flush=True)
        finally:
            # No FORCE or termination of unrelated sessions. Report if cleanup is blocked.
            admin.execute("DROP DATABASE nightclub_ai_prompt4_test")
            remaining = admin.execute("SELECT datname FROM pg_database WHERE datname = %s", (DATABASE,)).fetchall()
            print("TEST_DATABASE_REMAINING:", remaining, flush=True)
            print("ROLE_RETAINED:", admin.execute(
                "SELECT rolname, rolcreatedb FROM pg_roles WHERE rolname = current_user"
            ).fetchone(), flush=True)


if __name__ == "__main__":
    try:
        run()
    finally:
        os.environ.pop("DATABASE_MIGRATION_URL", None)
        print("VALIDATION_PROCESS_MIGRATION_URL_REMOVED", flush=True)
