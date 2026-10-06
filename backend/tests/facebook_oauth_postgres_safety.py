"""Guards and read-only observation for the isolated OAuth certification instance."""
from contextlib import asynccontextmanager
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url

DATABASE = "nightclub_ai_oauth_recert_test"
NAME = "nightclub-ai-oauth-pg16-cert"
PURPOSE = "oauth-recertification"
DIGEST = "sha256:a85daf0dbd5e79586e850e3fe4b21b796799828ad015ce2166aeb98cc24da61c"
IMAGE = "docker.io/library/postgres@" + DIGEST
HEAD = "20260928_0010"
ROOT = Path(__file__).resolve().parents[2]


def local_url(value, role):
    try:
        url = make_url(value)
        if (url.drivername not in {"postgresql+psycopg", "postgresql+asyncpg"}
                or url.host != "127.0.0.1" or url.port != 55432
                or url.database != DATABASE or url.username != role
                or not url.password or url.query):
            raise ValueError()
        return url
    except Exception:
        raise ValueError("Explicit isolated OAuth database configuration required") from None


def migration():
    path = ROOT / "backend/migrations/versions/20260928_0010_system_automation_execution_access.py"
    spec = spec_from_file_location("oauth_expected_migration", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


QUERIES = {
    "policies": """SELECT schemaname,tablename,policyname,permissive,
        ARRAY(SELECT unnest(roles)::text ORDER BY 1),cmd,qual,with_check
        FROM pg_policies WHERE schemaname='public' ORDER BY 1,2,3""",
    "objects": """SELECT n.nspname,c.relname,c.relkind::text,
        pg_get_userbyid(c.relowner),c.relrowsecurity,c.relforcerowsecurity,c.relpersistence::text
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind IN ('r','S') ORDER BY 1,2""",
    "roles": """SELECT rolname,rolcanlogin,rolinherit,rolsuper,rolcreatedb,
        rolcreaterole,rolreplication,rolbypassrls FROM pg_roles
        WHERE rolname IN ('postgres','alembic_test_user','nightclub_api','nightclub_scheduler')
        ORDER BY 1""",
    "memberships": """SELECT pg_get_userbyid(roleid),pg_get_userbyid(member),admin_option
        FROM pg_auth_members WHERE pg_get_userbyid(roleid) IN
        ('alembic_test_user','nightclub_api','nightclub_scheduler') OR pg_get_userbyid(member)
        IN ('alembic_test_user','nightclub_api','nightclub_scheduler') ORDER BY 1,2""",
    "grants": """WITH acl AS (
        SELECT 'schema' AS kind,n.nspname AS object,'' AS col,a.* FROM pg_namespace n
        CROSS JOIN LATERAL aclexplode(COALESCE(n.nspacl,acldefault('n',n.nspowner))) a
        WHERE n.nspname='public'
        UNION ALL
        SELECT c.relkind::text,c.relname,'',a.* FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,
        acldefault(CASE WHEN c.relkind='S' THEN 'S' ELSE 'r' END::\"char\",c.relowner))) a
        WHERE n.nspname='public' AND c.relkind IN ('r','S')
        UNION ALL
        SELECT 'column',c.relname,at.attname,a.* FROM pg_attribute at
        JOIN pg_class c ON c.oid=at.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        CROSS JOIN LATERAL aclexplode(COALESCE(at.attacl,acldefault('c',c.relowner))) a
        WHERE n.nspname='public' AND at.attnum>0 AND NOT at.attisdropped
        UNION ALL
        SELECT 'function',p.oid::regprocedure::text,'',a.* FROM pg_proc p
        JOIN pg_namespace n ON n.oid=p.pronamespace
        CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
        WHERE n.nspname='public')
        SELECT kind,object,col,pg_get_userbyid(grantor),
        CASE WHEN grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(grantee) END,
        privilege_type,is_grantable FROM acl ORDER BY 1,2,3,4,5,6,7""",
    "sequences": """SELECT c.relname,t.relname,a.attname,d.deptype::text
        FROM pg_depend d JOIN pg_class c ON c.oid=d.objid
        JOIN pg_class t ON t.oid=d.refobjid JOIN pg_attribute a
        ON a.attrelid=t.oid AND a.attnum=d.refobjsubid
        WHERE c.relkind='S' AND c.relnamespace='public'::regnamespace ORDER BY 1,2,3""",
    "audits": """SELECT action,actor_id,organization_id,entity_id,correlation_id,\"after\"
        FROM public.audit_logs WHERE organization_id=:org ORDER BY id""",
    "states": """SELECT id,actor_id,consumed_at FROM public.facebook_oauth_states
        WHERE organization_id=:org ORDER BY created_at,id""",
    "connections": """SELECT id,credentials_ciphertext,credential_key_version,status
        FROM public.platform_connections WHERE organization_id=:org
        AND external_account_id=:page ORDER BY id""",
    "head": "SELECT version_num FROM public.alembic_version",
    "residues": """SELECT NOT EXISTS (SELECT 1 FROM pg_trigger
        WHERE tgname='oauth_recert_fail_connection_audit' AND NOT tgisinternal),
        to_regprocedure('public.oauth_recert_fail_connection_audit_fn()') IS NULL""",
}


class Inspector:
    """No arbitrary SQL API: one fresh READ ONLY transaction per observation."""
    def __init__(self, engine):
        self.__engine = engine

    async def observe(self, name, **params):
        query = QUERIES[name]
        async with self.__engine.connect() as connection:
            try:
                await connection.execute(text(
                    "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                identity = (await connection.execute(text(
                    "SELECT current_database(),current_user,session_user,current_setting('transaction_read_only')"
                ))).one()
                assert identity == (DATABASE, "postgres", "postgres", "on")
                return [tuple(row) for row in (await connection.execute(text(query), params)).all()]
            finally:
                await connection.rollback()

    async def snapshot(self):
        return {key: await self.observe(key) for key in
                ("policies", "objects", "roles", "memberships", "grants", "sequences")}


async def runtime_posture(engine, inspector):
    async with engine.connect() as connection:
        assert (await connection.execute(text("SELECT current_database(),current_user,session_user"))).one() == (
            DATABASE, "nightclub_api", "nightclub_api")
        assert not await connection.scalar(text("SELECT has_schema_privilege(current_user,'public','CREATE')"))
    snapshot = await inspector.snapshot()
    expected = migration()
    tables = [row for row in snapshot["objects"] if row[1] in expected.TABLES and row[2] == 'r']
    assert len(tables) == len(expected.TABLES) == 22
    assert all(row[3] == "alembic_test_user" and row[4] and row[5] for row in tables)
    safe = (True, False, False, False, False, False, False)
    roles = {row[0]: row[1:] for row in snapshot["roles"]}
    assert roles["nightclub_api"] == roles["nightclub_scheduler"] == safe
    assert roles["alembic_test_user"] == (True, True, False, True, False, False, False)
    assert snapshot["memberships"] == []
    keys = {(row[1], row[2]) for row in snapshot["policies"]}
    expected_keys = set(expected.expected_baseline_identities()) | {
        (table, name) for name, (table, *_rest) in expected.POLICIES.items()}
    assert keys == expected_keys and len(keys) == 58
    assert await inspector.observe("head") == [(HEAD,)]
    return snapshot
