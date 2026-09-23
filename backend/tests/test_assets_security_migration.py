"""Static and dry preflight gates; real catalog behavior remains a separate opt-in."""
import ast
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import insert
from sqlalchemy.dialects import postgresql

from backend.app.modules.assets.audit import AssetAuditWriter
from backend.app.modules.assets.models import Asset
from backend.app.modules.assets.repository import AssetRepository
from backend.app.modules.content.models import ContentVersion
from backend.app.modules.content.repository import ContentRepository
from backend.tests.assets_fakes import AssetFixture, intent

ROOT = Path(__file__).parents[2]
MIGRATION = "backend/migrations/versions/20260922_0006_asset_business_access.py"


def load(relative):
    spec = spec_from_file_location("prompt8_local_gate", ROOT / relative)
    module = module_from_spec(spec); spec.loader.exec_module(module)
    return module


@pytest.fixture
def migration():
    return load(MIGRATION)


def test_parent_offline_and_security_downgrade(migration, monkeypatch):
    assert migration.revision == "20260922_0006" and migration.down_revision == "20260910_0005"
    with pytest.raises(RuntimeError, match="Unsafe security downgrade blocked"): migration.downgrade()
    monkeypatch.setattr(migration.context, "is_offline_mode", lambda: True)
    with pytest.raises(RuntimeError, match="online"): migration.upgrade()


def test_baseline_drift_stops_before_any_ddl(migration, monkeypatch):
    bind, ddl = Mock(), Mock(); bind.scalar.return_value = "wrong"
    monkeypatch.setattr(migration.context, "is_offline_mode", lambda: False)
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(migration.op, "execute", ddl)
    with pytest.raises(RuntimeError, match="exact 0005"): migration.upgrade()
    ddl.assert_not_called()


def test_historical_policy_preflight_detects_semantic_drift(migration):
    rows = [(name, t, [migration.ROLE], cmd, "PERMISSIVE", using, check)
            for name, (t, cmd, using, check) in migration.historical_policies().items()]
    bind = Mock(); bind.execute.return_value.all.return_value = rows
    migration.verify_historical_policies(bind)
    # A widened historical policy must stop, including a changed literal's spaces.
    for field, changed in ((5, "true"), (2, ["public"]), (4, "RESTRICTIVE")):
        bad = list(rows[0]); bad[field] = changed
        bind.execute.return_value.all.return_value = [tuple(bad), *rows[1:]]
        with pytest.raises(RuntimeError, match="drift"): migration.verify_historical_policies(bind)
    assert migration.normalized("status='draft'") != migration.normalized("status='d r a f t'")


def test_only_six_policies_and_exact_column_privileges(migration):
    assert set(migration.POLICIES) == {"assets_business_select", "assets_business_insert", "assets_business_update",
        "content_assets_business_select", "content_assets_business_insert", "audit_asset_insert"}
    assert len(migration.historical_policies()) + len(migration.POLICIES) == 24
    assert migration.UPDATE_COLUMNS == {"assets": {"status", "width", "height", "updated_at"}}
    assert migration.INSERT_COLUMNS["content_assets"] == {"content_version_id", "content_item_id", "organization_id", "asset_id", "position"}
    assert not {"width", "height", "deleted_at", "created_at"} & migration.INSERT_COLUMNS["assets"]
    source = (ROOT / MIGRATION).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            statement = node.value.strip().upper()
            assert not statement.startswith(("CREATE ROLE", "ALTER ROLE", "DROP ROLE", "GRANT DELETE", "GRANT UPDATE ON", "UPDATE PUBLIC.CONTENT_VERSIONS"))


def test_creation_policy_checks_pair_tenant_creator_and_ready(migration):
    table, cmd, using, check = migration.POLICIES["content_assets_business_insert"]
    assert (table, cmd, using) == ("content_assets", "INSERT", None)
    for fragment in ("pg_current_xact_id()", "transaction_timestamp()", "cv.version_no = ci.current_version_no",
                     "ci.status = 'draft'", "cv.created_by =", "a.status = 'ready'", "position BETWEEN 0 AND 9",
                     "ci.organization_id =", "a.organization_id =", "app.user_id", "app.organization_id"):
        assert fragment in check
    for forbidden in ("xmin", "SECURITY DEFINER"):
        assert forbidden not in check


def test_upgrade_adds_nullable_marker_before_future_default_and_no_backfill(migration, monkeypatch):
    calls = []
    monkeypatch.setattr(migration.context,"is_offline_mode",lambda:False)
    monkeypatch.setattr(migration.op,"get_bind",Mock())
    monkeypatch.setattr(migration,"verify_baseline",Mock())
    monkeypatch.setattr(migration,"verify_grants",Mock())
    monkeypatch.setattr(migration,"verify_historical_policies",Mock())
    monkeypatch.setattr(migration.op,"execute",calls.append)
    migration.upgrade()
    assert calls[0] == "ALTER TABLE public.content_versions ADD COLUMN attachment_creation_xid xid8"
    assert calls[1] == "ALTER TABLE public.content_versions ALTER COLUMN attachment_creation_xid SET DEFAULT pg_current_xact_id()"
    assert sum(statement.startswith("CREATE POLICY") for statement in calls)==6
    assert not any(statement.startswith("UPDATE ") for statement in calls)
    assert not any("attachment_creation_xid" in statement for statement in calls if statement.startswith("GRANT"))


