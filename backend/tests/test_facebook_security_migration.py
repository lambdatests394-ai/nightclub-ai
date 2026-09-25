"""Static Prompt 10 B2-A security gates; catalog behavior is opt-in PostgreSQL."""

import hashlib
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import Mock

import pytest

from backend.app.core.database import Base
from backend.app.core.database_security import PROTECTED_TABLES
from backend.app.modules.automation.models import PublicationAttempt, PublicationJob
from backend.app.modules.integrations.models import FacebookOAuthState, PlatformConnection


ROOT = Path(__file__).parents[2]
MIGRATION = "backend/migrations/versions/20260924_0008_facebook_publication_access.py"
FROZEN_HASHES = {
    "sql/001_initial_schema.sql": "abaaf3e6977b77f9522648b23d67b4e0f7946a1d03dd6eee75986ecd8091749d",
    "sql/002_indexes.sql": "7d207e1c300f55d3978ed62862a7f1c5148100f3829b90b9fa834d2ad9c16895",
    "sql/003_rls.sql": "1a15e1799465aa4498f482a3bba0ad76aff933bc7611341248fc5ee5eaa940b2",
    "backend/migrations/versions/20260907_0001_initial_schema.py": "9e6d43ef6d9d4be095ccd07b22917968dad8bcda7b9f92e038fd0388525b14d0",
    "backend/migrations/versions/20260907_0002_indexes.py": "69c1d2073d92688c310c5d1534cbeeabde4eba30b6d29df58d5c14b64fba5812",
    "backend/migrations/versions/20260909_0003_identity_rls.py": "7c7d8b88fb6be79d7d8ab1b6243edfb4ac71ed8c73bd0d56f4f7bfd6e08863ca",
    "backend/migrations/versions/20260910_0004_campaign_business_access.py": "1cd3b3c2b46533165707f775453e90ba3f2e18b822ca12c6a388a044d9485990",
    "backend/migrations/versions/20260910_0005_content_business_access.py": "913093b98df2ea5c70488686e5249a2c9b729bbd54c9bce4d71183f499ed37ed",
    "backend/migrations/versions/20260922_0006_asset_business_access.py": "3e5b1a42a9a93d20ceba7eb8e6809d3ed5d3247c957d8721b94ee9cec88b2943",
    "backend/migrations/versions/20260923_0007_ai_generation_business_access.py": "1b10fcd120c0a181671697a16d8f424091942984c35837a6da5ae80e9412bc97",
}


def load_migration():
    spec = spec_from_file_location("prompt10_b2a_gate", ROOT / MIGRATION)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migration():
    return load_migration()


def test_revision_parent_offline_and_unsafe_downgrade(migration, monkeypatch):
    assert migration.revision == "20260924_0008"
    assert migration.down_revision == "20260923_0007"
    with pytest.raises(RuntimeError, match="Unsafe security downgrade blocked"):
        migration.downgrade()
    monkeypatch.setattr(migration.context, "is_offline_mode", lambda: True)
    with pytest.raises(RuntimeError, match="online"):
        migration.upgrade()


def test_exact_prompt10_policy_and_table_certification_counts(migration):
    assert len(migration.historical_policies()) == 30
    assert len(migration.POLICIES) == 13
    assert len(migration.TABLES) == 22
    assert set(migration.TABLES) == set(PROTECTED_TABLES)
    assert set(migration.POLICIES) == {
        "facebook_oauth_states_business_select",
        "facebook_oauth_states_business_insert",
        "facebook_oauth_states_business_update",
        "platform_connections_facebook_insert",
        "platform_connections_facebook_update",
        "publication_jobs_business_select",
        "publication_jobs_business_insert",
        "publication_jobs_business_update",
        "publication_attempts_business_select",
        "publication_attempts_business_insert",
        "publication_attempts_business_update",
        "content_items_publication_update",
        "audit_facebook_publication_insert",
    }


def test_historical_sql_and_migrations_are_byte_frozen():
    for relative, expected in FROZEN_HASHES.items():
        assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == expected


def test_migration_has_no_privileged_role_or_destructive_access_ddl():
    source = (ROOT / MIGRATION).read_text(encoding="utf-8").upper()
    for forbidden in (
        "SECURITY DEFINER",
        "ALTER ROLE",
        "BYPASSRLS",
        "DISABLE ROW LEVEL SECURITY",
        "GRANT DELETE",
        "FOR DELETE",
    ):
        assert forbidden not in source
    assert "FORCE ROW LEVEL SECURITY" in source


