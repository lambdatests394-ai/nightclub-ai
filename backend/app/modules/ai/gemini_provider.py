"""Gemini Interactions adapter with header authentication and no provider storage."""
import json
from decimal import Decimal

import httpx

from backend.app.modules.ai.provider import (
    GenerationPrompt, GenerationResult, ProviderFailure, STRUCTURED_OUTPUT_SCHEMA,
    estimated_cost, normalize_output, normalize_request_id, token_count,
)

GEMINI_INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1/interactions"


class GeminiProvider:
    def __init__(self, client: httpx.AsyncClient, *, api_key: str, input_rate: Decimal,
                 output_rate: Decimal, timeout: int):
        self._client, self._api_key = client, api_key
        self._input_rate, self._output_rate, self._timeout = input_rate, output_rate, timeout

    async def health_check(self) -> bool:
        return bool(self._api_key)

    async def generate_structured_content(self, request: GenerationPrompt) -> GenerationResult:
        payload = {
            "model": request.model,
            "system_instruction": request.instructions,
            "input": json.dumps(request.user_data, ensure_ascii=False),
            "response_format": {"type": "text", "mime_type": "application/json",
                                "schema": STRUCTURED_OUTPUT_SCHEMA},
            "generation_config": {"max_output_tokens": request.max_output_tokens},
            "store": False, "stream": False, "background": False,
        }
        try:
            response = await self._client.post(
                GEMINI_INTERACTIONS_URL, json=payload,
                headers={"x-goog-api-key": self._api_key, "Content-Type": "application/json"},
                timeout=self._timeout, follow_redirects=False,
            )
        except httpx.TimeoutException:
            raise ProviderFailure("AI_PROVIDER_TIMEOUT") from None
        except httpx.RemoteProtocolError:
            raise ProviderFailure("AI_PROVIDER_PROTOCOL_ERROR") from None
        except httpx.RequestError:
            raise ProviderFailure("AI_PROVIDER_UNAVAILABLE") from None
        self._check_status(response.status_code)
        try:
            document = response.json()
        except (ValueError, TypeError):
            raise ProviderFailure("AI_PROVIDER_PROTOCOL_ERROR") from None
        if not isinstance(document, dict):
            raise ProviderFailure("AI_PROVIDER_PROTOCOL_ERROR")
        if document.get("status") in {"blocked", "safety_blocked"} or document.get("blocked") is True:
            raise ProviderFailure("AI_OUTPUT_BLOCKED")
        if document.get("status") != "completed":
            raise ProviderFailure("AI_PROVIDER_PROTOCOL_ERROR")
        texts = []
        for step in document.get("steps", []):
            if not isinstance(step, dict):
                raise ProviderFailure("AI_PROVIDER_PROTOCOL_ERROR")
            if step.get("blocked") is True or step.get("finish_reason") in {"SAFETY", "BLOCKED"}:
                raise ProviderFailure("AI_OUTPUT_BLOCKED")
            if step.get("type") != "model_output":
                continue
            content = step.get("content", [])
            content = content if isinstance(content, list) else [content]
            for part in content:
                if isinstance(part, dict) and part.get("type") in {"text", "output_text"} and isinstance(part.get("text"), str):
                    texts.append(part["text"])
        if len(texts) != 1:
            raise ProviderFailure("AI_PROVIDER_PROTOCOL_ERROR")
        usage = document.get("usage")
        if not isinstance(usage, dict):
            raise ProviderFailure("AI_PROVIDER_PROTOCOL_ERROR")
        input_tokens = token_count(usage.get("input_tokens", usage.get("total_input_tokens")))
        output_tokens = token_count(usage.get("output_tokens", usage.get("total_output_tokens")))
        return GenerationResult(
            output=normalize_output(texts[0]), provider_request_id=normalize_request_id(document.get("id")),
            input_tokens=input_tokens, output_tokens=output_tokens,
            estimated_cost_usd=estimated_cost(input_tokens, output_tokens, self._input_rate, self._output_rate),
        )

    @staticmethod
    def _check_status(status: int) -> None:
        if status == 408:
            raise ProviderFailure("AI_PROVIDER_TIMEOUT")
        if status in {401, 403}:
            raise ProviderFailure("AI_PROVIDER_AUTH_FAILED")
        if status == 429:
            raise ProviderFailure("AI_PROVIDER_RATE_LIMITED")
        if 500 <= status <= 599:
            raise ProviderFailure("AI_PROVIDER_UNAVAILABLE")
        if status != 200:
            raise ProviderFailure("AI_PROVIDER_PROTOCOL_ERROR")
