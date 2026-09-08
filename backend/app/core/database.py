"""Async application database engine, session factory, and FastAPI dependency."""

from collections.abc import AsyncIterator
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from backend.app.core.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for all persistence mappings."""


class TimestampMixin:
    """Uniform timestamps; PostgreSQL triggers remain authoritative for updates."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


def _async_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql+asyncpg://"):
        return database_url
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+asyncpg://", 1)
    raise ValueError("DATABASE_URL must be a PostgreSQL URL")


def create_database_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(_async_database_url(database_url), pool_pre_ping=True)


_database_url = get_settings().database_url
engine: AsyncEngine | None = create_database_engine(_database_url) if _database_url else None
SessionFactory: async_sessionmaker[AsyncSession] | None = (
    async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession) if engine else None
)


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Yield one transaction-scoped session; fail clearly when DB is unconfigured."""

    if SessionFactory is None:
        raise RuntimeError("DATABASE_URL is not configured")
    async with SessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
