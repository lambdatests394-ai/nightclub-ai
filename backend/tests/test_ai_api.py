"""Prompt 9 domain/API tests use in-memory doubles and never access providers or PostgreSQL."""
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from backend.app.core.config import Settings
from backend.app.main import create_app
from backend.app.modules.ai.audit import AIAuditWriter
from backend.app.modules.ai.coordinator import AICoordinator
from backend.app.modules.ai.dependencies import get_ai_coordinator
from backend.app.modules.ai.errors import (
    AIBudgetExceeded, AIContentStateConflict, AIGenerationAlreadyApplied,
    AIGenerationNotFound, AIGenerationNotReady, AIProviderNotConfigured,
    AIProviderNotSupported, AITemplateNotSupported,
)
from backend.app.modules.ai.provider import GenerationResult, ProviderFailure
from backend.app.modules.ai.schemas import AIGenerationApply, AIGenerationCreate, ProviderOutput
from backend.app.modules.ai.service import AIService
from backend.app.modules.content.models import ContentItem, ContentVersion
from backend.app.modules.content.state_machine import ContentState
from backend.app.modules.identity.errors import Forbidden
from backend.app.modules.identity.policy import MemberRole, OrganizationContext
from backend.app.platform.enums import ContentStatus
from backend.app.shared.errors import IdempotencyConflict

pytestmark = pytest.mark.anyio


class MemoryIdempotency:
    def __init__(self):
        self.rows = {}

    async def claim(self, key, operation, digest):
        if key in self.rows:
            old = self.rows[key]
            if old[:2] != (operation, digest) or old[2] is None:
                raise IdempotencyConflict()
            return old[2]
        self.rows[key] = (operation, digest, None)

    async def complete(self, key, status, body):
        operation, digest, _ = self.rows[key]
        self.rows[key] = (operation, digest, (status, body))


class MemoryAudit:
    def __init__(self):
        self.events = []

    async def write(self, action, subject, **metadata):
        self.events.append((action, subject.id if hasattr(subject, "id") else subject.content_id, metadata))


class MemoryAIRepository:
    def __init__(self, context, content):
        self.context, self.content = context, content
        self.generations = {}
        self.spend = Decimal("0")
        self.applied = set()
        self.successes, self.failures = 0, 0

    async def content_for_update(self, content_id):
        return self.content if self.content.id == content_id else None

    async def daily_spend(self, usage_date):
        return self.spend

    async def create_generation(self, generation):
        self.generations[generation.id] = generation

    async def generation(self, generation_id, lock=False):
        return self.generations.get(generation_id)

    async def mark_running(self, generation):
        if generation.status != "queued":
            return False
        generation.status = "running"
        return True

    async def finish_success(self, generation, result, usage_date):
        self.successes += 1
        self.spend += result.estimated_cost_usd
        generation.output = result.output.model_dump(mode="json", by_alias=True)
        generation.provider_request_id = result.provider_request_id
        generation.input_tokens, generation.output_tokens = result.input_tokens, result.output_tokens
        generation.estimated_cost_usd, generation.error_code = result.estimated_cost_usd, None

    async def finish_failure(self, generation, error_code):
        self.failures += 1
        generation.output, generation.estimated_cost_usd, generation.error_code = None, None, error_code

    async def was_applied(self, generation_id):
        return generation_id in self.applied


class MemoryContentRepository:
    def __init__(self, context, item, version):
        self.context, self.item = context, item
        self.versions = {(item.id, version.version_no): version}
        self.attachments = {version.id: []}

    async def get_item(self, content_id):
        return self.item if self.item.id == content_id else None

    async def current(self, content_id):
        if self.item.id != content_id:
            return None
        return self.item, self.versions[(content_id, self.item.current_version_no)]

    async def asset_ids(self, version_id):
        return list(self.attachments.get(version_id, []))

    async def add_content_version(self, version):
        self.versions[version.content_item_id, version.version_no] = version

    async def save_content_state(self, state):
        self.item.status = state.status
        self.item.current_version_no = state.current_version_no
        self.item.approved_version_no = state.approved_version_no

    async def snapshot_assets(self, version, assets):
        self.attachments[version.id] = list(assets)


def create_request(content_id, provider=None, **changes):
    payload = {"contentId": content_id, "provider": provider, "templateKey": "facebook_event_v1",
               "brief": {"eventName": "Neon Friday", "tone": "energético", "language": "es-MX"}}
    payload.update(changes)
    return AIGenerationCreate.model_validate(payload)


