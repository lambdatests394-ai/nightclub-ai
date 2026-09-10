"""Explicit migration credential selection, independent of runtime connections."""
from backend.app.core.config import Settings


def migration_url(settings: Settings) -> str:
    value = settings.database_migration_url
    if not value:
        raise RuntimeError("DATABASE_MIGRATION_URL is required for Alembic")
    for prefix in ("postgresql+asyncpg://", "postgresql://", "postgres://"):
        if value.startswith(prefix):
            return value.replace(prefix, "postgresql+psycopg://", 1)
    if not value.startswith("postgresql+psycopg://"):
        raise RuntimeError("Alembic requires a PostgreSQL psycopg migration URL")
    return value
