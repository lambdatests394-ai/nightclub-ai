"""Environment-backed application configuration."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Non-secret settings required by the Phase 1 application scaffold."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "nightclub-ai"
    app_env: str = "development"
    log_level: str = "INFO"
    database_url: str | None = None
    database_migration_url: str | None = None


@lru_cache
def get_settings() -> Settings:
    """Return cached process-level settings."""

    return Settings()
