"""Provider contracts use only MockTransport; no test can reach the internet."""
import json
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from backend.app.modules.ai.coordinator import AICoordinator
from backend.app.modules.ai.gemini_provider import GEMINI_INTERACTIONS_URL, GeminiProvider
from backend.app.modules.ai.openai_provider import OPENAI_RESPONSES_URL, OpenAIProvider
from backend.app.modules.ai.provider import GenerationPrompt, ProviderFailure

pytestmark = pytest.mark.anyio

OUTPUT = {"variants": [{"body": "Uno"}, {"body": "Dos"}, {"body": "Tres"}], "safetyFlags": []}


def prompt():
    return GenerationPrompt("server-model", "server instructions", {"brief": {"eventName": "Fixture"}}, 321)


async def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), verify=True,
                             follow_redirects=False, trust_env=False)


async def test_openai_fixed_origin_schema_store_and_usage():
    seen = {}
    def handler(request):
        seen["request"] = request
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "resp_fixture", "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(OUTPUT)}]}],
            "usage": {"input_tokens": 100, "output_tokens": 50}})
    async with await client_for(handler) as client:
        result = await OpenAIProvider(client, api_key="synthetic-openai-fixture",
            input_rate=Decimal("2"), output_rate=Decimal("4"), timeout=7).generate_structured_content(prompt())
    request, body = seen["request"], seen["body"]
    assert str(request.url) == OPENAI_RESPONSES_URL and request.method == "POST"
    assert request.headers["authorization"].startswith("Bearer ")
    assert body["store"] is False and body["model"] == "server-model"
    assert body["max_output_tokens"] == 321 and body["text"]["format"]["strict"] is True
    schema = body["text"]["format"]["schema"]
    assert schema["additionalProperties"] is False and schema["properties"]["variants"]["minItems"] == 3
    assert len(result.output.variants) == 3 and result.provider_request_id == "resp_fixture"
    assert (result.input_tokens, result.output_tokens, result.estimated_cost_usd) == (100, 50, Decimal("0.000400"))
    assert "synthetic-openai-fixture" not in repr(result)


async def test_gemini_fixed_origin_header_schema_store_and_usage():
    seen = {}
    def handler(request):
        seen["request"] = request
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "interaction_fixture", "status": "completed",
            "steps": [{"type": "thought"}, {"type": "model_output",
                       "content": [{"type": "text", "text": json.dumps(OUTPUT)}]}],
            "usage": {"total_input_tokens": 120, "total_output_tokens": 80}})
    async with await client_for(handler) as client:
        result = await GeminiProvider(client, api_key="synthetic-gemini-fixture",
            input_rate=Decimal("1"), output_rate=Decimal("3"), timeout=8).generate_structured_content(prompt())
    request, body = seen["request"], seen["body"]
    assert str(request.url) == GEMINI_INTERACTIONS_URL and request.url.query == b""
    assert request.headers["x-goog-api-key"] == "synthetic-gemini-fixture"
    assert body["store"] is False and body["model"] == "server-model"
    assert body["response_format"]["type"] == "text"
    assert body["response_format"]["mime_type"] == "application/json"
    assert body["response_format"]["schema"]["additionalProperties"] is False
    assert body["generation_config"]["max_output_tokens"] == 321
    assert len(result.output.variants) == 3 and result.provider_request_id == "interaction_fixture"
    assert result.estimated_cost_usd == Decimal("0.000360")


