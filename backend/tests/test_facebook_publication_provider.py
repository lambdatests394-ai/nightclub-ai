"""Local-only Meta publication adapter tests; no request reaches the internet."""

from datetime import UTC, datetime, timedelta
import json

import httpx
from pydantic import SecretStr
import pytest

from backend.app.modules.integrations.facebook_provider import (
    GRAPH_ORIGIN,
    MAX_RESPONSE_BYTES,
    MetaGraphPublicationAdapter,
    ProviderDisposition,
    ReconciliationDisposition,
)


pytestmark = pytest.mark.anyio
TOKEN = "synthetic-page-token-not-a-real-secret"
STARTED = datetime(2026, 9, 25, 18, 0, tzinfo=UTC)


def adapter(handler):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=httpx.Timeout(15.0),
        follow_redirects=False,
    )
    return MetaGraphPublicationAdapter("v24.0", client=client), client


@pytest.mark.parametrize("version", ["v24.0/evil", "v0.1", "24.0", "v24", "v24.0 "])
async def test_graph_version_rejects_noncanonical_path_input(version):
    with pytest.raises(ValueError, match="server-owned Meta Graph version"):
        MetaGraphPublicationAdapter(version)


async def test_text_only_request_uses_fixed_graph_path_bearer_and_one_post():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "opaque-post-id"}, headers={
            "x-fb-trace-id": "safe-trace",
        })
    provider, client = adapter(handler)
    try:
        result = await provider.publish_post(
            page_id="12345", message="Exact approved body", link_url=None,
            access_token=SecretStr(TOKEN),
        )
    finally:
        await client.aclose()
    assert result.disposition == ProviderDisposition.SUCCEEDED
    assert result.external_post_id == "opaque-post-id"
    assert result.provider_request_id == "safe-trace"
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == f"{GRAPH_ORIGIN}/v24.0/12345/feed"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(request.url)
    assert request.content == b"message=Exact+approved+body"


async def test_text_and_https_link_are_sent_exactly_without_decoration():
    captured = None
    def handler(request):
        nonlocal captured
        captured = request.content
        return httpx.Response(200, json={"id": "post/control-safe"})
    provider, client = adapter(handler)
    try:
        result = await provider.publish_post(
            page_id="987", message="Night event", link_url="https://example.test/event?a=1",
            access_token=SecretStr(TOKEN),
        )
    finally:
        await client.aclose()
    assert result.external_post_id == "post/control-safe"
    assert captured == b"message=Night+event&link=https%3A%2F%2Fexample.test%2Fevent%3Fa%3D1"


@pytest.mark.parametrize("response,disposition,code", [
    (httpx.Response(200, json={}), ProviderDisposition.AMBIGUOUS, "META_MALFORMED_SUCCESS"),
    (httpx.Response(200, content=b"not-json"), ProviderDisposition.AMBIGUOUS, "META_MALFORMED_SUCCESS"),
    (httpx.Response(429, json={"error":{"code":4}}), ProviderDisposition.RETRYABLE, "META_4"),
    (httpx.Response(503, json={"error":{"code":2}}), ProviderDisposition.RETRYABLE, "META_2"),
    (httpx.Response(400, json={"error":{"code":100}}), ProviderDisposition.PERMANENT, "META_100"),
    (httpx.Response(400, json={"error":{"code":2,"is_transient":True}}), ProviderDisposition.RETRYABLE, "META_2"),
    (httpx.Response(401, json={"error":{"code":190}}), ProviderDisposition.PERMANENT, "META_AUTHENTICATION_FAILED"),
])
async def test_publish_response_classification(response, disposition, code):
    calls = 0
    def handler(_request):
        nonlocal calls
        calls += 1
        return response
    provider, client = adapter(handler)
    try:
        result = await provider.publish_post(
            page_id="123", message="body", link_url=None,
            access_token=SecretStr(TOKEN),
        )
    finally:
        await client.aclose()
    assert result.disposition == disposition and result.error_code == code
    assert calls == 1
    assert TOKEN not in repr(result) and TOKEN not in str(result)


async def test_oversized_success_is_ambiguous_without_raw_response():
    provider, client = adapter(lambda _request: httpx.Response(
        200, content=b"x" * (MAX_RESPONSE_BYTES + 1),
    ))
    try:
        result = await provider.publish_post(
            page_id="123", message="body", link_url=None,
            access_token=SecretStr(TOKEN),
        )
    finally:
        await client.aclose()
    assert result.disposition == ProviderDisposition.AMBIGUOUS
    assert result.error_code == "META_MALFORMED_SUCCESS"
    assert len(repr(result)) < 500


