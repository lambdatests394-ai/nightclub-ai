"""Read-only discovery shape without a PostgreSQL connection."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from backend.app.modules.automation import discovery


pytestmark = pytest.mark.anyio
ORG = UUID("20000000-0000-0000-0000-000000000001")
JOB = UUID("93000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


async def test_discovery_uses_scheduler_scope_role_and_only_returns_ids(monkeypatch):
    calls = []

    class Session:
        async def execute(self, statement, values):
            calls.append((str(statement), values))
            return SimpleNamespace(all=lambda: [SimpleNamespace(
                organization_id=ORG, publication_job_id=JOB,
            )])

    @asynccontextmanager
    async def read_only_scheduler_scope():
        calls.append("scope_open")
        try:
            yield Session()
        finally:
            calls.append("scope_closed")

    async def verified(_session, role):
        assert role == "nightclub_scheduler"
        calls.append("role_verified")

    monkeypatch.setattr(discovery, "verify_scheduler_role", verified)
    store = discovery.SQLAlchemyDueJobDiscovery(
        session_scope=read_only_scheduler_scope, clock=lambda: NOW,
    )
    jobs = await store.discover(8)
    assert jobs == (discovery.DiscoveredPublicationJob(ORG, JOB),)
    assert calls[0:2] == ["scope_open", "role_verified"]
    assert calls[-1] == "scope_closed"
    sql, params = calls[2]
    assert params == {"now": NOW, "limit": 8}
    assert "JOIN public.content_items" in sql
    assert "ORDER BY CASE" in sql and "pj.id ASC" in sql
    assert "pj.status = 'pending'" in sql
    assert "pj.status = 'retryable_failure'" in sql
    assert "pj.status IN ('leased', 'publishing')" in sql
    assert "pj.lease_expires_at <= :now" in sql
    assert "LIMIT :limit" in sql
    assert "FOR UPDATE" not in sql.upper()
    assert "SELECT ci.organization_id, pj.id AS publication_job_id" in sql


@pytest.mark.parametrize("invalid", [0, -1, 33, True])
async def test_discovery_rejects_non_server_limits_before_database(invalid):
    store = discovery.SQLAlchemyDueJobDiscovery()
    with pytest.raises(ValueError):
        await store.discover(invalid)
