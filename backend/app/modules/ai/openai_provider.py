"""OpenAI Responses adapter with a fixed origin and no provider-side storage."""
import json
from decimal import Decimal

import httpx

from backend.app.modules.ai.provider import (
    GenerationPrompt, GenerationResult, ProviderFailure, STRUCTURED_OUTPUT_SCHEMA,
    estimated_cost, normalize_output, normalize_request_id, token_count,
)

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"


class OpenAIProvider:
    def __init__(self, client: httpx.AsyncClient, *, api_key: str, input_rate: Decimal,
                 output_rate: Decimal, timeout: int):
        self._client, self._api_key = client, api_key
        self._input_rate, self._output_rate, self._timeout = input_rate, output_rate, timeout

    async def health_check(self) -> bool:
        return bool(self._api_key)

    async def generate_structured_content(self, request: GenerationPrompt) -> GenerationResult:
        payload = {
            "model": request.model,
            "instructions": request.instructions,
            "input": [{"role": "user", "content": [{
                "type": "input_text", "text": json.dumps(request.user_data, ensure_ascii=False),
            }]}],
            "text": {"format": {"type": "json_schema", "name": "nightclub_content_variants",
                                  "strict": True, "schema": STRUCTURED_OUTPUT_SCHEMA}},
            "max_output_tokens": request.max_output_tokens,
            "store": False,
        }
        try:
            response = await self._client.post(
                OPENAI_RESPONSES_URL, json=payload,
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
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
        if document.get("status") in {"failed", "incomplete"}:
            details = document.get("incomplete_details") or {}
            code = "AI_OUTPUT_BLOCKED" if details.get("reason") in {"content_filter", "safety"} else "AI_PROVIDER_PROTOCOL_ERROR"
            raise ProviderFailure(code)
        texts = []
        for output in document.get("output", []):
            if not isinstance(output, dict):
                raise ProviderFailure("AI_PROVIDER_PROTOCOL_ERROR")
            for content in output.get("content", []):
                if not isinstance(content, dict):
                    raise ProviderFailure("AI_PROVIDER_PROTOCOL_ERROR")
                if content.get("type") == "refusal":
                    raise ProviderFailure("AI_OUTPUT_BLOCKED")
                if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                    texts.append(content["text"])
        if len(texts) != 1:
            raise ProviderFailure("AI_PROVIDER_PROTOCOL_ERROR")
        usage = document.get("usage")
        if not isinstance(usage, dict):
            raise ProviderFailure("AI_PROVIDER_PROTOCOL_ERROR")
        input_tokens = token_count(usage.get("input_tokens"))
        output_tokens = token_count(usage.get("output_tokens"))
        return GenerationResult(
            output=normalize_output(texts[0]),
            provider_request_id=normalize_request_id(document.get("id")),
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
