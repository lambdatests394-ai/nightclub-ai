"""Static security gates for Prompt 11 scheduler discovery migration."""

import ast
import hashlib
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy.engine import URL


ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "backend/migrations/versions/20260926_0009_scheduler_discovery_access.py"
PREVIOUS = ROOT / "backend/migrations/versions/20260924_0008_facebook_publication_access.py"
FROZEN_0008 = "6a0962655398513d75cd8fd6bb4a562789d59e492f54f14e191169209e62f5dd"


def load_migration():
    spec = spec_from_file_location("prompt11_scheduler_gate", MIGRATION)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_harness():
    path = ROOT / "scripts/local-dev/validate_prompt11_scheduler.py"
    spec = spec_from_file_location("prompt11_scheduler_harness_gate", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def historical_catalog(migration):
    rows = (
        ("public", table, name, "PERMISSIVE", (migration.RUNTIME_ROLE,),
         "SELECT", f"raw_catalog_expression_{name}", None)
        for table, name in migration.expected_historical_policy_identities()
    )
    return tuple(sorted(rows, key=lambda row: (row[2], row[1])))


def scheduler_catalog(migration):
    rows = (
        ("public", table, name, "PERMISSIVE", (migration.SCHEDULER_ROLE,),
         command, using, check)
        for name, (table, command, using, check) in migration.POLICIES.items()
    )
    return tuple(sorted(rows, key=lambda row: (row[2], row[1])))


@pytest.fixture
def migration():
    return load_migration()


def test_revision_parent_frozen_hash_online_and_downgrade(migration, monkeypatch) -> None:
    assert migration.revision == "20260926_0009"
    assert migration.down_revision == "20260924_0008"
    assert migration.branch_labels is None and migration.depends_on is None
    assert migration.FROZEN_0008_SHA256 == FROZEN_0008
    assert hashlib.sha256(PREVIOUS.read_bytes()).hexdigest() == FROZEN_0008
    migration.verify_frozen_previous()
    with pytest.raises(RuntimeError, match="Unsafe security downgrade blocked"):
        migration.downgrade()
    monkeypatch.setattr(migration.context, "is_offline_mode", lambda: True)
    with pytest.raises(RuntimeError, match="online"):
        migration.upgrade()


def test_exact_scheduler_policies_and_column_surface(migration) -> None:
    assert migration.SCHEDULER_ROLE == "nightclub_scheduler"
    assert migration.SCHEDULER_SELECT_COLUMNS == {
        "publication_jobs": (
            "id", "content_item_id", "status", "scheduled_for",
            "next_attempt_at", "lease_expires_at",
        ),
        "content_items": ("id", "organization_id"),
    }
    assert set(migration.POLICIES) == {
        "publication_jobs_scheduler_due_select",
        "content_items_scheduler_due_select",
    }
    for table, command, using, check in migration.POLICIES.values():
        assert table in {"publication_jobs", "content_items"}
        assert command == "SELECT" and check is None
        assert "statement_timestamp()" in using
    assert "content_items" not in migration.JOB_DUE
    assert "FROM public.publication_jobs pj" in migration.CONTENT_DUE
    assert len(migration.expected_historical_policy_identities()) == 43


def test_only_allowlisted_scheduler_ddl_is_emitted(migration, monkeypatch) -> None:
    execute = Mock()
    monkeypatch.setattr(migration.op, "execute", execute)
    migration._apply_scheduler_access()
    statements = [call.args[0] for call in execute.call_args_list]
    assert statements == [
        "GRANT USAGE ON SCHEMA public TO nightclub_scheduler",
        "GRANT SELECT (id, content_item_id, status, scheduled_for, next_attempt_at, lease_expires_at) ON public.publication_jobs TO nightclub_scheduler",
        "GRANT SELECT (id, organization_id) ON public.content_items TO nightclub_scheduler",
        f"CREATE POLICY publication_jobs_scheduler_due_select ON public.publication_jobs FOR SELECT TO nightclub_scheduler USING ({migration.JOB_DUE})",
        f"CREATE POLICY content_items_scheduler_due_select ON public.content_items FOR SELECT TO nightclub_scheduler USING ({migration.CONTENT_DUE})",
    ]


def test_source_contains_no_role_provisioning_bypass_or_broad_grant() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    tree = ast.parse(source)
    execute_literals = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute" and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            execute_literals.append(node.args[0].value.upper())
    emitted = "\n".join(execute_literals)
    for forbidden in (
        "CREATE ROLE", "ALTER ROLE", "SECURITY DEFINER", "GRANT ALL",
        "SELECT ON ALL TABLES", "ALTER DEFAULT PRIVILEGES",
        "GRANT INSERT", "GRANT UPDATE", "GRANT DELETE",
    ):
        assert forbidden not in emitted


@pytest.mark.parametrize("defect", [
    "missing", "rolname", "rolcanlogin", "rolinherit", "rolsuper", "rolcreatedb",
    "rolcreaterole", "rolreplication", "rolbypassrls", "memberships", "schema_create",
])
def test_scheduler_role_verification_fails_closed(migration, defect) -> None:
    role = {
        "oid": 9001, "rolname": "nightclub_scheduler", "rolcanlogin": True,
        "rolinherit": False, "rolsuper": False, "rolcreatedb": False,
        "rolcreaterole": False, "rolreplication": False, "rolbypassrls": False,
    }
    if defect == "rolname":
        role[defect] = "wrong"
    elif defect not in {"missing", "memberships", "schema_create"}:
        role[defect] = not role[defect]
    bind = Mock()
    bind.execute.return_value.mappings.return_value.one_or_none.return_value = (
        None if defect == "missing" else role
    )
    bind.scalar.side_effect = [defect == "memberships", defect == "schema_create"]
    with pytest.raises(RuntimeError):
        migration._scheduler_role(bind)


def test_policy_verifier_preserves_all_historical_rows_exactly(migration, monkeypatch) -> None:
    historical = historical_catalog(migration)
    scheduler = scheduler_catalog(migration)
    monkeypatch.setattr(migration.BASE, "policy_catalog_snapshot", lambda _bind: historical + scheduler)
    migration.verify_policies(Mock(), historical)
    changed = list(historical)
    changed[0] = (*changed[0][:6], "widened", changed[0][7])
    monkeypatch.setattr(migration.BASE, "policy_catalog_snapshot", lambda _bind: tuple(changed) + scheduler)
    with pytest.raises(RuntimeError, match="Policy drift"):
        migration.verify_policies(Mock(), historical)


def test_exact_43_policy_identity_baseline_is_required(migration, monkeypatch) -> None:
    rows = historical_catalog(migration)
    monkeypatch.setattr(migration.BASE, "policy_catalog_snapshot", lambda _bind: rows)
    assert migration.verify_historical_catalog(Mock()) == rows


def test_missing_historical_policy_fails(migration, monkeypatch) -> None:
    rows = historical_catalog(migration)[:-1]
    monkeypatch.setattr(migration.BASE, "policy_catalog_snapshot", lambda _bind: rows)
    with pytest.raises(RuntimeError, match="exact 43-policy"):
        migration.verify_historical_catalog(Mock())


def test_unexpected_historical_policy_fails(migration, monkeypatch) -> None:
    rows = list(historical_catalog(migration))
    rows[-1] = ("public", "assets", "unexpected_policy", "PERMISSIVE",
                (migration.RUNTIME_ROLE,), "SELECT", "true", None)
    monkeypatch.setattr(migration.BASE, "policy_catalog_snapshot", lambda _bind: tuple(rows))
    with pytest.raises(RuntimeError, match="exact 43-policy"):
        migration.verify_historical_catalog(Mock())


def test_incorrect_historical_policy_identity_fails(migration, monkeypatch) -> None:
    rows = list(historical_catalog(migration))
    target = next(index for index, row in enumerate(rows) if row[2] == "assets_business_insert")
    rows[target] = (*rows[target][:1], "wrong_table", *rows[target][2:])
    monkeypatch.setattr(migration.BASE, "policy_catalog_snapshot", lambda _bind: tuple(rows))
    with pytest.raises(RuntimeError, match="exact 43-policy"):
        migration.verify_historical_catalog(Mock())


def test_frozen_0008_hash_mismatch_fails(migration, monkeypatch, tmp_path) -> None:
    changed = tmp_path / "changed_0008.py"
    changed.write_text("changed", encoding="utf-8")
    monkeypatch.setattr(migration, "PREVIOUS_PATH", changed)
    with pytest.raises(RuntimeError, match="hash mismatch"):
        migration.verify_frozen_previous()


@pytest.mark.parametrize(("field", "replacement"), [
    (6, "predicate drift"),
    (4, ("wrong_role",)),
    (5, "UPDATE"),
])
def test_scheduler_policy_security_drift_fails(
        migration, monkeypatch, field, replacement) -> None:
    historical = historical_catalog(migration)
    scheduler = list(scheduler_catalog(migration))
    changed = list(scheduler[0])
    changed[field] = replacement
    scheduler[0] = tuple(changed)
    monkeypatch.setattr(
        migration.BASE, "policy_catalog_snapshot",
        lambda _bind: historical + tuple(scheduler),
    )
    with pytest.raises(RuntimeError, match="Scheduler policy drift"):
        migration.verify_policies(Mock(), historical)


def test_exactly_two_scheduler_policies_are_required(migration, monkeypatch) -> None:
    historical = historical_catalog(migration)
    scheduler = scheduler_catalog(migration)
    extra = ("public", "assets", "unexpected_scheduler_policy", "PERMISSIVE",
             (migration.SCHEDULER_ROLE,), "SELECT", "true", None)
    monkeypatch.setattr(
        migration.BASE, "policy_catalog_snapshot",
        lambda _bind: historical + scheduler + (extra,),
    )
    with pytest.raises(RuntimeError, match="Policy drift after 0009"):
        migration.verify_policies(Mock(), historical)


def test_baseline_invokes_frozen_0008_policy_verifier(migration, monkeypatch) -> None:
    bind = Mock()
    bind.scalar.return_value = migration.down_revision
    frozen_verifier = Mock()
    monkeypatch.setattr(migration.BASE, "verify_policies", frozen_verifier)
    monkeypatch.setattr(migration, "verify_historical_catalog", Mock(return_value=("history",)))
    monkeypatch.setattr(migration.BASE, "_verify_active_index", Mock())
    monkeypatch.setattr(migration.BASE, "verify_grants", Mock())
    monkeypatch.setattr(migration, "_scheduler_role", Mock(return_value={"oid": 42}))
    monkeypatch.setattr(migration, "verify_table_posture", Mock())
    monkeypatch.setattr(migration, "verify_scheduler_has_no_application_access", Mock())
    monkeypatch.setattr(migration, "runtime_acl_snapshot", Mock(return_value=("acl",)))
    monkeypatch.setattr(migration, "function_execute_snapshot", Mock(return_value=("functions",)))
    migration.verify_baseline(bind)
    frozen_verifier.assert_called_once_with(bind)


@pytest.mark.parametrize("failure", [
    "Unsafe runtime role",
    "Unexpected privileged public function",
])
def test_frozen_runtime_security_failure_stops_before_scheduler_ddl(
        migration, monkeypatch, failure) -> None:
    bind = Mock()
    bind.scalar.return_value = migration.down_revision
    apply_access = Mock()
    monkeypatch.setattr(migration.context, "is_offline_mode", lambda: False)
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(migration.BASE, "verify_policies", Mock())
    monkeypatch.setattr(migration.BASE, "_verify_active_index", Mock())
    monkeypatch.setattr(
        migration.BASE, "verify_grants", Mock(side_effect=RuntimeError(failure)),
    )
    monkeypatch.setattr(migration, "_apply_scheduler_access", apply_access)
    with pytest.raises(RuntimeError, match=failure):
        migration.upgrade()
    apply_access.assert_not_called()


def test_missing_enable_or_force_rls_fails(migration, monkeypatch) -> None:
    runtime_oid = 41
    scheduler_oid = 42
    rows = [
        (table, 1000 + index, True, index != 0)
        for index, table in enumerate(migration.TABLES)
    ]
    bind = Mock()
    bind.execute.return_value.all.return_value = rows
    monkeypatch.setattr(migration.BASE, "_role_row", lambda _bind: {"oid": runtime_oid})
    with pytest.raises(RuntimeError, match="ENABLE/FORCE"):
        migration.verify_table_posture(bind, scheduler_oid)


def test_scheduler_table_grant_widening_fails(migration) -> None:
    bind = Mock()

    def scalar(statement, parameters):
        sql = str(statement)
        if "has_schema_privilege" in sql:
            return "'USAGE'" in sql
        if "has_table_privilege" in sql:
            return (parameters["table"] == "public." + migration.TABLES[0]
                    and parameters["privilege"] == "SELECT")
        return False

    bind.scalar.side_effect = scalar
    with pytest.raises(RuntimeError, match="table-level privileges forbidden"):
        migration.verify_scheduler_grants(bind, function_snapshot=())


def test_baseline_snapshot_is_captured_before_scheduler_ddl(
        migration, monkeypatch) -> None:
    bind = Mock()
    bind.scalar.return_value = migration.down_revision
    events = []
    historical = historical_catalog(migration)
    runtime_acl = (("table", "assets", "", "SELECT", False),)
    monkeypatch.setattr(migration.context, "is_offline_mode", lambda: False)
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(migration.BASE, "verify_policies", Mock())
    monkeypatch.setattr(migration.BASE, "_verify_active_index", Mock())
    monkeypatch.setattr(migration.BASE, "verify_grants", Mock())
    monkeypatch.setattr(migration, "_scheduler_role", Mock(return_value={"oid": 42}))
    monkeypatch.setattr(migration, "verify_table_posture", Mock())
    monkeypatch.setattr(migration, "verify_scheduler_has_no_application_access", Mock())
    monkeypatch.setattr(migration, "runtime_acl_snapshot", Mock(return_value=runtime_acl))
    monkeypatch.setattr(migration, "function_execute_snapshot", Mock(return_value=("functions",)))
    monkeypatch.setattr(
        migration, "verify_historical_catalog",
        Mock(side_effect=lambda _bind: events.append("snapshot") or historical),
    )
    monkeypatch.setattr(
        migration, "_apply_scheduler_access", Mock(side_effect=lambda: events.append("ddl")),
    )
    verify_policies = Mock()
    monkeypatch.setattr(migration, "verify_policies", verify_policies)
    monkeypatch.setattr(migration, "verify_scheduler_grants", Mock())
    migration.upgrade()
    assert events == ["snapshot", "ddl"]
    verify_policies.assert_called_once_with(bind, historical)


def test_runtime_acl_snapshot_uses_valid_defaults_and_keeps_explicit_runtime_grants(
        migration) -> None:
    explicit = (
        ("table", "assets", "", "SELECT", False),
        ("column", "platform_connections", "credentials_ciphertext", "SELECT", False),
    )
    bind = Mock()
    bind.execute.return_value.all.return_value = explicit
    assert migration.runtime_acl_snapshot(bind) == explicit
    statement, parameters = bind.execute.call_args.args
    sql = str(statement)
    assert "COALESCE(c.relacl,acldefault('r',c.relowner))" in sql
    assert "COALESCE(at.attacl,acldefault('c',c.relowner))" in sql
    assert "'{}'::aclitem[]" not in sql
    assert sql.count("a.grantee=(SELECT oid FROM target)") == 2
    assert parameters == {
        "role": migration.RUNTIME_ROLE,
        "tables": list(migration.TABLES),
    }


def test_failed_historical_preflight_applies_no_scheduler_ddl(migration, monkeypatch) -> None:
    bind = Mock()
    apply_access = Mock()
    monkeypatch.setattr(migration.context, "is_offline_mode", lambda: False)
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(
        migration, "verify_baseline",
        Mock(side_effect=RuntimeError("Expected exact 43-policy 0008 baseline")),
    )
    monkeypatch.setattr(migration, "_apply_scheduler_access", apply_access)
    with pytest.raises(RuntimeError, match="exact 43-policy"):
        migration.upgrade()
    apply_access.assert_not_called()


@pytest.mark.parametrize("after_acl", [
    (),
    (
        ("table", "assets", "", "SELECT", False),
        ("table", "assets", "", "UPDATE", False),
    ),
])
def test_upgrade_rejects_nightclub_api_acl_drift(
        migration, monkeypatch, after_acl) -> None:
    bind = Mock()
    before_acl = (("table", "assets", "", "SELECT", False),)
    monkeypatch.setattr(migration.context, "is_offline_mode", lambda: False)
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(
        migration, "verify_baseline",
        Mock(return_value=(("history",), before_acl, ("functions",), {"oid": 42})),
    )
    monkeypatch.setattr(migration, "_apply_scheduler_access", Mock())
    monkeypatch.setattr(migration, "verify_policies", Mock())
    monkeypatch.setattr(migration.BASE, "verify_grants", Mock())
    monkeypatch.setattr(
        migration, "runtime_acl_snapshot",
        Mock(return_value=after_acl),
    )
    with pytest.raises(RuntimeError, match="Runtime ACL drift"):
        migration.upgrade()


def test_baseline_version_drift_stops_before_ddl(migration, monkeypatch) -> None:
    bind = Mock()
    bind.scalar.return_value = "wrong"
    execute = Mock()
    monkeypatch.setattr(migration.context, "is_offline_mode", lambda: False)
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(migration.op, "execute", execute)
    with pytest.raises(RuntimeError, match="exact 0008"):
        migration.upgrade()
    execute.assert_not_called()


@pytest.mark.parametrize("name,user,driver", [
    ("DATABASE_MIGRATION_URL", "alembic_test_user", "postgresql+psycopg"),
    ("PROMPT11_RUNTIME_URL", "nightclub_api", "postgresql+asyncpg"),
    ("PROMPT11_SCHEDULER_URL", "nightclub_scheduler", "postgresql+asyncpg"),
])
def test_harness_accepts_only_exact_local_role_urls(monkeypatch, name, user, driver) -> None:
    harness = load_harness()
    value = URL.create(
        driver, username=user, password="synthetic-local-only",
        host="127.0.0.1", port=5432, database=harness.DATABASE,
    ).render_as_string(hide_password=False)
    monkeypatch.setenv(name, value)
    assert harness.local_url(name, user).database == harness.DATABASE


def test_harness_preserves_historical_migration_role_inherit_posture() -> None:
    harness = load_harness()
    assert harness.EXPECTED_MIGRATION_ROLE == (
        "alembic_test_user", False, False, True, True, False, False, True,
    )


@pytest.mark.parametrize("changes", [
    {"host": "database.example.test"},
    {"port": 5433},
    {"database": "nightclub_ai"},
    {"username": "postgres"},
    {"password": None},
    {"query": {"sslmode": "require"}},
])
def test_harness_rejects_unsafe_targets(monkeypatch, changes) -> None:
    harness = load_harness()
    values = dict(
        drivername="postgresql+psycopg", username="alembic_test_user",
        password="synthetic-local-only", host="localhost", port=5432,
        database=harness.DATABASE, query=None,
    )
    values.update(changes)
    value = URL.create(**values).render_as_string(hide_password=False)
    monkeypatch.setenv("DATABASE_MIGRATION_URL", value)
    with pytest.raises(RuntimeError, match="unsafe target"):
        harness.local_url("DATABASE_MIGRATION_URL", "alembic_test_user")


def test_harness_never_terminates_sessions_or_provisions_roles() -> None:
    source = (ROOT / "scripts/local-dev/validate_prompt11_scheduler.py").read_text(encoding="utf-8").upper()
    assert "PG_TERMINATE_BACKEND" not in source
    assert "CREATE ROLE" not in source
    assert "ALTER ROLE" not in source
    assert "DROP ROLE" not in source
    assert "CREATE DATABASE NIGHTCLUB_AI_PROMPT11_SCHEDULER_TEST" in source
    assert "DROP DATABASE NIGHTCLUB_AI_PROMPT11_SCHEDULER_TEST" in source