def test_policy_scopes_and_grant_allowlists_are_narrow(migration):
    oauth = migration.POLICIES["facebook_oauth_states_business_select"]
    assert oauth[0:2] == ("facebook_oauth_states", "SELECT")
    assert "app.organization_id" in oauth[2] and "actor_id" in oauth[2] and "app.user_id" in oauth[2]
    assert "last_error_message" not in migration.UPDATE_COLUMNS["content_items"]
    assert "connection_id" not in migration.UPDATE_COLUMNS["content_items"]
    assert "created_at" not in migration.UPDATE_COLUMNS["facebook_oauth_states"]
    assert "updated_at" not in migration.OAUTH_SELECT_COLUMNS
    # Public redacted connection reads expose both lifecycle timestamps.
    assert {"created_at", "updated_at"} <= migration.CONNECTION_SELECT_COLUMNS


def test_historical_policy_snapshot_is_compared_exactly(migration):
    historical = tuple(
        ("public", f"table_{i}", f"history_{i}", "PERMISSIVE", (migration.ROLE,),
         "SELECT", f"qual_{i}", None)
        for i in range(30)
    )
    current = tuple(
        ("public", table, name, "PERMISSIVE", (migration.ROLE,), command, using, check)
        for name, (table, command, using, check) in migration.POLICIES.items()
    )
    bind = Mock()
    bind.execute.return_value.all.return_value = [*historical, *current]
    migration.verify_policies(bind, historical)
    changed = list(historical)
    changed[0] = (*changed[0][:6], "widened", changed[0][7])
    bind.execute.return_value.all.return_value = [*changed, *current]
    with pytest.raises(RuntimeError, match="Historical policy catalog drift"):
        migration.verify_policies(bind, historical)


def test_baseline_drift_stops_before_any_ddl(migration, monkeypatch):
    bind, execute, create_table = Mock(), Mock(), Mock()
    bind.scalar.return_value = "wrong"
    monkeypatch.setattr(migration.context, "is_offline_mode", lambda: False)
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(migration.op, "execute", execute)
    monkeypatch.setattr(migration.op, "create_table", create_table)
    with pytest.raises(RuntimeError, match="exact 0007"):
        migration.upgrade()
    execute.assert_not_called()
    create_table.assert_not_called()


def test_oauth_and_publication_orm_metadata_aligns_with_0008():
    assert FacebookOAuthState.__table__.fullname == "facebook_oauth_states"
    assert FacebookOAuthState.__table__.c.state_digest.type.length == 64
    assert FacebookOAuthState.__table__.c.expires_at.type.timezone
    assert FacebookOAuthState.__table__.c.consumed_at.type.timezone
    assert PublicationJob.__table__.c.status.type.name == "publication_status"
    assert PublicationJob.__table__.c.scheduled_for.type.timezone
    checks = {constraint.name for constraint in PublicationAttempt.__table__.constraints}
    assert {
        "publication_attempts_valid_outcome",
        "publication_attempts_outcome_finish_shape",
        "publication_attempts_valid_finish_time",
    } <= checks
    assert PublicationAttempt.__table__.c.started_at.type.timezone
    assert PlatformConnection.__table__.c.platform.type.name == "platform_type"
    assert "facebook_oauth_states" in Base.metadata.tables


def test_required_database_guards_and_active_retry_state_are_declared():
    source = (ROOT / MIGRATION).read_text(encoding="utf-8")
    for fragment in (
        "enforce_facebook_oauth_state_lifecycle",
        "enforce_publication_attempt_lifecycle",
        "enforce_publication_job_lifecycle",
        "enforce_facebook_connection_invariants",
        "enforce_content_publication_lifecycle",
        "status IN ('pending','leased','publishing','retryable_failure')",
        "pg_column_size(NEW.provider_response) > 4096",
    ):
        assert fragment in source


def test_active_index_verifier_accepts_only_the_frozen_and_amended_predicates(migration):
    bind = Mock()
    bind.execute.return_value.one_or_none.return_value = (
        True,
        ["content_item_id"],
        "(status = ANY (ARRAY['pending'::publication_status, "
        "'leased'::publication_status, 'publishing'::publication_status]))",
    )
    migration._verify_active_index(bind)
    with pytest.raises(RuntimeError, match="predicate drift"):
        migration._verify_active_index(bind, expected_retryable=True)
    bind.execute.return_value.one_or_none.return_value = (
        True,
        ["content_item_id"],
        "(status = ANY (ARRAY['pending'::publication_status, "
        "'leased'::publication_status, 'publishing'::publication_status, "
        "'retryable_failure'::publication_status]))",
    )
    migration._verify_active_index(bind, expected_retryable=True)


def test_historical_grant_aggregation_preserves_content_version_insert_columns(migration):
    inserts, updates = migration._historical_grants()
    assert {"id", "content_item_id", "version_no", "body", "title", "link_url",
            "payload", "source", "created_by", "ai_generation_id"} <= inserts["content_versions"]
    assert {"status", "current_version_no", "approved_version_no", "updated_at"} <= updates["content_items"]