@pytest.fixture
def case():
    context = OrganizationContext(uuid4(), uuid4(), MemberRole.EDITOR)
    now, content_id = datetime.now(UTC), uuid4()
    item = ContentItem(id=content_id, organization_id=context.organization_id, platform="facebook",
        status=ContentStatus.DRAFT, current_version_no=1, approved_version_no=None,
        created_by=context.user_id, created_at=now, updated_at=now)
    version = ContentVersion(id=uuid4(), content_item_id=content_id, version_no=1,
        body="Human draft", title="Human title", link_url="https://example.test/event",
        payload={}, source="manual", created_by=context.user_id, created_at=now, updated_at=now)
    ai, content = MemoryAIRepository(context, item), MemoryContentRepository(context, item, version)
    ai_audit, content_audit, idem = MemoryAudit(), MemoryAudit(), MemoryIdempotency()
    settings = Settings(_env_file=None, openai_api_key="synthetic-openai", openai_model="model-a",
                        gemini_api_key="synthetic-gemini", gemini_model="model-b",
                        ai_daily_budget_usd=Decimal("10"))
    service = AIService(context, ai, content, idem, ai_audit, content_audit, settings)
    return SimpleNamespace(context=context, item=item, version=version, ai=ai, content=content,
                           ai_audit=ai_audit, content_audit=content_audit, idem=idem,
                           settings=settings, service=service)


async def accepted(case, provider=None):
    request = create_request(case.item.id, provider)
    return await case.service.accept(uuid4(), request)


async def succeeded(case):
    acceptance = await accepted(case)
    claim = await case.service.claim_execution(acceptance.generation_id)
    result = GenerationResult(ProviderOutput.model_validate({
        "variants": [{"body": "Variant A"}, {"body": "Variant B"}, {"body": "Variant C"}],
        "safetyFlags": [],
    }), "request-fixture", 10, 20, Decimal("0.001000"))
    await case.service.succeed(claim.generation_id, result)
    return claim.generation_id


@pytest.mark.parametrize("role,allowed", [(role, role in {MemberRole.OWNER, MemberRole.MANAGER, MemberRole.EDITOR}) for role in MemberRole])
async def test_ai_rbac_matrix(case, role, allowed):
    case.service.context = OrganizationContext(case.context.organization_id, case.context.user_id, role)
    if allowed:
        assert (await accepted(case)).status == 201
    else:
        with pytest.raises(Forbidden):
            await accepted(case)


async def test_default_openai_and_explicit_gemini_are_server_configured(case):
    openai = await accepted(case)
    gemini = await case.service.accept(uuid4(), create_request(case.item.id, "gemini"))
    assert case.ai.generations[openai.generation_id].provider == "openai"
    assert case.ai.generations[openai.generation_id].model == "model-a"
    assert case.ai.generations[gemini.generation_id].provider == "gemini"
    assert case.ai.generations[gemini.generation_id].model == "model-b"


async def test_unsupported_provider_template_and_unconfigured_provider(case):
    with pytest.raises(AIProviderNotSupported):
        await case.service.accept(uuid4(), create_request(case.item.id, "other"))
    with pytest.raises(AITemplateNotSupported):
        await case.service.accept(uuid4(), create_request(case.item.id, templateKey="other"))
    case.settings.openai_api_key = case.settings.openai_api_key.__class__("")
    with pytest.raises(AIProviderNotConfigured):
        await case.service.accept(uuid4(), create_request(case.item.id))


@pytest.mark.parametrize("status", [ContentStatus.IN_REVIEW, ContentStatus.APPROVED, ContentStatus.SCHEDULED,
    ContentStatus.PUBLISHING, ContentStatus.PUBLISHED, ContentStatus.FAILED, ContentStatus.CANCELLED])
async def test_generate_rejects_all_non_ai_editable_states(case, status):
    case.item.status = status
    with pytest.raises(AIContentStateConflict):
        await accepted(case)


async def test_budget_checked_after_replay_and_only_new_acceptance_rejected(case):
    key, request = uuid4(), create_request(case.item.id)
    original = await case.service.accept(key, request)
    case.ai.spend = case.settings.ai_daily_budget_usd
    replay = await case.service.accept(key, request)
    assert replay.body == original.body and len(case.ai.generations) == 1
    with pytest.raises(AIBudgetExceeded):
        await case.service.accept(uuid4(), request)


