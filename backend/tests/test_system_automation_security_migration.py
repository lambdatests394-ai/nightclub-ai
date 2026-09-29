"""Static fail-closed gates for Prompt 11 system automation migration."""

import hashlib
from importlib.util import module_from_spec, spec_from_file_location
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "backend/migrations/versions/20260928_0010_system_automation_execution_access.py"
PREVIOUS = ROOT / "backend/migrations/versions/20260926_0009_scheduler_discovery_access.py"
FROZEN_0009 = "268160d5a38d0c7d4d8acab36af06d67e0973fa8efa691de6a3dd69c3e159450"
HARNESS = ROOT / "scripts/local-dev/validate_prompt11_system_automation.py"


def load_migration():
    spec = spec_from_file_location("prompt11_system_automation_gate", MIGRATION)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_direct_harness_invocation_reaches_safe_configuration_preflight() -> None:
    environment = os.environ.copy()
    for name in (
        "DATABASE_MIGRATION_URL", "PROMPT11_RUNTIME_URL", "PROMPT11_SCHEDULER_URL",
    ):
        environment[name] = ""
    result = subprocess.run(
        [sys.executable, str(HARNESS)], cwd=ROOT, env=environment,
        capture_output=True, text=True, check=False, timeout=15,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert "ModuleNotFoundError" not in output
    assert "VALIDATION FAILED OR BLOCKED" in output


@pytest.fixture
def migration():
    return load_migration()


def baseline_catalog(migration):
    rows = (
        ("public", table, name, "PERMISSIVE", (migration.RUNTIME_ROLE,),
         "SELECT", f"raw_0009_catalog_{name}", None)
        for table, name in migration.expected_baseline_identities()
    )
    return tuple(sorted(rows, key=lambda row: (row[2], row[1])))


def system_catalog(migration):
    rows = (
        ("public", table, name, "PERMISSIVE", (migration.RUNTIME_ROLE,),
         command, using, check)
        for name, (table, command, using, check) in migration.POLICIES.items()
    )
    return tuple(sorted(rows, key=lambda row: (row[2], row[1])))


def changed_policy_catalog(migration, policy_name, field, replacement):
    rows = list(system_catalog(migration))
    index = next(index for index, row in enumerate(rows) if row[2] == policy_name)
    changed = list(rows[index])
    changed[field] = replacement(changed[field])
    rows[index] = tuple(changed)
    return tuple(rows)


def test_revision_frozen_hash_online_and_downgrade(migration, monkeypatch) -> None:
    assert migration.revision == "20260928_0010"
    assert migration.down_revision == "20260926_0009"
    assert migration.FROZEN_0009_SHA256 == FROZEN_0009
    assert hashlib.sha256(PREVIOUS.read_bytes()).hexdigest() == FROZEN_0009
    migration.verify_frozen_previous()
    with pytest.raises(RuntimeError, match="Unsafe security downgrade blocked"):
        migration.downgrade()
    monkeypatch.setattr(migration.context, "is_offline_mode", lambda: True)
    with pytest.raises(RuntimeError, match="online"):
        migration.upgrade()


def test_exact_13_policy_surface_and_no_forbidden_tables(migration) -> None:
    assert len(migration.POLICIES) == 13
    assert set(migration.POLICIES) == {
        "organizations_system_automation_select",
        "publication_jobs_system_automation_select",
        "publication_jobs_system_automation_update",
        "publication_attempts_system_automation_select",
        "publication_attempts_system_automation_insert",
        "publication_attempts_system_automation_update",
        "content_items_system_automation_select",
        "content_items_system_automation_update",
        "content_versions_system_automation_select",
        "content_assets_system_automation_select",
        "platform_connections_system_automation_select",
        "platform_connections_system_automation_update",
        "audit_logs_system_automation_insert",
    }
    assert {value[0] for value in migration.POLICIES.values()} == {
        "organizations", "publication_jobs", "publication_attempts", "content_items",
        "content_versions", "content_assets", "platform_connections", "audit_logs",
    }
    forbidden = {
        "profiles", "organization_members", "campaigns", "assets", "review_decisions",
        "ai_generation_requests", "ai_daily_usage", "webhook_events",
        "whatsapp_conversations", "whatsapp_messages", "outbox_events",
        "automation_runs", "idempotency_keys", "facebook_oauth_states",
    }
    assert not forbidden & {value[0] for value in migration.POLICIES.values()}
    assert not any(
        table == "publication_jobs" and command == "INSERT"
        for table, command, _, _ in migration.POLICIES.values()
    )


def test_every_system_policy_requires_context_user_null_and_tenant(migration) -> None:
    for table, _command, using, check in migration.POLICIES.values():
        predicate = check if using is None else using
        assert "app.execution_context" in predicate
        assert "system_automation" in predicate
        assert "app.user_id" in predicate and "IS NULL" in predicate
        assert "app.organization_id" in predicate
        assert table in predicate or table in {
            "organizations", "content_items", "platform_connections", "audit_logs",
        }
    content_update = migration.POLICIES["content_items_system_automation_update"]
    assert "platform = 'facebook'" in content_update[2]
    assert all(value in content_update[3] for value in (
        "'scheduled'", "'publishing'", "'published'", "'failed'",
    ))
    attempt_insert = migration.POLICIES["publication_attempts_system_automation_insert"][3]
    assert "outcome = 'in_progress'" in attempt_insert and "finished_at IS NULL" in attempt_insert
    audit = migration.POLICIES["audit_logs_system_automation_insert"][3]
    assert "actor_type = 'system'" in audit and "actor_id IS NULL" in audit
    assert "entity_type = 'publication'" in audit


def test_migration_emits_only_policy_ddl_and_zero_grants(migration, monkeypatch) -> None:
    """system_automation is a nightclub_api GUC context, not a SQL principal."""
    assert migration.RUNTIME_ROLE == "nightclub_api"
    assert migration.SCHEDULER_ROLE == "nightclub_scheduler"
    source = MIGRATION.read_text(encoding="utf-8").upper()
    assert "CREATE ROLE" not in source and "ALTER ROLE" not in source
    execute = Mock()
    monkeypatch.setattr(migration.op, "execute", execute)
    migration._apply_system_policies()
    statements = [call.args[0] for call in execute.call_args_list]
    assert len(statements) == 13
    assert all(statement.startswith("CREATE POLICY ") for statement in statements)
    assert not any("GRANT " in statement or "REVOKE " in statement for statement in statements)


def test_exact_45_policy_baseline_is_required(migration, monkeypatch) -> None:
    rows = baseline_catalog(migration)
    bind = Mock()
    bind.scalar.return_value = migration.down_revision
    monkeypatch.setattr(migration, "policy_catalog_snapshot", Mock(return_value=rows[:-1]))
    with pytest.raises(RuntimeError, match="exact 45-policy"):
        migration.verify_baseline(bind)


def test_incorrect_alembic_baseline_fails_before_ddl(migration, monkeypatch) -> None:
    bind = Mock()
    bind.scalar.return_value = "wrong"
    execute = Mock()
    monkeypatch.setattr(migration.context, "is_offline_mode", lambda: False)
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(migration.op, "execute", execute)
    with pytest.raises(RuntimeError, match="exact 0009"):
        migration.upgrade()
    execute.assert_not_called()


def test_historical_catalog_is_preserved_byte_for_byte(migration, monkeypatch) -> None:
    historical = baseline_catalog(migration)
    system = system_catalog(migration)
    monkeypatch.setattr(
        migration, "policy_catalog_snapshot", lambda _bind: historical + system,
    )
    migration.verify_policies(Mock(), historical)
    changed = list(historical)
    changed[0] = (*changed[0][:6], "changed raw policy", changed[0][7])
    monkeypatch.setattr(
        migration, "policy_catalog_snapshot", lambda _bind: tuple(changed) + system,
    )
    with pytest.raises(RuntimeError, match="Policy drift after 0010"):
        migration.verify_policies(Mock(), historical)


@pytest.mark.parametrize(("field", "replacement"), [
    (6, "true"),
    (4, ("wrong_role",)),
    (5, "DELETE"),
])
def test_system_policy_drift_fails(migration, monkeypatch, field, replacement) -> None:
    historical = baseline_catalog(migration)
    system = list(system_catalog(migration))
    changed = list(system[0])
    changed[field] = replacement
    system[0] = tuple(changed)
    monkeypatch.setattr(
        migration, "policy_catalog_snapshot", lambda _bind: historical + tuple(system),
    )
    with pytest.raises(RuntimeError, match="System automation policy drift"):
        migration.verify_policies(Mock(), historical)


@pytest.mark.parametrize(("policy", "field", "token"), [
    ("organizations_system_automation_select", 6, "app.execution_context"),
    ("publication_jobs_system_automation_update", 7, "app.user_id"),
    ("content_versions_system_automation_select", 6, "app.organization_id"),
    ("content_items_system_automation_update", 7, "platform = 'facebook'"),
    ("audit_logs_system_automation_insert", 7, "actor_id IS NULL"),
    ("audit_logs_system_automation_insert", 7, "actor_type = 'system'"),
])
def test_required_system_predicate_removal_is_detected(
        migration, monkeypatch, policy, field, token) -> None:
    historical = baseline_catalog(migration)
    changed = changed_policy_catalog(
        migration, policy, field, lambda value: value.replace(token, "true"),
    )
    monkeypatch.setattr(
        migration, "policy_catalog_snapshot", lambda _bind: historical + changed,
    )
    with pytest.raises(RuntimeError, match="System automation policy drift"):
        migration.verify_policies(Mock(), historical)


def test_extra_forbidden_policy_is_detected(migration, monkeypatch) -> None:
    historical = baseline_catalog(migration)
    extra = (
        "public", "assets", "assets_system_automation_select", "PERMISSIVE",
        (migration.RUNTIME_ROLE,), "SELECT", migration.SYSTEM_TENANT, None,
    )
    monkeypatch.setattr(
        migration, "policy_catalog_snapshot",
        lambda _bind: historical + system_catalog(migration) + (extra,),
    )
    with pytest.raises(RuntimeError, match="Policy drift after 0010"):
        migration.verify_policies(Mock(), historical)


def test_publication_job_insert_authority_is_detected(migration, monkeypatch) -> None:
    historical = baseline_catalog(migration)
    system = changed_policy_catalog(
        migration, "publication_jobs_system_automation_select", 5,
        lambda _value: "INSERT",
    )
    monkeypatch.setattr(
        migration, "policy_catalog_snapshot", lambda _bind: historical + system,
    )
    with pytest.raises(RuntimeError, match="System automation policy drift"):
        migration.verify_policies(Mock(), historical)


@pytest.mark.parametrize("surface", ["runtime", "scheduler"])
def test_upgrade_rejects_any_acl_change(migration, monkeypatch, surface) -> None:
    bind = Mock()
    historical = baseline_catalog(migration)
    runtime_acl = (("table", "content_items", "", "SELECT", "NO"),)
    scheduler_acl = (("column", "content_items", "id", "SELECT", "NO"),)
    monkeypatch.setattr(migration.context, "is_offline_mode", lambda: False)
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(
        migration, "verify_baseline",
        Mock(return_value=(historical, runtime_acl, scheduler_acl, (), {"oid": 42})),
    )
    monkeypatch.setattr(migration, "_apply_system_policies", Mock())
    monkeypatch.setattr(migration, "verify_policies", Mock())
    monkeypatch.setattr(migration.BASE.BASE, "verify_grants", Mock())
    monkeypatch.setattr(
        migration, "role_acl_snapshot",
        Mock(side_effect=lambda _bind, role: (
            runtime_acl + (("extra",),)
            if surface == "runtime" and role == migration.RUNTIME_ROLE
            else scheduler_acl + (("extra",),)
            if surface == "scheduler" and role == migration.SCHEDULER_ROLE
            else runtime_acl if role == migration.RUNTIME_ROLE else scheduler_acl
        )),
    )
    monkeypatch.setattr(migration.BASE, "verify_scheduler_grants", Mock())
    monkeypatch.setattr(migration.BASE, "verify_table_posture", Mock())
    with pytest.raises(RuntimeError, match=f"{surface.capitalize()} ACL drift"):
        migration.upgrade()
