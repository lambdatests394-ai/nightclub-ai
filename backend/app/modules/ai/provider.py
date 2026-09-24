"""Provider-neutral AI port and strict result normalization."""
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from pydantic import ValidationError

from backend.app.modules.ai.schemas import ProviderOutput

TERMINAL_ERROR_CODES = frozenset({
    "AI_PROVIDER_TIMEOUT", "AI_PROVIDER_UNAVAILABLE", "AI_PROVIDER_RATE_LIMITED",
    "AI_PROVIDER_AUTH_FAILED", "AI_PROVIDER_PROTOCOL_ERROR", "AI_OUTPUT_INVALID",
    "AI_OUTPUT_BLOCKED",
})

STRUCTURED_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "variants": {
            "type": "array", "minItems": 3, "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {"body": {"type": "string"}},
                "required": ["body"], "additionalProperties": False,
            },
        },
        "safetyFlags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["variants", "safetyFlags"],
    "additionalProperties": False,
}


class ProviderFailure(Exception):
    """Carries only an allow-listed code, never provider response or exception text."""

    def __init__(self, code: str):
        if code not in TERMINAL_ERROR_CODES:
            raise ValueError("Unsupported provider error code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class GenerationPrompt:
    model: str
    instructions: str
    user_data: dict
    max_output_tokens: int


@dataclass(frozen=True)
class GenerationResult:
    output: ProviderOutput
    provider_request_id: str
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: Decimal


class AIProvider(Protocol):
    async def generate_structured_content(self, request: GenerationPrompt) -> GenerationResult: ...
    async def health_check(self) -> bool: ...


def normalize_output(value: str | dict) -> ProviderOutput:
    try:
        document = json.loads(value) if isinstance(value, str) else value
        return ProviderOutput.model_validate(document)
    except (json.JSONDecodeError, ValidationError, TypeError):
        raise ProviderFailure("AI_OUTPUT_INVALID") from None


def normalize_request_id(value) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > 256
            or any(ord(character) < 32 for character in value)):
        raise ProviderFailure("AI_PROVIDER_PROTOCOL_ERROR")
    return value.strip()


def token_count(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProviderFailure("AI_PROVIDER_PROTOCOL_ERROR")
    return value


def estimated_cost(input_tokens: int, output_tokens: int, input_rate: Decimal,
                   output_rate: Decimal) -> Decimal:
    cost = (Decimal(input_tokens) * input_rate + Decimal(output_tokens) * output_rate) / Decimal(1_000_000)
    return cost.quantize(Decimal("0.000001"))
