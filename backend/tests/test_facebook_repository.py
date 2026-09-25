from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from backend.app.modules.integrations.repository import FacebookConnectionRepository


def sql(statement):
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}))


def test_ordinary_connection_projection_excludes_all_credential_columns():
    repository = FacebookConnectionRepository(None, uuid4(), uuid4())
    query = sql(repository._read_query()).lower()
    assert "credentials_ciphertext" not in query
    assert "credential_key_version" not in query
    assert "platform_connections.organization_id" not in query.partition("\nfrom ")[0]
    assert "platform_connections.organization_id" in query
    assert "platform_connections.platform" in query


@pytest.mark.anyio
@pytest.mark.parametrize("method,args", [
    ("list_facebook_connections", ()),
    ("get_facebook_connection", (uuid4(),)),
])
async def test_each_ordinary_read_executes_only_the_redacted_projection(method, args):
    session = MagicMock()
    result = MagicMock()
    result.__iter__.return_value = iter(())
    result.one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result)
    repository = FacebookConnectionRepository(session, uuid4(), uuid4())
    await getattr(repository, method)(*args)
    query = sql(session.execute.await_args.args[0]).lower()
    projection = query.partition("\nfrom ")[0]
    assert "credentials_ciphertext" not in projection
    assert "credential_key_version" not in projection
    assert "platform_connections.organization_id" in query
    assert "platform_connections.platform" in query


@pytest.mark.anyio
async def test_dedicated_credential_projection_is_explicit_tenant_and_facebook_only():
    session = MagicMock()
    result = MagicMock()
    result.one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result)
    repository = FacebookConnectionRepository(session, uuid4(), uuid4())
    assert await repository.get_facebook_credentials(uuid4()) is None
    query = sql(session.execute.await_args.args[0]).lower()
    assert "credentials_ciphertext" in query and "credential_key_version" in query
    assert "platform_connections.organization_id" in query
    assert "platform_connections.platform" in query
    projection = query.partition("\nfrom ")[0]
    for column in ("organization_id", "platform", "external_account_id",
                   "credential_key_version", "credentials_ciphertext"):
        assert f"platform_connections.{column}" in projection
    assert "platform_connections.display_name" not in projection


@pytest.mark.anyio
async def test_insert_uses_conflict_arbitration_on_immutable_business_identity():
    session = MagicMock()
    session.scalar = AsyncMock(return_value=uuid4())
    repository = FacebookConnectionRepository(session, uuid4(), uuid4())
    created = await repository.insert_verified_facebook_connection(
        connection_id=uuid4(), page_id="123", display_name="Night Club",
        capabilities={"canPublishPosts": True}, ciphertext=b"opaque", key_version=1,
        token_expires_at=None, verified_at=datetime.now(UTC),
    )
    statement = session.scalar.await_args.args[0]
    compiled = sql(statement).lower()
    assert created is True
    assert "on conflict (organization_id, platform, external_account_id) do nothing" in compiled


@pytest.mark.anyio
async def test_reconnect_updates_mutable_fields_only():
    session = MagicMock()
    result = MagicMock(rowcount=0)
    session.execute = AsyncMock(return_value=result)
    repository = FacebookConnectionRepository(session, uuid4(), uuid4())
    assert await repository.reconnect_facebook_connection(
        uuid4(), display_name="Club", capabilities={}, ciphertext=b"opaque",
        key_version=1, token_expires_at=None, verified_at=datetime.now(UTC),
    ) is None
    params = session.execute.await_args.args[0].compile().params
    assert not {"id", "organization_id", "platform", "external_account_id"} & set(params)
    assert any(name.startswith("display_name") for name in params)
