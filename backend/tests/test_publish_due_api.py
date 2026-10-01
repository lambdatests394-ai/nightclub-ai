"""No-network tests of the one internal automation endpoint."""

import asyncio
import hashlib
import hmac
import time
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from starlette.requests import Request

from backend.app import main as main_module
from backend.app.core.config import Settings
from backend.app.main import create_app
from backend.app.modules.automation.coordinator import PublishDueCounts
from backend.app.modules.automation.coordinator import PublishDueCoordinator
from backend.app.modules.automation.discovery import DiscoveredPublicationJob
from backend.app.modules.automation.internal_auth import canonical_payload
from backend.app.modules.automation.schemas import ExecutionStatus, ExecutorResult
from backend.app.modules.automation import publish_due_dependencies as composition
from backend.app.api.internal.automation import publish_due


pytestmark = pytest.mark.anyio
PATH = "/internal/automation/publish-due"
CURRENT = "current-internal-hmac-secret-value-0001"
PREVIOUS = "previous-internal-hmac-secret-value-002"
ZERO = PublishDueCounts(0, 0, 0, 0, 0, 0, 0, 0)


def signed(secret=CURRENT, *, timestamp=None, body=b"", path=PATH):
    timestamp = str(int(time.time())) if timestamp is None else str(timestamp)
    payload = canonical_payload(method="POST", path=path, timestamp=timestamp, raw_body=body)
    signature = "v1=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return [("X-N8N-Timestamp", timestamp), ("X-N8N-Signature", signature)]


class FakeCoordinator:
    def __init__(self, result=ZERO):
        self.calls = 0
        self.result = result

    async def run(self):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def application(*, secret=CURRENT):
    app = create_app(Settings(
        _env_file=None, n8n_internal_secret=secret,
        n8n_internal_secret_previous=PREVIOUS if secret else "",
    ))
    fake = FakeCoordinator()
    created = []

    def factory(_request):
        created.append(1)
        return fake

    app.state.publish_due_coordinator_factory = factory
    return app, fake, created


async def call(app, headers=(), *, path=PATH, body=b""):
    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        return await client.post(path, headers=headers, content=body)


@pytest.mark.parametrize("secret", [CURRENT, PREVIOUS])
async def test_current_and_previous_secret_succeed_with_exact_count_only_envelope(secret):
    app, fake, _ = application()
    response = await call(app, signed(secret))
    assert response.status_code == 200
    assert response.json()["data"] == ZERO.as_dict()
    assert set(response.json()) == {"data", "meta"}
    assert set(response.json()["meta"]) == {"correlationId"}
    assert response.headers["X-Correlation-Id"] == response.json()["meta"]["correlationId"]
    assert response.headers["Cache-Control"] == "no-store"
    assert fake.calls == 1


@pytest.mark.parametrize("headers", [
    [],
    signed(secret="wrong-internal-hmac-secret-value-000"),
    signed(timestamp=1),
    signed(timestamp=4_000_000_000),
    signed(body=b"other"),
    signed(path="/internal/automation/other"),
    signed() + [("X-N8N-Timestamp", "1")],
    signed() + [("X-N8N-Signature", "v1=" + "0" * 64)],
])
async def test_bad_hmac_is_generic_and_never_constructs_coordinator(headers):
    app, fake, created = application()
    response = await call(app, headers)
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["Cache-Control"] == "no-store"
    assert "WWW-Authenticate" not in response.headers
    assert response.json()["code"] == "INTERNAL_AUTHENTICATION_FAILED"
    assert response.json()["detail"] == "Internal authentication failed"
    assert response.json()["correlationId"] == response.headers["X-Correlation-Id"]
    assert fake.calls == 0 and created == []
    assert "X-N8N" not in response.text and "wrong-internal" not in response.text


async def test_missing_server_secret_is_indistinguishable_from_bad_signature():
    app, fake, created = application(secret="")
    response = await call(app, signed())
    assert response.status_code == 401
    assert response.json()["code"] == "INTERNAL_AUTHENTICATION_FAILED"
    assert fake.calls == 0 and created == []


@pytest.mark.parametrize("path,body,headers", [
    (PATH, b"payload", signed(body=b"payload")),
    (PATH + "?limit=100", b"", signed()),
])
async def test_authenticated_body_and_query_are_rejected_before_discovery(
        path, body, headers):
    app, fake, created = application()
    response = await call(app, headers, path=path, body=body)
    assert response.status_code == 422
    assert response.json()["code"] == "AUTOMATION_INVALID_REQUEST"
    assert response.headers["Cache-Control"] == "no-store"
    assert fake.calls == 0 and created == []
    assert "payload" not in response.text and "limit" not in response.text


