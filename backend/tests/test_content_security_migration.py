"""Static/guard coverage, never a substitute for the real PostgreSQL harness."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import MagicMock, Mock
import ast

import pytest

ROOT = Path(__file__).parents[2]


def load(relative):
    spec=spec_from_file_location("prompt7_local_module",ROOT/relative)
    module=module_from_spec(spec); spec.loader.exec_module(module)
    return module


@pytest.fixture
def migration():
    return load("backend/migrations/versions/20260910_0005_content_business_access.py")


def test_security_offline_downgrade_and_parent(migration,monkeypatch):
    assert migration.revision=="20260910_0005" and migration.down_revision=="20260910_0004"
    with pytest.raises(RuntimeError,match="Unsafe security downgrade blocked"):migration.downgrade()
    monkeypatch.setattr(migration.context,"is_offline_mode",lambda:True)
    with pytest.raises(RuntimeError,match="online"):migration.upgrade()


def test_bad_baseline_stops_before_ddl(migration,monkeypatch):
    bind,ddl=Mock(),Mock(); bind.scalar.return_value="wrong"
    monkeypatch.setattr(migration.context,"is_offline_mode",lambda:False)
    monkeypatch.setattr(migration.op,"get_bind",lambda:bind)
    monkeypatch.setattr(migration.op,"execute",ddl)
    with pytest.raises(RuntimeError,match="exact 0004"):migration.upgrade()
    ddl.assert_not_called()


def test_narrow_policy_column_scope(migration):
    assert len(migration.POLICIES)==8
    assert migration.CONNECTION_COLUMNS=={"id","organization_id","platform","status"}
    assert set(migration.UPDATE_COLUMNS)=={"content_items"}
    assert migration.UPDATE_COLUMNS["content_items"]=={"status","current_version_no","approved_version_no","updated_at"}
    assert "scheduled_for" not in migration.INSERT_COLUMNS["content_items"]
    assert "ai_generation_id" not in migration.INSERT_COLUMNS["content_versions"]
    for _,(table,command,using,check) in migration.POLICIES.items():
        assert command!="DELETE" and table!="campaigns"
        if table=="review_decisions":assert command=="INSERT"
        if table=="content_versions":assert command in {"SELECT","INSERT"}
        if command in {"SELECT","UPDATE"}:assert using
        if command in {"INSERT","UPDATE"}:assert check


def test_content_update_check_restricts_exact_prompt7_resulting_states(migration):
    table, command, using, check = migration.POLICIES["content_items_business_update"]
    assert (table, command, using) == ("content_items", "UPDATE", migration.TENANT)
    expected = migration.TENANT + " AND status IN ('draft', 'in_review', 'changes_requested', 'approved')"
    # Check the actual policy expression, not merely a detached allowlist constant.
    assert " ".join(check.split()) == " ".join(expected.split())
    for forbidden in ("scheduled", "publishing", "published", "failed", "cancelled"):
        assert f"'{forbidden}'" not in check


def test_catalog_normalization_preserves_different_authority(migration):
    expression="action IN ('campaign.created', 'campaign.updated', 'campaign.archived')"
    postgres="(action = ANY (ARRAY['campaign.created'::text, 'campaign.updated'::text, 'campaign.archived'::text]))"
    assert migration.normalized(expression)==migration.normalized(postgres)
    assert migration.normalized("true")!=migration.normalized(migration.TENANT)
    assert migration.normalized(expression)!=migration.normalized(postgres.replace("campaign.created","content.created"))


def test_shared_idempotency_is_neutral_and_campaign_alias_same():
    from backend.app.shared.errors import IdempotencyConflict
    from backend.app.modules.campaigns.errors import IdempotencyConflict as Legacy
    assert Legacy is IdempotencyConflict
    tree=ast.parse((ROOT/"backend/app/shared/idempotency.py").read_text(encoding="utf-8"))
    assert not any(isinstance(node,ast.ImportFrom) and "campaigns" in (node.module or "") for node in ast.walk(tree))


def test_harness_unset_environment_stops_before_connect(monkeypatch):
    harness=load("scripts/local-dev/validate_prompt7.py")
    monkeypatch.delenv("DATABASE_MIGRATION_URL",raising=False)
    connect=Mock(side_effect=AssertionError("No database authorized in unit test"))
    monkeypatch.setattr(harness.psycopg,"connect",connect)
    with pytest.raises(RuntimeError,match="withheld"):harness.run()
    connect.assert_not_called()
    assert harness.DATABASE=="nightclub_ai_prompt7_test" and harness.REVISION=="20260910_0005"


def test_historical_harnesses_pinned_and_no_role_commands():
    for number,head in ((5,"20260909_0003"),(6,"20260910_0004")):
        module=load(f"scripts/local-dev/validate_prompt{number}.py")
        assert module.REVISION==head
    source=(ROOT/"scripts/local-dev/validate_prompt7.py").read_text(encoding="utf-8")
    for forbidden in ("ALTER ROLE","CREATE ROLE","DROP ROLE","sql/003_rls.sql"):
        assert forbidden not in source
    assert '"upgrade", "20260910_0004"' in source
    assert '"downgrade", "20260910_0004"' in source
