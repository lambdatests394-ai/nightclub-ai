"""Tenant/creator-scoped AI persistence with no transaction ownership."""
from datetime import date

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from backend.app.modules.ai.models import AIDailyUsage, AIGenerationRequest
from backend.app.modules.content.models import ContentItem, ContentVersion


class AIRepository:
    def __init__(self, session, organization_id, actor_id):
        self.session = session
        self.organization_id, self.actor_id = organization_id, actor_id

    async def content_for_update(self, content_id):
        return await self.session.scalar(select(ContentItem).where(
            ContentItem.id == content_id,
            ContentItem.organization_id == self.organization_id,
        ).with_for_update())

    async def daily_spend(self, usage_date: date):
        value = await self.session.scalar(select(AIDailyUsage.estimated_cost_usd).where(
            AIDailyUsage.organization_id == self.organization_id,
            AIDailyUsage.usage_date == usage_date,
        ))
        return value or 0

    async def create_generation(self, generation):
        await self.session.execute(insert(AIGenerationRequest.__table__).inline().values(
            id=generation.id, organization_id=generation.organization_id,
            content_item_id=generation.content_item_id, provider=generation.provider,
            model=generation.model, prompt_template_key=generation.prompt_template_key,
            prompt_template_version=generation.prompt_template_version,
            input_redacted=generation.input_redacted,
            provider_request_id=None, input_tokens=None, output_tokens=None,
            estimated_cost_usd=None, status="queued", error_code=None,
            created_by=generation.created_by,
        ))

    def _generation_query(self, generation_id, *, lock=False):
        query = select(AIGenerationRequest).where(
            AIGenerationRequest.id == generation_id,
            AIGenerationRequest.organization_id == self.organization_id,
            AIGenerationRequest.created_by == self.actor_id,
        )
        return query.with_for_update() if lock else query

    async def generation(self, generation_id, *, lock=False):
        return await self.session.scalar(self._generation_query(generation_id, lock=lock))

    async def mark_running(self, generation):
        result = await self.session.execute(update(AIGenerationRequest).where(
            AIGenerationRequest.id == generation.id,
            AIGenerationRequest.organization_id == self.organization_id,
            AIGenerationRequest.created_by == self.actor_id,
            AIGenerationRequest.status == "queued",
        ).values(status="running"))
        return result.rowcount == 1

    async def finish_success(self, generation, result, usage_date: date):
        ledger = pg_insert(AIDailyUsage).values(
            organization_id=self.organization_id, usage_date=usage_date,
            estimated_cost_usd=result.estimated_cost_usd,
        )
        await self.session.execute(ledger.on_conflict_do_update(
            index_elements=[AIDailyUsage.organization_id, AIDailyUsage.usage_date],
            set_={
                "estimated_cost_usd": AIDailyUsage.estimated_cost_usd + ledger.excluded.estimated_cost_usd,
                "updated_at": func.now(),
            },
        ))
        output = result.output.model_dump(mode="json", by_alias=True)
        changed = await self.session.execute(update(AIGenerationRequest).where(
            AIGenerationRequest.id == generation.id,
            AIGenerationRequest.organization_id == self.organization_id,
            AIGenerationRequest.created_by == self.actor_id,
            AIGenerationRequest.status == "running",
        ).values(status="succeeded", output=output,
                 provider_request_id=result.provider_request_id,
                 input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                 estimated_cost_usd=result.estimated_cost_usd, error_code=None))
        if changed.rowcount != 1:
            raise RuntimeError("Generation terminal transition lost its lock")

    async def finish_failure(self, generation, error_code):
        changed = await self.session.execute(update(AIGenerationRequest).where(
            AIGenerationRequest.id == generation.id,
            AIGenerationRequest.organization_id == self.organization_id,
            AIGenerationRequest.created_by == self.actor_id,
            AIGenerationRequest.status == "running",
        ).values(status="failed", provider_request_id=None,
                 input_tokens=None, output_tokens=None, estimated_cost_usd=None,
                 error_code=error_code))
        if changed.rowcount != 1:
            raise RuntimeError("Generation terminal transition lost its lock")

    async def was_applied(self, generation_id):
        return await self.session.scalar(select(ContentVersion.id).where(
            ContentVersion.ai_generation_id == generation_id,
        ).limit(1)) is not None
