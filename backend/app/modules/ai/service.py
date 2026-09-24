"""AI generation/application business rules within one caller-owned transaction."""
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import ValidationError

from backend.app.modules.ai.errors import (
    AIBudgetExceeded, AIContentNotFound, AIContentStateConflict,
    AIGenerationAlreadyApplied, AIGenerationNotFound, AIGenerationNotReady,
    AIProviderNotConfigured, AIProviderNotSupported, AITemplateNotSupported,
    AIVariantInvalid,
)
from backend.app.modules.ai.models import AIGenerationRequest
from backend.app.modules.ai.prompt_registry import PROMPT_REGISTRY
from backend.app.modules.ai.provider import GenerationPrompt, ProviderOutput, TERMINAL_ERROR_CODES
from backend.app.modules.ai.schemas import AIGenerationRead
from backend.app.modules.content.audit import ContentAuditWriter
from backend.app.modules.content.repository import ContentRepository
from backend.app.modules.content.service import ContentService
from backend.app.modules.content.state_machine import ContentState, InvalidContentTransition
from backend.app.modules.content.workflow_service import ContentWorkflowService
from backend.app.modules.identity.errors import IdentityUnavailable
from backend.app.modules.identity.policy import Permission, require_permission
from backend.app.platform.enums import ContentStatus
from backend.app.shared.idempotency import fingerprint

AI_ALLOWED_STATES = {ContentStatus.DRAFT, ContentStatus.CHANGES_REQUESTED}


@dataclass(frozen=True)
class Acceptance:
    status: int
    body: dict
    generation_id: UUID


@dataclass(frozen=True)
class ExecutionClaim:
    generation_id: UUID
    provider: str
    prompt: GenerationPrompt


