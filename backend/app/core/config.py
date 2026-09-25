"""Environment-backed application configuration."""

from functools import lru_cache
from decimal import Decimal
import re
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings; the server storage credential is excluded from serialization."""

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
    database_runtime_expected_role: str = Field(default="nightclub_api", pattern=r"^[a-z_][a-z0-9_]{0,62}$")
    supabase_jwt_issuer: str = ""
    supabase_jwt_audience: str = ""
    supabase_jwks_url: str = ""
    supabase_jwt_allowed_algorithms: list[str] = Field(default_factory=lambda: ["ES256"])
    jwt_clock_skew_seconds: int = Field(default=60, ge=0, le=300)
    supabase_jwks_cache_ttl_seconds: int = Field(default=300, gt=0, le=600)
    supabase_url: str = ""
    supabase_service_role_key: SecretStr = Field(default=SecretStr(""), repr=False, exclude=True)
    storage_bucket: Literal["nightclub-assets"] = "nightclub-assets"
    storage_signed_download_ttl_seconds: int = Field(default=60, ge=1, le=60)
    asset_max_image_bytes: int = Field(default=10_485_760, ge=1, le=10_485_760)
    asset_verification_timeout_seconds: int = Field(default=15, ge=1, le=15)
    ai_default_provider: Literal["openai", "gemini"] = "openai"
    openai_api_key: SecretStr = Field(default=SecretStr(""), repr=False, exclude=True)
    openai_model: str = ""
    gemini_api_key: SecretStr = Field(default=SecretStr(""), repr=False, exclude=True)
    gemini_model: str = ""
    ai_max_output_tokens: int = Field(default=2048, ge=1, le=8192)
    ai_daily_budget_usd: Decimal = Field(default=Decimal("100.00"), gt=0)
    ai_provider_timeout_seconds: int = Field(default=30, ge=1, le=60)
    openai_input_cost_per_1m_usd: Decimal = Field(default=Decimal("0"), ge=0)
    openai_output_cost_per_1m_usd: Decimal = Field(default=Decimal("0"), ge=0)
    gemini_input_cost_per_1m_usd: Decimal = Field(default=Decimal("0"), ge=0)
    gemini_output_cost_per_1m_usd: Decimal = Field(default=Decimal("0"), ge=0)
    meta_app_id: str = ""
    meta_app_secret: SecretStr = Field(default=SecretStr(""), repr=False, exclude=True)
    meta_oauth_redirect_uri: str = ""
    meta_graph_api_version: str = ""
    meta_credential_encryption_key: SecretStr = Field(default=SecretStr(""), repr=False, exclude=True)
    meta_credential_key_version: int = Field(default=1, ge=1)
    meta_oauth_state_key: SecretStr = Field(default=SecretStr(""), repr=False, exclude=True)

    @field_validator("supabase_url")
    @classmethod
    def storage_origin(cls, value: str) -> str:
        if not value:
            return value
        url = urlsplit(value)
        if (url.scheme != "https" or not url.hostname or url.username is not None
                or url.password is not None or url.query or url.fragment
                or url.path not in {"", "/"} or url.port not in {None, 443}
                or any(ch.isspace() or ord(ch) < 32 for ch in value)):
            raise ValueError("Storage requires an HTTPS origin without credentials or URL parameters")
        return value.rstrip("/")

    @field_validator("openai_model", "gemini_model")
    @classmethod
    def model_name_has_no_surrounding_whitespace(cls, value: str) -> str:
        if value and (value != value.strip() or any(ord(ch) < 32 for ch in value)):
            raise ValueError("AI model names must be exact printable values")
        return value

    @field_validator("meta_app_id")
    @classmethod
    def meta_app_identifier(cls, value: str) -> str:
        if value and (not value.isascii() or not value.isdigit() or value.startswith("0")):
            raise ValueError("Meta App ID must be an exact decimal identifier")
        return value

    @field_validator("meta_graph_api_version")
    @classmethod
    def meta_graph_version(cls, value: str) -> str:
        if value and re.fullmatch(r"v[1-9][0-9]*\.[0-9]+", value) is None:
            raise ValueError("Meta Graph API version must use the vN.N form")
        return value

    @field_validator("meta_oauth_redirect_uri")
    @classmethod
    def meta_redirect_url(cls, value: str) -> str:
        if not value:
            return value
        url = urlsplit(value)
        if (url.scheme != "https" or not url.hostname or url.username is not None
                or url.password is not None or url.fragment
                or any(ch.isspace() or ord(ch) < 32 for ch in value)):
            raise ValueError("Meta OAuth redirect must be an HTTPS URL without credentials or fragment")
        return value

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
