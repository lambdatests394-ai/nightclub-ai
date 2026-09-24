"""Static Prompt 9 security gates; real catalog behavior is opt-in PostgreSQL."""
import ast
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from backend.app.modules.ai.audit import AIAuditWriter
from backend.app.modules.ai.models import AIDailyUsage, AIGenerationRequest
from backend.app.modules.ai.repository import AIRepository
from backend.app.core.config import Settings

ROOT = Path(__file__).parents[2]
MIGRATION = "backend/migrations/versions/20260923_0007_ai_generation_business_access.py"


def load(relative):
    spec = spec_from_file_location("prompt9_local_gate", ROOT / relative)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migration():
    return load(MIGRATION)


def test_revision_parent_offline_and_unsafe_downgrade(migration, monkeypatch):
    assert migration.revision == "20260923_0007" and migration.down_revision == "20260922_0006"
    with pytest.raises(RuntimeError, match="Unsafe security downgrade blocked"):
        migration.downgrade()
    monkeypatch.setattr(migration.context, "is_offline_mode", lambda: True)
    with pytest.raises(RuntimeError, match="online"):
        migration.upgrade()


def test_exact_six_policies_and_amended_counts(migration):
    assert len(migration.historical_policies()) == 24
    assert set(migration.POLICIES) == {
        "ai_generation_business_select", "ai_generation_business_insert",
        "ai_generation_business_update", "content_versions_ai_insert",
        "audit_ai_insert", "ai_daily_usage_business_all",
    }
    assert len(migration.historical_policies()) + len(migration.POLICIES) == 30
    assert len(migration.TABLES) == 21 and migration.TABLES[-1] == "ai_daily_usage"


def test_creator_select_and_ledger_tenant_all_are_not_widened(migration):
    table, command, using, check = migration.POLICIES["ai_generation_business_select"]
    assert (table, command, check) == ("ai_generation_requests", "SELECT", None)
    assert "app.organization_id" in using and "created_by" in using and "app.user_id" in using
    table, command, using, check = migration.POLICIES["ai_daily_usage_business_all"]
    assert (table, command) == ("ai_daily_usage", "ALL") and using == migration.TENANT and check == migration.TENANT


def test_generation_insert_policy_requires_sql_null_output(migration):
    table, command, using, check = migration.POLICIES["ai_generation_business_insert"]
    assert (table, command, using) == ("ai_generation_requests", "INSERT", None)
    assert "output IS NULL" in check
    assert "output = 'null'::jsonb" not in check


def test_ai_content_version_policy_uses_catalog_canonical_variant_column(migration):
    _, _, _, check = migration.POLICIES["content_versions_ai_insert"]
    assert "variant.value->>'body'" in check
    assert "SELECT variant->>'body'" not in check
    assert "jsonb_array_elements(g.output->'variants') variant(value)" in check


def test_historical_policy_catalog_snapshot_is_exact_and_immutable(migration):
    historical = tuple(
        (f"table_{index:02}", f"historical_{index:02}", (migration.ROLE,),
         "SELECT", "PERMISSIVE", f"qual_{index:02}", None)
        for index in range(24)
    )
    prompt9 = tuple(
        (table, name, (migration.ROLE,), command, "PERMISSIVE", using, check)
        for name, (table, command, using, check) in migration.POLICIES.items()
    )
    bind = Mock()
    bind.execute.return_value.all.return_value = [*historical, *prompt9]

    migration.verify_policies(bind, historical)

    changed = list(historical)
    changed[0] = (*changed[0][:5], "widened", changed[0][6])
    bind.execute.return_value.all.return_value = [*changed, *prompt9]
    with pytest.raises(RuntimeError, match="Historical policy catalog drift"):
        migration.verify_policies(bind, historical)


def test_prompt9_normalization_does_not_reinterpret_historical_sql(migration):
    assert migration.normalized("kind = 'image'::asset_kind") != migration.normalized("kind = 'image'")
    assert migration.normalized("size BETWEEN 1 AND 10") != migration.normalized("size >= 1 AND size <= 10")
    assert migration.normalized("CASE kind WHEN 'image' THEN 1 END") != migration.normalized(
        "CASE kind WHEN 'image' THEN 1 ELSE NULL END"
    )


def test_narrow_column_grants_and_no_delete(migration):
    assert migration.UPDATE_COLUMNS["ai_daily_usage"] == {"estimated_cost_usd", "updated_at"}
    assert not {"organization_id", "usage_date", "created_at"} & migration.UPDATE_COLUMNS["ai_daily_usage"]
    assert migration.INSERT_COLUMNS["content_versions"] == {"ai_generation_id"}
    assert "created_by" not in migration.UPDATE_COLUMNS["ai_generation_requests"]
    source = (ROOT / MIGRATION).read_text(encoding="utf-8")
    for forbidden in ("CREATE ROLE", "ALTER ROLE", "DROP ROLE", "GRANT DELETE"):
        assert forbidden not in source.upper()
    assert "LANGUAGE plpgsql SECURITY DEFINER" not in source
    assert "rolbypassrls" in source  # inspected as a stop condition, never changed


def test_transition_monotonic_unique_and_ai_body_closure_present(migration):
    source = (ROOT / MIGRATION).read_text(encoding="utf-8")
    for fragment in (
        "OLD.status = 'queued' AND NEW.status = 'running'",
        "OLD.status = 'running' AND NEW.status IN ('succeeded','failed')",
        "NEW.estimated_cost_usd < OLD.estimated_cost_usd",
        "uq_content_versions_ai_generation_once",
        "jsonb_array_elements(g.output->'variants')",
        "content_versions.version_no = ci.current_version_no + 1",
    ):
        assert fragment in source