class AIService:
    def __init__(self, context, repository, content_repository, idempotency, audit,
                 content_audit: ContentAuditWriter, settings):
        self.context, self.repository = context, repository
        self.content_repository, self.idempotency = content_repository, idempotency
        self.audit, self.content_audit, self.settings = audit, content_audit, settings

    def _provider_model(self, provider: str) -> str:
        if provider == "openai":
            configured = self.settings.openai_api_key.get_secret_value()
            model = self.settings.openai_model
        elif provider == "gemini":
            configured = self.settings.gemini_api_key.get_secret_value()
            model = self.settings.gemini_model
        else:
            raise AIProviderNotSupported()
        if not configured or not model:
            raise AIProviderNotConfigured()
        return model

    async def accept(self, key: UUID, request) -> Acceptance:
        require_permission(self.context, Permission.AI_GENERATE)
        provider = request.provider or self.settings.ai_default_provider
        if provider not in {"openai", "gemini"}:
            raise AIProviderNotSupported()
        template = PROMPT_REGISTRY.get(request.template_key)
        if template is None:
            raise AITemplateNotSupported()
        brief = request.brief.model_dump(mode="json", by_alias=True, exclude_none=True)
        operation = "ai:generate"
        semantic = {
            "organizationId": str(self.context.organization_id),
            "actorId": str(self.context.user_id), "contentId": str(request.content_id),
            "provider": provider, "templateKey": template.key, "brief": brief,
        }
        replay = await self.idempotency.claim(key, operation, fingerprint(operation, semantic))
        if replay is not None:
            status, body = replay
            return Acceptance(status, body, UUID(body["generationId"]))
        model = self._provider_model(provider)
        content = await self.repository.content_for_update(request.content_id)
        if content is None:
            raise AIContentNotFound()
        if content.status not in AI_ALLOWED_STATES:
            raise AIContentStateConflict()
        spend = Decimal(await self.repository.daily_spend(datetime.now(UTC).date()))
        if spend >= self.settings.ai_daily_budget_usd:
            raise AIBudgetExceeded()
        generation = AIGenerationRequest(
            id=uuid4(), organization_id=self.context.organization_id,
            content_item_id=content.id, provider=provider, model=model,
            prompt_template_key=template.key, prompt_template_version=template.version,
            input_redacted={"brief": brief}, output=None, status="queued",
            created_by=self.context.user_id,
        )
        await self.repository.create_generation(generation)
        await self.audit.write("ai.generation_requested", generation)
        body = {"generationId": str(generation.id), "status": "queued"}
        await self.idempotency.complete(key, 201, body)
        return Acceptance(201, body, generation.id)

    async def claim_execution(self, generation_id: UUID) -> ExecutionClaim | None:
        generation = await self.repository.generation(generation_id, lock=True)
        if generation is None or generation.status != "queued":
            return None
        if not await self.repository.mark_running(generation):
            return None
        template = PROMPT_REGISTRY.get(generation.prompt_template_key)
        if template is None or template.version != generation.prompt_template_version:
            raise IdentityUnavailable()
        brief = generation.input_redacted.get("brief") if isinstance(generation.input_redacted, dict) else None
        if not isinstance(brief, dict):
            raise IdentityUnavailable()
        return ExecutionClaim(generation.id, generation.provider, GenerationPrompt(
            model=generation.model, instructions=template.instructions,
            user_data={"brief": brief}, max_output_tokens=self.settings.ai_max_output_tokens,
        ))

    async def succeed(self, generation_id, result):
        generation = await self.repository.generation(generation_id, lock=True)
        if generation is None or generation.status != "running":
            return False
        await self.repository.finish_success(generation, result, datetime.now(UTC).date())
        generation.status = "succeeded"
        await self.audit.write("ai.generation_succeeded", generation,
            input_tokens=result.input_tokens, output_tokens=result.output_tokens,
            estimated_cost_usd=result.estimated_cost_usd)
        return True

    async def fail(self, generation_id, error_code):
        if error_code not in TERMINAL_ERROR_CODES:
            error_code = "AI_PROVIDER_PROTOCOL_ERROR"
        generation = await self.repository.generation(generation_id, lock=True)
        if generation is None or generation.status != "running":
            return False
        await self.repository.finish_failure(generation, error_code)
        generation.status, generation.error_code = "failed", error_code
        await self.audit.write("ai.generation_failed", generation, error_code=error_code)
        return True

    async def read(self, generation_id):
        require_permission(self.context, Permission.AI_GENERATE)
        generation = await self.repository.generation(generation_id)
        if generation is None:
            raise AIGenerationNotFound()
        try:
            output = ProviderOutput.model_validate(generation.output) if generation.status == "succeeded" else None
        except ValidationError:
            raise IdentityUnavailable() from None
        usage = None if generation.status != "succeeded" else {
            "inputTokens": generation.input_tokens, "outputTokens": generation.output_tokens,
        }
        return AIGenerationRead(
            id=generation.id, content_id=generation.content_item_id,
            provider=generation.provider, model=generation.model,
            template_key=generation.prompt_template_key,
            template_version=generation.prompt_template_version, status=generation.status,
            variants=output.variants if output else None,
            safety_flags=output.safety_flags if output else None, usage=usage,
            estimated_cost_usd=generation.estimated_cost_usd if output else None,
            error_code=generation.error_code if generation.status == "failed" else None,
        ).model_dump(mode="json", by_alias=True)

    async def apply(self, generation_id: UUID, key: UUID, variant_index: int):
        require_permission(self.context, Permission.AI_GENERATE)
        if isinstance(variant_index, bool) or not isinstance(variant_index, int) or not 0 <= variant_index <= 2:
            raise AIVariantInvalid()
        operation = f"ai:apply:{generation_id}"
        semantic = {"organizationId": str(self.context.organization_id),
                    "actorId": str(self.context.user_id),
                    "generationId": str(generation_id), "variantIndex": variant_index}
        replay = await self.idempotency.claim(key, operation, fingerprint(operation, semantic))
        if replay is not None:
            return replay
        generation = await self.repository.generation(generation_id, lock=True)
        if generation is None:
            raise AIGenerationNotFound()
        if generation.status != "succeeded":
            raise AIGenerationNotReady()
        if await self.repository.was_applied(generation.id):
            raise AIGenerationAlreadyApplied()
        content = await self.content_repository.get_item(generation.content_item_id)
        if content is None:
            raise AIContentNotFound()
        if content.status not in AI_ALLOWED_STATES:
            raise AIContentStateConflict()
        current = await self.content_repository.current(content.id)
        if current is None:
            raise IdentityUnavailable()
        current_version = current[1]
        assets = await self.content_repository.asset_ids(current_version.id)
        try:
            output = ProviderOutput.model_validate(generation.output)
            selected = output.variants[variant_index]
        except (IndexError, TypeError, ValueError):
            raise IdentityUnavailable() from None
        state = ContentState(content.id, content.created_by, content.status,
                             content.current_version_no, content.approved_version_no)
        try:
            version = await ContentWorkflowService(self.content_repository).edit(
                state, body=selected.body, title=current_version.title,
                link_url=current_version.link_url, source="ai",
                ai_generation_id=generation.id, created_by=self.context.user_id,
            )
        except InvalidContentTransition:
            raise AIContentStateConflict() from None
        await self.content_repository.snapshot_assets(version, assets)
        await self.audit.write("ai.generation_applied", generation, variant_index=variant_index)
        await self.content_audit.write("content.version_created", state,
            previous_status=str(content.status), changed_fields=("body",), asset_count=len(assets))
        row = await self.content_repository.current(content.id)
        result = ContentService.serialize(row, assets)
        await self.idempotency.complete(key, 200, result)
        return 200, result