@pytest.mark.parametrize("provider_class,base_document,content", [
    (OpenAIProvider, {"id": "x", "status": "completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
     [{"type": "message", "content": [{"type": "refusal", "refusal": "withheld"}]}]),
    (GeminiProvider, {"id": "x", "status": "blocked", "usage": {"input_tokens": 1, "output_tokens": 1}}, []),
])
async def test_blocked_outputs_are_normalized(provider_class, base_document, content):
    base_document["output" if provider_class is OpenAIProvider else "steps"] = content
    async with await client_for(lambda request: httpx.Response(200, json=base_document)) as client:
        provider = provider_class(client, api_key="synthetic", input_rate=Decimal(0),
                                  output_rate=Decimal(0), timeout=1)
        with pytest.raises(ProviderFailure) as error:
            await provider.generate_structured_content(prompt())
    assert error.value.code == "AI_OUTPUT_BLOCKED"


@pytest.mark.parametrize("provider_class,status,expected", [
    (OpenAIProvider, 401, "AI_PROVIDER_AUTH_FAILED"),
    (OpenAIProvider, 403, "AI_PROVIDER_AUTH_FAILED"),
    (OpenAIProvider, 408, "AI_PROVIDER_TIMEOUT"),
    (OpenAIProvider, 429, "AI_PROVIDER_RATE_LIMITED"),
    (OpenAIProvider, 503, "AI_PROVIDER_UNAVAILABLE"),
    (OpenAIProvider, 302, "AI_PROVIDER_PROTOCOL_ERROR"),
    (GeminiProvider, 401, "AI_PROVIDER_AUTH_FAILED"),
    (GeminiProvider, 403, "AI_PROVIDER_AUTH_FAILED"),
    (GeminiProvider, 408, "AI_PROVIDER_TIMEOUT"),
    (GeminiProvider, 429, "AI_PROVIDER_RATE_LIMITED"),
    (GeminiProvider, 500, "AI_PROVIDER_UNAVAILABLE"),
    (GeminiProvider, 307, "AI_PROVIDER_PROTOCOL_ERROR"),
])
async def test_http_failures_are_controlled(provider_class, status, expected):
    async with await client_for(lambda request: httpx.Response(status, text="private-provider-body")) as client:
        provider = provider_class(client, api_key="synthetic", input_rate=Decimal(0),
                                  output_rate=Decimal(0), timeout=1)
        with pytest.raises(ProviderFailure) as error:
            await provider.generate_structured_content(prompt())
    assert error.value.code == expected and "private-provider-body" not in str(error.value)


@pytest.mark.parametrize("provider_class", [OpenAIProvider, GeminiProvider])
async def test_timeout_is_controlled(provider_class):
    def handler(request):
        raise httpx.ReadTimeout("private timeout detail", request=request)
    async with await client_for(handler) as client:
        provider = provider_class(client, api_key="synthetic", input_rate=Decimal(0),
                                  output_rate=Decimal(0), timeout=1)
        with pytest.raises(ProviderFailure) as error:
            await provider.generate_structured_content(prompt())
    assert error.value.code == "AI_PROVIDER_TIMEOUT" and "private" not in str(error.value)


@pytest.mark.parametrize("provider_class,document", [
    (OpenAIProvider, {"id": "x", "status": "completed", "output": [], "usage": {"input_tokens": 1, "output_tokens": 1}}),
    (GeminiProvider, {"id": "x", "status": "completed", "steps": [], "usage": {"input_tokens": 1, "output_tokens": 1}}),
])
async def test_malformed_protocol_is_rejected_without_retry(provider_class, document):
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=document)
    async with await client_for(handler) as client:
        provider = provider_class(client, api_key="synthetic", input_rate=Decimal(0),
                                  output_rate=Decimal(0), timeout=1)
        with pytest.raises(ProviderFailure) as error:
            await provider.generate_structured_content(prompt())
    assert error.value.code == "AI_PROVIDER_PROTOCOL_ERROR" and calls == 1


@pytest.mark.parametrize("selected,alternative_name", [("openai", "gemini"), ("gemini", "openai")])
async def test_coordinator_never_falls_back_after_provider_failure(selected, alternative_name):
    generation_id = uuid4()
    services = []
    class Service:
        async def accept(self, key, request):
            return SimpleNamespace(status=201, body={"generationId": str(generation_id), "status": "queued"}, generation_id=generation_id)
        async def claim_execution(self, value):
            return SimpleNamespace(generation_id=value, provider=selected, prompt=prompt())
        async def fail(self, value, code):
            services.append(code)
    class Failing:
        calls = 0
        async def generate_structured_content(self, request):
            self.calls += 1
            raise ProviderFailure("AI_PROVIDER_UNAVAILABLE")
    class Alternative:
        calls = 0
        async def generate_structured_content(self, request):
            self.calls += 1
    failing, alternative = Failing(), Alternative()
    class Registry:
        def get(self, name):
            return {selected: failing, alternative_name: alternative}.get(name)
    @asynccontextmanager
    async def transaction():
        yield Service()
    status, body = await AICoordinator(transaction, Registry()).generate(uuid4(), object())
    assert status == 201 and body["status"] == "queued"
    assert failing.calls == 1 and alternative.calls == 0 and services == ["AI_PROVIDER_UNAVAILABLE"]


@pytest.mark.parametrize("provider_class,document", [
    (OpenAIProvider, {"id": "x", "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text",
            "text": '{"variants":[{"body":"same"},{"body":"same"},{"body":"other"}],"safetyFlags":[]}'}]}],
        "usage": {"input_tokens": 1, "output_tokens": 1}}),
    (GeminiProvider, {"id": "x", "status": "completed",
        "steps": [{"type": "model_output", "content": [{"type": "text",
            "text": '{"variants":[{"body":"same"},{"body":"same"},{"body":"other"}],"safetyFlags":[]}'}]}],
        "usage": {"input_tokens": 1, "output_tokens": 1}}),
])
async def test_invalid_structured_output_is_not_repaired_or_retried(provider_class, document):
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=document)
    async with await client_for(handler) as client:
        provider = provider_class(client, api_key="synthetic", input_rate=Decimal(0),
                                  output_rate=Decimal(0), timeout=1)
        with pytest.raises(ProviderFailure) as error:
            await provider.generate_structured_content(prompt())
    assert error.value.code == "AI_OUTPUT_INVALID" and calls == 1