async def test_generate_idempotency_conflict_and_single_requested_audit(case):
    key, request = uuid4(), create_request(case.item.id)
    first = await case.service.accept(key, request)
    second = await case.service.accept(key, request)
    assert first == second and len(case.ai.generations) == 1
    assert [event[0] for event in case.ai_audit.events] == ["ai.generation_requested"]
    different = create_request(case.item.id, brief={"eventName": "Other", "tone": "calm", "language": "es"})
    with pytest.raises(IdempotencyConflict):
        await case.service.accept(key, different)


async def test_claim_and_terminal_result_are_single_transition_and_single_charge(case):
    acceptance = await accepted(case)
    claim = await case.service.claim_execution(acceptance.generation_id)
    assert claim.provider == "openai" and await case.service.claim_execution(acceptance.generation_id) is None
    result = GenerationResult(ProviderOutput.model_validate({
        "variants": [{"body": "A"}, {"body": "B"}, {"body": "C"}], "safetyFlags": []}),
        "provider-id", 1, 2, Decimal("0.500000"))
    assert await case.service.succeed(claim.generation_id, result) is True
    assert await case.service.succeed(claim.generation_id, result) is False
    assert case.ai.successes == 1 and case.ai.spend == Decimal("0.500000")


async def test_failed_generation_has_controlled_error_and_zero_spend(case):
    acceptance = await accepted(case)
    await case.service.claim_execution(acceptance.generation_id)
    assert await case.service.fail(acceptance.generation_id, "AI_PROVIDER_TIMEOUT") is True
    value = await case.service.read(acceptance.generation_id)
    assert value["status"] == "failed" and value["variants"] is None
    assert value["errorCode"] == "AI_PROVIDER_TIMEOUT" and case.ai.spend == 0


@pytest.mark.parametrize("variant", [0, 1, 2])
async def test_apply_inherits_human_fields_assets_and_requires_future_review(case, variant):
    first, second = uuid4(), uuid4()
    case.content.attachments[case.version.id] = [first, second]
    generation_id = await succeeded(case)
    status, result = await case.service.apply(generation_id, uuid4(), variant)
    created = case.content.versions[case.item.id, 2]
    assert status == 200 and result["status"] == "draft"
    assert created.source == "ai" and created.ai_generation_id == generation_id
    assert created.title == case.version.title and created.link_url == case.version.link_url
    assert case.content.attachments[created.id] == [first, second]
    assert created.body == ["Variant A", "Variant B", "Variant C"][variant]
    assert case.item.current_version_no == 2 and case.item.approved_version_no is None


async def test_apply_changes_requested_returns_to_draft_and_is_single_use(case):
    case.item.status = ContentStatus.CHANGES_REQUESTED
    generation_id = await succeeded(case)
    key = uuid4()
    original = await case.service.apply(generation_id, key, 0)
    case.ai.applied.add(generation_id)
    assert await case.service.apply(generation_id, key, 0) == original
    with pytest.raises(AIGenerationAlreadyApplied):
        await case.service.apply(generation_id, uuid4(), 0)
    assert case.item.status is ContentStatus.DRAFT and len(case.content.versions) == 2


async def test_apply_not_ready_and_missing_generation_are_sanitized(case):
    with pytest.raises(AIGenerationNotFound):
        await case.service.apply(uuid4(), uuid4(), 0)
    generation_id = (await accepted(case)).generation_id
    with pytest.raises(AIGenerationNotReady):
        await case.service.apply(generation_id, uuid4(), 0)


async def test_ai_public_schemas_forbid_extra_trim_and_strict_variant():
    with pytest.raises(ValidationError):
        create_request(uuid4(), extra="forbidden")
    with pytest.raises(ValidationError):
        create_request(uuid4(), brief={"eventName": " ", "tone": "x", "language": "es"})
    with pytest.raises(ValidationError):
        AIGenerationApply.model_validate({"variantIndex": 1.0})
    assert AIGenerationApply.model_validate({"variantIndex": 2}).variant_index == 2


async def test_coordinator_commits_before_provider_and_terminal_transaction():
    events, opened, generation_id = [], False, uuid4()
    class Service:
        async def accept(self, key, request):
            events.append("accept")
            return SimpleNamespace(status=201, body={"generationId": str(generation_id), "status": "queued"}, generation_id=generation_id)
        async def claim_execution(self, value):
            events.append("claim")
            return SimpleNamespace(generation_id=value, provider="openai", prompt=object())
        async def succeed(self, value, result): events.append("success")
    class Provider:
        async def generate_structured_content(self, request):
            assert not opened
            events.append("provider")
            return object()
    @asynccontextmanager
    async def transaction():
        nonlocal opened
        opened = True
        try: yield Service()
        finally:
            opened = False
            events.append("commit")
    registry = SimpleNamespace(get=lambda name: Provider())
    await AICoordinator(transaction, registry).generate(uuid4(), object())
    assert events == ["accept", "commit", "claim", "commit", "provider", "success", "commit"]


