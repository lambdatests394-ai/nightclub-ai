"""Offline guards/DDL scope and local harness contract, not PG validation."""
import ast
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

ROOT = Path(__file__).parents[2]


def load(relative):
    spec = spec_from_file_location("local_campaign_test_module", ROOT / relative)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migration():
    return load("backend/migrations/versions/20260910_0004_campaign_business_access.py")


def test_security_downgrade_and_offline_upgrade_blocked(migration, monkeypatch):
    with pytest.raises(RuntimeError, match="Unsafe security downgrade blocked"):
        migration.downgrade()
    monkeypatch.setattr(migration.context, "is_offline_mode", lambda: True)
    with pytest.raises(RuntimeError, match="online"):
        migration.upgrade()
    assert migration.down_revision == "20260909_0003"


def test_wrong_baseline_stops_before_ddl(migration, monkeypatch):
    bind, execute = Mock(), Mock()
    bind.scalar.return_value = "wrong"
    monkeypatch.setattr(migration.context, "is_offline_mode", lambda: False)
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(migration.op, "execute", execute)
    with pytest.raises(RuntimeError, match="exactly 0003"):
        migration.upgrade()
    execute.assert_not_called()


def test_snapshot_scope_no_delete_audit_read_or_identity_changes(migration):
    assert len(migration.TABLES) == 20 and len(migration.POLICIES) == 7
    assert {policy[0] for policy in migration.POLICIES.values()} == {"campaigns", "idempotency_keys", "audit_logs"}
    for table, operation, using, check in migration.POLICIES.values():
        assert operation != "DELETE"
        if operation in {"SELECT", "UPDATE"}:
            assert using and "app.organization_id" in using and "app.user_id" in using
        if operation in {"INSERT", "UPDATE"}:
            assert check and "app.organization_id" in check and "app.user_id" in check
        if table == "audit_logs":
            assert operation == "INSERT"
    assert "organization_id" not in migration.UPDATE_COLUMNS["campaigns"]
    assert "created_by" not in migration.UPDATE_COLUMNS["campaigns"]
    assert "key" not in migration.UPDATE_COLUMNS["idempotency_keys"]


@pytest.mark.parametrize("bad_name", ["DATABASE_MIGRATION_URL", "PROMPT6_RUNTIME_URL"])
def test_harness_missing_environment_stops_without_connect(monkeypatch, bad_name):
    harness = load("scripts/local-dev/validate_prompt6.py")
    monkeypatch.delenv(bad_name, raising=False)
    connection = Mock(side_effect=AssertionError("No database access permitted"))
    monkeypatch.setattr(harness.psycopg, "connect", connection)
    with pytest.raises(RuntimeError, match="withheld"):
        harness.local_url(bad_name, "nightclub_api")
    connection.assert_not_called()


def test_historical_harness_pinned_and_new_harness_isolated():
    historical = (ROOT / "scripts/local-dev/validate_prompt5.py").read_text(encoding="utf-8")
    calls = [node for node in ast.walk(ast.parse(historical)) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "command"]
    upgrades = [[arg.value for arg in call.args if isinstance(arg, ast.Constant)] for call in calls]
    assert ["-m", "alembic", "upgrade", "20260909_0003"] in upgrades
    assert ["-m", "alembic", "upgrade", "head"] not in upgrades
    harness = load("scripts/local-dev/validate_prompt6.py")
    assert harness.DATABASE == "nightclub_ai_prompt6_test"
    assert harness.REVISION == "20260910_0004"
    text = (ROOT / "scripts/local-dev/validate_prompt6.py").read_text(encoding="utf-8")
    for forbidden in ("CREATE ROLE", "ALTER ROLE", "DROP ROLE", "sql/003_rls.sql"):
        assert forbidden not in text


def test_guard_checks_column_permissions_even_without_table_update(migration):
    bind = MagicMock()
    bind.execute.return_value.scalars.return_value.all.return_value = ["id"]
    def scalar(statement, params):
        sql = str(statement)
        if "has_table_privilege" in sql:
            return params["table"] in {"public.profiles", "public.organization_members", "public.organizations"} and params["privilege"] == "SELECT"
        if "has_column_privilege" in sql:
            return True  # Unexpected INSERT on identity must be rejected.
        return False
    bind.scalar.side_effect = scalar
    with pytest.raises(RuntimeError, match="column privileges"):
        migration.check_access(bind, expanded=False)
