"""Exact test-only URL guards for disposable Prompt 10 PostgreSQL databases."""

from sqlalchemy.engine import make_url


PROMPT10_CERT_DATABASES = frozenset({
    "nightclub_ai_prompt10_b2c_cert_test",
    "nightclub_ai_prompt10_b2d_cert_test",
    "nightclub_ai_prompt10_b2e_cert_test",
})
PROMPT10_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1"})
PROMPT10_DRIVERS = frozenset({"postgresql+asyncpg", "postgresql+psycopg"})


def validated_prompt10_url(value, expected_username: str):
    """Return one exact disposable URL or reject it without exposing its value."""

    try:
        url = make_url(value or "")
    except Exception:
        raise ValueError("Unsafe Prompt 10 PostgreSQL target") from None
    if (
        url.host not in PROMPT10_LOOPBACK_HOSTS
        or url.database not in PROMPT10_CERT_DATABASES
        or url.username != expected_username
        or url.port != 5432
        or not url.password
        or url.query
        or url.drivername not in PROMPT10_DRIVERS
    ):
        raise ValueError("Unsafe Prompt 10 PostgreSQL target")
    return url


def validated_prompt10_pair(migration_value, runtime_value):
    """Require migration/runtime URLs to target the same known local database."""

    migration = validated_prompt10_url(migration_value, "alembic_test_user")
    runtime = validated_prompt10_url(runtime_value, "nightclub_api")
    if migration.host != runtime.host or migration.database != runtime.database:
        raise ValueError("Prompt 10 migration/runtime target mismatch")
    return migration, runtime