async def test_unexpected_failure_is_generic_503_without_private_data():
    app, fake, _ = application()
    fake.result = RuntimeError("private job and provider detail")
    response = await call(app, signed())
    assert response.status_code == 503
    assert response.json()["code"] == "AUTOMATION_UNAVAILABLE"
    assert response.headers["Cache-Control"] == "no-store"
    assert "private job" not in response.text
    assert fake.calls == 1


async def test_endpoint_has_no_public_jwt_or_tenant_header_requirement():
    app, fake, _ = application()
    response = await call(app, signed())
    assert response.status_code == 200 and fake.calls == 1
    routes = {(route.path, method) for route in app.routes for method in route.methods}
    assert (PATH, "POST") in routes
    assert not any(path.startswith("/api/v1/internal") for path, _ in routes)


async def test_unexpected_second_job_returns_503_without_rolling_back_first_job():
    app, _fake, _ = application()
    org = UUID("20000000-0000-0000-0000-000000000001")
    first, second = UUID(int=1), UUID(int=2)
    durable = []

    class Discovery:
        async def discover(self, limit):
            assert limit == 2
            return (DiscoveredPublicationJob(org, first),
                    DiscoveredPublicationJob(org, second))

    class Executor:
        async def execute_system(self, organization_id, publication_job_id):
            assert organization_id == org
            if publication_job_id == first:
                durable.append("committed")
                return ExecutorResult(ExecutionStatus.PUBLISHED, first)
            raise RuntimeError("synthetic private job detail")

    app.state.publish_due_coordinator_factory = lambda _request: PublishDueCoordinator(
        Discovery(), Executor(), batch_size=2, max_concurrency=1,
    )
    response = await call(app, signed())
    assert response.status_code == 503
    assert response.json()["code"] == "AUTOMATION_UNAVAILABLE"
    assert "synthetic private job detail" not in response.text
    assert durable == ["committed"]


async def test_request_cancellation_is_not_translated_to_http_503():
    app, _fake, _ = application()

    class Cancelled:
        async def run(self):
            raise asyncio.CancelledError()

    app.state.publish_due_coordinator_factory = lambda _request: Cancelled()
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request({
        "type": "http", "method": "POST", "path": PATH,
        "query_string": b"", "app": app,
        "headers": [(name.lower().encode(), value.encode()) for name, value in signed()],
    }, receive)
    request.state.correlation_id = uuid4()
    with pytest.raises(asyncio.CancelledError):
        await publish_due(request)


def test_production_composition_keeps_scheduler_role_and_server_owned_limits():
    from backend.app.modules.automation.publish_due_dependencies import (
        get_publish_due_coordinator,
    )

    configured = Settings(
        _env_file=None, database_scheduler_url="", database_url=None,
        meta_graph_api_version="v24.0",
        meta_credential_encryption_key="AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8",
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        settings=configured, http_client=object(),
    )))
    coordinator = get_publish_due_coordinator(request)
    assert coordinator._discovery._expected_role == "nightclub_scheduler"
    assert coordinator._batch_size == 8 and coordinator._max_concurrency == 4


async def test_lifespan_disposes_scheduler_pool_on_normal_shutdown(monkeypatch):
    called = []

    async def dispose():
        called.append("disposed")

    monkeypatch.setattr(main_module, "dispose_scheduler_engine", dispose)
    app, _fake, _ = application()
    async with app.router.lifespan_context(app):
        assert app.state.http_client is not None
    assert called == ["disposed"]
    assert app.state.http_client is None


