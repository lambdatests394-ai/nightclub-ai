"""Strict public and provider-neutral contracts for Prompt 9."""
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from backend.app.shared.schemas import ApiSchema


class FacebookEventBrief(ApiSchema):
    model_config = ConfigDict(extra="forbid")
    event_name: str = Field(min_length=1, max_length=120)
    tone: str = Field(min_length=1, max_length=80)
    language: str = Field(min_length=1, max_length=32)
    details: str | None = Field(default=None, max_length=1500)
    call_to_action: str | None = Field(default=None, max_length=240)

    @field_validator("event_name", "tone", "language", "details", "call_to_action", mode="before")
    @classmethod
    def trimmed_nonblank(cls, value) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("Text must be a string")
        value = value.strip()
        if not value:
            raise ValueError("Text must not be blank")
        return value


class AIGenerationCreate(ApiSchema):
    model_config = ConfigDict(extra="forbid")
    content_id: UUID
    provider: str | None = None
    template_key: str
    brief: FacebookEventBrief

    @field_validator("provider", "template_key", mode="before")
    @classmethod
    def exact_nonblank(cls, value) -> str | None:
        if value is not None and (not value or value != value.strip()):
            raise ValueError("Value must be exact nonblank text")
        return value


class AIGenerationApply(ApiSchema):
    model_config = ConfigDict(extra="forbid")
    variant_index: int = Field(strict=True, ge=0, le=2)


class GeneratedVariant(ApiSchema):
    model_config = ConfigDict(extra="forbid")
    body: str

    @field_validator("body")
    @classmethod
    def valid_body(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 2200:
            raise ValueError("Generated body is invalid")
        return value


class ProviderOutput(ApiSchema):
    model_config = ConfigDict(extra="forbid")
    variants: list[GeneratedVariant] = Field(min_length=3, max_length=3)
    safety_flags: list[str] = Field(max_length=20)

    @field_validator("safety_flags")
    @classmethod
    def valid_flags(cls, values: list[str]) -> list[str]:
        result = []
        for value in values:
            value = value.strip()
            if not value or len(value) > 100:
                raise ValueError("Safety flag is invalid")
            result.append(value)
        return result

    @model_validator(mode="after")
    def distinct_variants(self):
        normalized = {" ".join(item.body.split()).casefold() for item in self.variants}
        if len(normalized) != 3:
            raise ValueError("Generated variants must be distinct")
        return self


class GenerationUsage(ApiSchema):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class AIGenerationRead(ApiSchema):
    id: UUID
    content_id: UUID
    provider: Literal["openai", "gemini"]
    model: str
    template_key: str
    template_version: int
    status: Literal["queued", "running", "succeeded", "failed"]
    variants: list[GeneratedVariant] | None
    safety_flags: list[str] | None
    usage: GenerationUsage | None
    estimated_cost_usd: Decimal | None
    error_code: str | None
