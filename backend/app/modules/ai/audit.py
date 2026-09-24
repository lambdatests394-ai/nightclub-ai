"""Structural AI audit only; prompt and generated copy are deliberately absent."""
from sqlalchemy import insert

from backend.app.modules.audit.models import AuditLog


class AIAuditWriter:
    def __init__(self, session, context, correlation_id):
        self.session, self.context, self.correlation_id = session, context, correlation_id

    async def write(self, action, generation, **extra):
        metadata = {
            "generationId": str(generation.id),
            "contentId": str(generation.content_item_id),
            "provider": generation.provider,
            "model": generation.model,
            "templateKey": generation.prompt_template_key,
            "templateVersion": generation.prompt_template_version,
            "status": str(generation.status),
        }
        for source, target in (
            ("input_tokens", "inputTokens"), ("output_tokens", "outputTokens"),
            ("estimated_cost_usd", "estimatedCostUsd"), ("error_code", "errorCode"),
            ("variant_index", "variantIndex"),
        ):
            value = extra.get(source, getattr(generation, source, None))
            if value is not None:
                metadata[target] = str(value) if source == "estimated_cost_usd" else value
        await self.session.execute(insert(AuditLog.__table__).inline().values(
            organization_id=self.context.organization_id, actor_type="user",
            actor_id=str(self.context.user_id), action=action,
            entity_type="ai_generation", entity_id=generation.id,
            correlation_id=self.correlation_id, after=metadata,
        ))