@pytest.mark.anyio
async def test_asset_orm_insert_and_content_marker_server_owned():
    assert Asset.__table__.c.kind.type.enums == ["image", "video", "document"]
    assert not Asset.__table__.c.kind.type.create_type
    assert str(Asset.__table__.c.sha256.type) == "CHAR(64)"
    assert Asset.__table__.c.deleted_at.type.timezone is True
    assert str(ContentVersion.__table__.c.attachment_creation_xid.type) == "xid8"
    session = AsyncMock(); repository = ContentRepository(session, uuid4())
    await repository.add_content_version(ContentVersion(id=uuid4(), content_item_id=uuid4(), version_no=1,
        body="fixture", source="manual", created_by=uuid4()))
    statement = session.execute.call_args.args[0].compile(dialect=postgresql.dialect())
    assert "attachment_creation_xid" not in str(statement) and "created_at" not in statement.params


@pytest.mark.anyio
async def test_asset_insert_whitelist_and_audit_redaction():
    case = AssetFixture(); session = AsyncMock()
    repo = AssetRepository(session, case.context.organization_id)
    await repo.create_pending(asset_id=uuid4(), actor_id=case.context.user_id, bucket="nightclub-assets", key="safe-key",
        filename="private-name.png", mime_type="image/png", byte_size=3, sha256="0"*64)
    statement = session.execute.call_args.args[0].compile(dialect=postgresql.dialect())
    assert "ON CONFLICT (organization_id, sha256) DO NOTHING" in str(statement)
    assert set(statement.params) == {"id", "organization_id", "uploaded_by", "storage_bucket", "storage_key",
        "original_filename", "kind", "mime_type", "byte_size", "sha256", "status"}
    await case.coordinator.upload(uuid4(), intent())
    asset = next(iter(case.repository.rows.values()))
    await AssetAuditWriter(session,case.context,uuid4()).write("asset.ready",asset)
    params=session.execute.call_args.args[0].compile(dialect=postgresql.dialect()).params
    assert set(params["after"]) == {"organizationId","assetId","previousStatus","status","kind","mimeType","byteSize","width","height","rejectionCode"}
    assert "filename" not in str(params["after"]).lower() and "storage_key" not in params["after"]


@pytest.mark.anyio
async def test_lock_hidden_terminal_row_reports_state_without_unlocked_pending():
    from backend.app.modules.assets.errors import AssetConflict
    session = AsyncMock()
    repository = AssetRepository(session, uuid4())
    terminal = SimpleNamespace(status="ready")
    session.scalar.side_effect = [None, terminal]
    assert await repository.find_by_id(uuid4(), lock=True) is terminal
    session.scalar.side_effect = [None, SimpleNamespace(status="pending")]
    with pytest.raises(AssetConflict): await repository.find_by_id(uuid4(), lock=True)


def test_services_do_not_commit_or_expose_internal_field():
    for filename in ("repository.py","service.py","coordinator.py"):
        tree=ast.parse((ROOT/"backend/app/modules/assets"/filename).read_text(encoding="utf-8"))
        assert not any(isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=="commit" for node in ast.walk(tree))
    from backend.app.modules.content.schemas import ContentCreate,ContentPatch,ContentCurrentRead
    assert all("attachment_creation_xid" not in model.model_fields for model in (ContentCreate,ContentPatch,ContentCurrentRead))


def test_harness_unset_environment_fails_before_connect(monkeypatch):
    harness=load("scripts/local-dev/validate_prompt8.py")
    monkeypatch.delenv("DATABASE_MIGRATION_URL",raising=False)
    connect=Mock(side_effect=AssertionError("No connection in unit tests"))
    monkeypatch.setattr(harness.psycopg,"connect",connect)
    with pytest.raises(RuntimeError,match="withheld"):harness.run()
    connect.assert_not_called()
    assert harness.DATABASE == "nightclub_ai_prompt8_test" and harness.REVISION == "20260922_0006"


@pytest.mark.parametrize("component,value", [("host","remote.example.test"),("port",5433),("database","other_test"),
    ("username","postgres"),("password",None),("query",{"host":"remote.example.test"})])
def test_harness_rejects_unsafe_target_before_connect(monkeypatch,component,value):
    from sqlalchemy.engine import URL
    harness=load("scripts/local-dev/validate_prompt8.py")
    url=URL.create("postgresql+psycopg",username="alembic_test_user",password="synthetic-fixture-only",
        host="127.0.0.1",port=5432,database=harness.DATABASE)
    if component=="password" and value is None: url=url._replace(password=None)
    else: url=url.set(**{component:value})
    monkeypatch.setenv("DATABASE_MIGRATION_URL",url.render_as_string(hide_password=False))
    connect=Mock(); monkeypatch.setattr(harness.psycopg,"connect",connect)
    with pytest.raises(RuntimeError,match="withheld"):harness.run()
    connect.assert_not_called()


def test_harness_and_historical_revisions_stay_independent():
    for number,head in ((5,"20260909_0003"),(6,"20260910_0004"),(7,"20260910_0005"),(8,"20260922_0006")):
        assert load(f"scripts/local-dev/validate_prompt{number}.py").REVISION==head
    source=(ROOT/"scripts/local-dev/validate_prompt8.py").read_text(encoding="utf-8")
    for forbidden in ("CREATE ROLE","ALTER ROLE","DROP ROLE","sql/003_rls.sql","pg_terminate_backend"):
        assert forbidden not in source