def test_baseline_drift_stops_before_ddl(migration, monkeypatch):
    bind = Mock()
    bind.scalar.return_value = "wrong"
    ddl, create_table = Mock(), Mock()
    monkeypatch.setattr(migration.context, "is_offline_mode", lambda: False)
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(migration.op, "execute", ddl)
    monkeypatch.setattr(migration.op, "create_table", create_table)
    with pytest.raises(RuntimeError, match="exact 0006"):
        migration.upgrade()
    ddl.assert_not_called()
    create_table.assert_not_called()


def test_orm_ledger_and_generation_types_align():
    assert AIDailyUsage.__table__.primary_key.columns.keys() == ["organization_id", "usage_date"]
    assert str(AIDailyUsage.__table__.c.estimated_cost_usd.type) == "NUMERIC(12, 6)"
    assert str(AIGenerationRequest.__table__.c.estimated_cost_usd.type) == "NUMERIC(12, 6)"
    assert AIGenerationRequest.__table__.c.status.type.enums == ["queued", "running", "succeeded", "failed", "cancelled"]
    assert not AIGenerationRequest.__table__.c.status.type.create_type


def test_ai_secrets_are_excluded_from_repr_and_serialization():
    settings = Settings(_env_file=None, openai_api_key="synthetic-openai-secret",
                        gemini_api_key="synthetic-gemini-secret")
    rendered, dumped = repr(settings), settings.model_dump()
    assert "synthetic-openai-secret" not in rendered and "synthetic-gemini-secret" not in rendered
    assert "openai_api_key" not in dumped and "gemini_api_key" not in dumped


@pytest.mark.anyio
async def test_initial_generation_insert_omits_jsonb_output_for_database_sql_null():
    session = AsyncMock()
    organization_id, actor_id = uuid4(), uuid4()
    generation = SimpleNamespace(
        id=uuid4(), organization_id=organization_id, content_item_id=uuid4(),
        provider="openai", model="fixture-model", prompt_template_key="facebook_event_v1",
        prompt_template_version=1, input_redacted={"brief": {"eventName": "fixture"}},
        created_by=actor_id,
    )

    await AIRepository(session, organization_id, actor_id).create_generation(generation)

    statement = session.execute.call_args.args[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    assert "output" not in compiled.params
    assert compiled.params["status"] == "queued"
    for field in (
        "provider_request_id", "input_tokens", "output_tokens",
        "estimated_cost_usd", "error_code",
    ):
        assert compiled.params[field] is None
    output_column = AIGenerationRequest.__table__.c.output
    assert output_column.nullable and output_column.server_default is None


@pytest.mark.anyio
async def test_failed_generation_update_preserves_existing_database_sql_null_output():
    session = AsyncMock()
    session.execute.return_value.rowcount = 1
    organization_id, actor_id = uuid4(), uuid4()
    generation = SimpleNamespace(id=uuid4())

    await AIRepository(session, organization_id, actor_id).finish_failure(
        generation, "AI_PROVIDER_TIMEOUT"
    )

    statement = session.execute.call_args.args[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    assert "output" not in compiled.params
    assert compiled.params["status"] == "failed"
    assert compiled.params["error_code"] == "AI_PROVIDER_TIMEOUT"
    for field in (
        "provider_request_id", "input_tokens", "output_tokens", "estimated_cost_usd",
    ):
        assert compiled.params[field] is None
    assert "running" in compiled.params.values()


@pytest.mark.anyio
async def test_ai_audit_is_structural_and_redacted():
    session, context = AsyncMock(), Mock(organization_id=uuid4(), user_id=uuid4())
    generation = SimpleNamespace(id=uuid4(), content_item_id=uuid4(), provider="openai", model="fixture-model",
        prompt_template_key="facebook_event_v1", prompt_template_version=1,
        status="succeeded", input_tokens=1, output_tokens=2,
        estimated_cost_usd="0.001", error_code=None)
    await AIAuditWriter(session, context, uuid4()).write("ai.generation_succeeded", generation)
    statement = session.execute.call_args.args[0].compile(dialect=postgresql.dialect())
    metadata = statement.params["after"]
    assert set(metadata) <= {"generationId", "contentId", "provider", "model", "templateKey",
                             "templateVersion", "status", "inputTokens", "outputTokens",
                             "estimatedCostUsd", "errorCode", "variantIndex"}
    for forbidden in ("brief", "body", "eventName", "details", "callToAction", "apiKey", "headers"):
        assert forbidden.casefold() not in str(metadata).casefold()


def test_no_service_commit_and_historical_files_not_touched_by_import():
    for filename in ("repository.py", "service.py", "coordinator.py"):
        tree = ast.parse((ROOT / "backend/app/modules/ai" / filename).read_text(encoding="utf-8"))
        assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                       and node.func.attr == "commit" for node in ast.walk(tree))


def test_harness_unset_environment_stops_without_connection(monkeypatch):
    harness = load("scripts/local-dev/validate_prompt9.py")
    monkeypatch.delenv("DATABASE_MIGRATION_URL", raising=False)
    connect = Mock(side_effect=AssertionError("No database access permitted"))
    monkeypatch.setattr(harness.psycopg, "connect", connect)
    with pytest.raises(RuntimeError, match="withheld"):
        harness.run()
    connect.assert_not_called()
    assert harness.DATABASE == "nightclub_ai_prompt9_test" and harness.REVISION == "20260923_0007"