async def test_exactly_three_ai_routes_and_http_validation_codes():
    app = create_app(Settings(_env_file=None))
    paths = [route.path for route in app.routes if route.path.startswith("/api/v1/ai/")]
    assert paths == ["/api/v1/ai/generations", "/api/v1/ai/generations/{generation_id}",
                     "/api/v1/ai/generations/{generation_id}/apply"]
    coordinator = SimpleNamespace(
        generate=lambda *args: None, read=lambda *args: None, apply=lambda *args: None,
    )
    app.dependency_overrides[get_ai_coordinator] = lambda: coordinator
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        invalid = await client.post("/api/v1/ai/generations", json={})
        invalid_variant = await client.post(f"/api/v1/ai/generations/{uuid4()}/apply",
            json={"variantIndex": 1.5}, headers={"Idempotency-Key": str(uuid4())})
    assert invalid.status_code == 422 and invalid.json()["code"] == "AI_INVALID_REQUEST"
    assert invalid_variant.status_code == 422 and invalid_variant.json()["code"] == "AI_VARIANT_INVALID"


async def test_ai_http_success_envelopes_location_and_idempotency_headers():
    generation_id, content_id, key = uuid4(), uuid4(), uuid4()
    calls = []
    class Coordinator:
        async def generate(self, supplied_key, body):
            calls.append(("generate", supplied_key, body.content_id))
            return 201, {"generationId": str(generation_id), "status": "queued"}
        async def read(self, supplied_id):
            calls.append(("read", supplied_id))
            return {"id": str(supplied_id), "status": "queued"}
        async def apply(self, supplied_id, supplied_key, variant):
            calls.append(("apply", supplied_id, supplied_key, variant))
            return 200, {"id": str(content_id), "status": "draft"}
    app = create_app(Settings(_env_file=None))
    app.dependency_overrides[get_ai_coordinator] = lambda: Coordinator()
    headers = {"Idempotency-Key": str(key)}
    body = {"contentId": str(content_id), "templateKey": "facebook_event_v1",
            "brief": {"eventName": "Fixture", "tone": "modern", "language": "es-MX"}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        created = await client.post("/api/v1/ai/generations", json=body, headers=headers)
        read = await client.get(f"/api/v1/ai/generations/{generation_id}")
        applied = await client.post(f"/api/v1/ai/generations/{generation_id}/apply",
                                    json={"variantIndex": 2}, headers=headers)
        missing = await client.post("/api/v1/ai/generations", json=body)
        duplicate = await client.post("/api/v1/ai/generations", json=body,
            headers=[("Idempotency-Key", str(key)), ("Idempotency-Key", str(uuid4()))])
    assert created.status_code == 201 and created.json()["data"]["status"] == "queued"
    assert created.headers["location"] == f"/api/v1/ai/generations/{generation_id}"
    assert read.status_code == 200 and applied.status_code == 200
    assert all(response.headers["cache-control"] == "no-store" for response in (created, read, applied))
    assert calls == [("generate", key, content_id), ("read", generation_id),
                     ("apply", generation_id, key, 2)]
    assert missing.status_code == duplicate.status_code == 422
    assert missing.json()["code"] == duplicate.json()["code"] == "AI_INVALID_REQUEST"


async def test_ai_provider_not_configured_http_has_retry_after():
    class Coordinator:
        async def generate(self, key, body):
            raise AIProviderNotConfigured()
    app = create_app(Settings(_env_file=None))
    app.dependency_overrides[get_ai_coordinator] = lambda: Coordinator()
    body = {"contentId": str(uuid4()), "templateKey": "facebook_event_v1",
            "brief": {"eventName": "Fixture", "tone": "modern", "language": "es-MX"}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        response = await client.post("/api/v1/ai/generations", json=body,
                                     headers={"Idempotency-Key": str(uuid4())})
    assert response.status_code == 503 and response.json()["code"] == "AI_PROVIDER_NOT_CONFIGURED"
    assert response.headers["Retry-After"] == "30"
