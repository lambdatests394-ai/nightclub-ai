"""Environment-backed application configuration."""

from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field, field_validator
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
    supabase_jwt_issuer: str = ""
    supabase_jwt_audience: str = ""
    supabase_jwks_url: str = ""
    supabase_jwt_allowed_algorithms: list[str] = Field(default_factory=lambda: ["ES256"])
    jwt_clock_skew_seconds: int = Field(default=60, ge=0, le=300)
    supabase_jwks_cache_ttl_seconds: int = Field(default=300, gt=0, le=600)

    @field_validator("supabase_jwt_allowed_algorithms")
    @classmethod
    def approved_algorithm_only(cls, value: list[str]) -> list[str]:
        if value != ["ES256"]:
            raise ValueError("Prompt 4 permits only ES256")
        return value

    @field_validator("supabase_jwks_url", "supabase_jwt_issuer")
    @classmethod
    def trusted_https_url(cls, value: str) -> str:
        if not value:
            return value
        url = urlsplit(value)
        if (url.scheme != "https" or not url.hostname or url.username is not None
                or url.password is not None or url.query or url.fragment):
            raise ValueError("Auth URLs must be HTTPS without credentials, query or fragment")
        return value

    @field_validator("supabase_jwt_audience")
    @classmethod
    def audience_has_no_surrounding_whitespace(cls, value: str) -> str:
        if value and (not value.strip() or value != value.strip()):
            raise ValueError("Audience must be an exact nonblank value")
        return value


@lru_cache
def get_settings() -> Settings:
    """Return cached process-level settings."""

    return Settings()