async def test_lifespan_finishes_scheduler_disposal_when_cancelled(monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    completed = []

    async def dispose():
        entered.set()
        await release.wait()
        completed.append("disposed")

    monkeypatch.setattr(main_module, "dispose_scheduler_engine", dispose)
    app, _fake, _ = application()

    async def cycle():
        async with app.router.lifespan_context(app):
            pass

    task = asyncio.create_task(cycle())
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert completed == ["disposed"] and app.state.http_client is None


def production_application(monkeypatch, selected=(), **meta_settings):
    """Keep real endpoint/composition/coordinator; replace only external boundaries."""
    app = create_app(Settings(_env_file=None, n8n_internal_secret=CURRENT,
                              **meta_settings))
    app.state.http_client = object()
    events = []

    async def discover(self, limit):
        assert self._expected_role == "nightclub_scheduler"
        assert limit == 8
        events.append("discovery_closed")
        return selected

    monkeypatch.setattr(composition.SQLAlchemyDueJobDiscovery, "discover", discover)
    for name in ("MetaGraphPublicationAdapter", "CredentialCipher",
                 "SQLAlchemyPublicationExecutionStore"):
        constructor = getattr(composition, name)

        def tracked(*args, _name=name, _constructor=constructor, **kwargs):
            events.append(_name)
            return _constructor(*args, **kwargs)

        monkeypatch.setattr(composition, name, tracked)
    return app, events


@pytest.mark.parametrize("meta_settings", [
    {},
    {"meta_graph_api_version": "v24.0"},
    {"meta_graph_api_version": "v24.0",
     "meta_credential_encryption_key": "malformed-private-key"},
])
async def test_production_empty_batch_returns_200_without_initializing_meta(
        monkeypatch, meta_settings):
    app, events = production_application(monkeypatch, **meta_settings)
    response = await call(app, signed())
    assert response.status_code == 200
    assert response.json()["data"] == ZERO.as_dict()
    assert set(response.json()) == {"data", "meta"}
    assert set(response.json()["meta"]) == {"correlationId"}
    assert response.json()["meta"]["correlationId"] == response.headers["X-Correlation-Id"]
    assert response.headers["Cache-Control"] == "no-store"
    assert events == ["discovery_closed"]


@pytest.mark.parametrize("meta_settings", [
    {},
    {"meta_graph_api_version": "v24.0"},
    {"meta_graph_api_version": "v24.0",
     "meta_credential_encryption_key": "malformed-private-key"},
])
async def test_production_nonempty_batch_requires_meta_before_executor_work(
        monkeypatch, meta_settings):
    org, job = uuid4(), uuid4()
    app, events = production_application(
        monkeypatch, (DiscoveredPublicationJob(org, job),), **meta_settings,
    )

    async def unexpected_execution(*_args, **_kwargs):
        pytest.fail("Missing Meta configuration must never reach runtime/provider work")

    monkeypatch.setattr(composition.FacebookPublicationExecutor, "execute_system",
                        unexpected_execution)
    response = await call(app, signed())
    assert response.status_code == 503
    assert response.json()["code"] == "AUTOMATION_UNAVAILABLE"
    assert response.json()["detail"] == "Automation unavailable"
    assert response.json()["correlationId"] == response.headers["X-Correlation-Id"]
    assert response.headers["Cache-Control"] == "no-store"
    assert events[0] == "discovery_closed"
    assert str(org) not in response.text and str(job) not in response.text
    assert "malformed-private-key" not in response.text


async def test_production_nonempty_batch_initializes_one_executor_after_discovery(
        monkeypatch):
    org = uuid4()
    selected = tuple(DiscoveredPublicationJob(org, uuid4()) for _ in range(3))
    app, events = production_application(
        monkeypatch, selected, meta_graph_api_version="v24.0",
        meta_credential_encryption_key="AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8",
    )
    instances, executed = [], []

    async def execute(self, organization_id, publication_job_id):
        assert events[0] == "discovery_closed"
        assert self.store.expected_role == "nightclub_api"
        assert organization_id == org
        instances.append(self)
        executed.append(publication_job_id)
        await asyncio.sleep(0)
        return ExecutorResult(ExecutionStatus.NOT_DUE, publication_job_id)

    monkeypatch.setattr(composition.FacebookPublicationExecutor, "execute_system", execute)
    response = await call(app, signed())
    assert response.status_code == 200
    assert response.json()["data"] == {**ZERO.as_dict(), "selected": 3,
                                      "processed": 3, "notDue": 3}
    assert set(executed) == {job.publication_job_id for job in selected}
    assert len({id(executor) for executor in instances}) == 1
    assert events == ["discovery_closed", "MetaGraphPublicationAdapter",
                      "SQLAlchemyPublicationExecutionStore", "CredentialCipher"]


async def test_production_discovery_failure_is_not_reported_as_an_empty_batch(monkeypatch):
    app, events = production_application(monkeypatch)

    async def failed_discovery(self, limit):
        events.append("discovery_failed")
        raise RuntimeError("private scheduler failure")

    monkeypatch.setattr(composition.SQLAlchemyDueJobDiscovery, "discover", failed_discovery)
    response = await call(app, signed())
    assert response.status_code == 503
    assert response.json()["code"] == "AUTOMATION_UNAVAILABLE"
    assert "private scheduler failure" not in response.text
    assert events == ["discovery_failed"]


@pytest.mark.parametrize("authenticated,path,status", [
    (False, PATH, 401),
    (True, PATH + "?limit=100", 422),
])
async def test_production_validation_precedes_discovery_and_meta(
        monkeypatch, authenticated, path, status):
    app, events = production_application(monkeypatch)
    response = await call(app, signed() if authenticated else (), path=path)
    assert response.status_code == status
    assert events == []
