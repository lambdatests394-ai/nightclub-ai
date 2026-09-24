"""Sanitized Prompt 9 errors; provider detail never reaches HTTP or storage."""
from backend.app.modules.identity.errors import SecurityError


class AIContentNotFound(SecurityError):
    status, code, title = 404, "AI_CONTENT_NOT_FOUND", "Content not found"


class AIGenerationNotFound(SecurityError):
    status, code, title = 404, "AI_GENERATION_NOT_FOUND", "AI generation not found"


class AIContentStateConflict(SecurityError):
    status, code, title = 409, "AI_CONTENT_STATE_CONFLICT", "Content state does not permit AI generation"


class AIGenerationNotReady(SecurityError):
    status, code, title = 409, "AI_GENERATION_NOT_READY", "AI generation is not ready"


class AIGenerationAlreadyApplied(SecurityError):
    status, code, title = 409, "AI_GENERATION_ALREADY_APPLIED", "AI generation was already applied"


class AIInvalidRequest(SecurityError):
    status, code, title = 422, "AI_INVALID_REQUEST", "Invalid AI request"


class AIProviderNotSupported(SecurityError):
    status, code, title = 422, "AI_PROVIDER_NOT_SUPPORTED", "AI provider is not supported"


class AITemplateNotSupported(SecurityError):
    status, code, title = 422, "AI_TEMPLATE_NOT_SUPPORTED", "AI template is not supported"


class AIVariantInvalid(SecurityError):
    status, code, title = 422, "AI_VARIANT_INVALID", "AI variant is invalid"


class AIBudgetExceeded(SecurityError):
    status, code, title = 429, "AI_BUDGET_EXCEEDED", "AI daily budget is exhausted"


class AIProviderNotConfigured(SecurityError):
    status, code, title = 503, "AI_PROVIDER_NOT_CONFIGURED", "AI provider is not configured"