@pytest.mark.parametrize("exception", [
    httpx.ReadTimeout("synthetic"),
    httpx.WriteTimeout("synthetic"),
    httpx.RemoteProtocolError("synthetic"),
])
async def test_transport_after_write_is_ambiguous_and_never_retried(exception):
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        raise exception
    provider, client = adapter(handler)
    try:
        result = await provider.publish_post(
            page_id="123", message="body", link_url=None,
            access_token=SecretStr(TOKEN),
        )
    finally:
        await client.aclose()
    assert result.disposition == ProviderDisposition.AMBIGUOUS
    assert calls == 1


def candidate(identifier="post-1", message="body", link_marker=False, **extra):
    value = {"id": identifier, "message": message, "created_time": STARTED.isoformat()}
    if link_marker is not False:
        value["link"] = link_marker
    value.update(extra)
    return value


async def reconcile(payload, *, message="body", link=None, status=200):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, json=payload)
    provider, client = adapter(handler)
    try:
        result = await provider.reconcile_post(
            page_id="123", message=message, link_url=link,
            attempt_started_at=STARTED,
            observed_at=STARTED + timedelta(minutes=1),
            access_token=SecretStr(TOKEN),
        )
    finally:
        await client.aclose()
    return result, requests


async def test_reconciliation_exact_text_match_and_bounded_query():
    result, requests = await reconcile({"data":[candidate()]})
    assert result.disposition == ReconciliationDisposition.MATCHED
    assert result.external_post_id == "post-1"
    assert len(requests) == 1 and requests[0].method == "GET"
    assert requests[0].url.params["limit"] == "25"
    assert requests[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(requests[0].url) and TOKEN not in repr(result)


@pytest.mark.parametrize("payload,link,expected", [
    ({"data":[]}, None, ReconciliationDisposition.NOT_FOUND),
    ({"data":[candidate("one"),candidate("two")]}, None, ReconciliationDisposition.AMBIGUOUS_MULTIPLE),
    ({"data":[candidate(message="wrong")]}, None, ReconciliationDisposition.NOT_FOUND),
    ({"data":[candidate(link_marker="https://wrong.test")]}, "https://right.test", ReconciliationDisposition.NOT_FOUND),
    ({"data":[candidate(link_marker="https://right.test")]}, "https://right.test", ReconciliationDisposition.MATCHED),
    ({"data":[candidate(created_time=(STARTED-timedelta(minutes=6)).isoformat())]}, None, ReconciliationDisposition.NOT_FOUND),
    ({"data":[{"id":"x","created_time":STARTED.isoformat()}]}, None, ReconciliationDisposition.INCONCLUSIVE),
    ({"data":"invalid"}, None, ReconciliationDisposition.INCONCLUSIVE),
])
async def test_reconciliation_classifications(payload, link, expected):
    result, _ = await reconcile(payload, link=link)
    assert result.disposition == expected


async def test_reconciliation_read_failure_is_inconclusive():
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("synthetic")
    provider, client = adapter(handler)
    try:
        result = await provider.reconcile_post(
            page_id="123", message="body", link_url=None,
            attempt_started_at=STARTED, observed_at=STARTED + timedelta(minutes=1),
            access_token=SecretStr(TOKEN),
        )
    finally:
        await client.aclose()
    assert result.disposition == ReconciliationDisposition.INCONCLUSIVE
    assert calls == 1


async def test_reconciliation_rejects_more_than_twenty_five_candidates():
    payload = {"data": [candidate(f"post-{index}") for index in range(26)]}
    result, requests = await reconcile(payload)
    assert result.disposition == ReconciliationDisposition.INCONCLUSIVE
    assert result.error_code == "META_RECONCILIATION_RESPONSE_INVALID"
    assert requests[0].url.params["limit"] == "25"


async def test_response_trace_is_bounded_and_raw_graph_error_is_not_retained():
    raw_message = "provider-controlled-sensitive-text"
    payload = {"error":{"code":100,"message":raw_message,"fbtrace_id":"trace-body"}}
    result, _ = await reconcile(payload, status=400)
    serialized = json.dumps(result.__dict__ if hasattr(result, "__dict__") else {
        "error_code": result.error_code,
        "request_id": result.provider_request_id,
    })
    assert raw_message not in serialized
    assert result.provider_request_id == "trace-body"
