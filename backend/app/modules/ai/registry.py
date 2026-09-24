"""Server-configured provider registry; never falls back between providers."""
from types import MappingProxyType

from backend.app.modules.ai.gemini_provider import GeminiProvider
from backend.app.modules.ai.openai_provider import OpenAIProvider


class ProviderRegistry:
    def __init__(self, providers=None):
        self._providers = MappingProxyType(dict(providers or {}))

    @classmethod
    def from_settings(cls, settings, client):
        providers = {}
        openai_key = settings.openai_api_key.get_secret_value()
        if openai_key and settings.openai_model:
            providers["openai"] = OpenAIProvider(
                client, api_key=openai_key, input_rate=settings.openai_input_cost_per_1m_usd,
                output_rate=settings.openai_output_cost_per_1m_usd,
                timeout=settings.ai_provider_timeout_seconds,
            )
        gemini_key = settings.gemini_api_key.get_secret_value()
        if gemini_key and settings.gemini_model:
            providers["gemini"] = GeminiProvider(
                client, api_key=gemini_key, input_rate=settings.gemini_input_cost_per_1m_usd,
                output_rate=settings.gemini_output_cost_per_1m_usd,
                timeout=settings.ai_provider_timeout_seconds,
            )
        return cls(providers)

    def get(self, name: str):
        return self._providers.get(name)
