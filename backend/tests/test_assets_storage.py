"""Supabase REST contracts via MockTransport, never a live Supabase service."""
from datetime import UTC, datetime
import json
from uuid import uuid4
import httpx
import pytest

from backend.app.core.config import Settings
from backend.app.modules.assets.storage import StorageConflict,StorageInvalidResponse,StorageMissing,StorageTimeout,StorageUnavailable
from backend.app.modules.assets.supabase_storage import SupabaseStorage

pytestmark=pytest.mark.anyio
BUCKET="nightclub-assets"
KEY=f"org/{uuid4()}/assets/{uuid4()}/asset.png"
SETTINGS=Settings(_env_file=None,supabase_url="https://storage.example.test",supabase_service_role_key="synthetic-server-only-key")


async def test_upload_create_only_fixed_two_hours_and_download_60(caplog):
    calls=[]
    def handler(request):
        calls.append(request)
        assert request.url.host=="storage.example.test"
        assert request.headers["authorization"] == "Bearer synthetic-server-only-key"
        if "/upload/sign/" in request.url.path:
            assert request.headers["x-upsert"] == "false" and json.loads(request.content)=={}
            return httpx.Response(200,json={"url":request.url.path.replace("/storage/v1","")+"?token=synthetic-capability"})
        assert json.loads(request.content)=={"expiresIn":60}
        return httpx.Response(200,json={"signedURL":request.url.path.replace("/storage/v1","")+"?token=synthetic-capability"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),trust_env=False) as client:
        provider=SupabaseStorage(SETTINGS,client)
        start=datetime.now(UTC)
        upload=await provider.create_signed_upload(BUCKET,KEY,mime_type="image/png",upsert=False)
        assert 7199 <= (upload.expires_at-start).total_seconds() <= 7201
        assert upload.headers=={"Content-Type":"image/png"} and upload.method=="PUT"
        assert "synthetic-server-only-key" not in upload.url and "synthetic-capability" not in repr(upload)
        result=await provider.create_signed_download(BUCKET,KEY,expires_in=60)
        assert 59 <= (result.expires_at-datetime.now(UTC)).total_seconds() <= 60
        with pytest.raises(StorageInvalidResponse): await provider.create_signed_upload(BUCKET,KEY,mime_type="image/png",upsert=True)
    assert len(calls)==2 and "synthetic-server-only-key" not in caplog.text


@pytest.mark.parametrize("value", [None,"https://evil.example.test/object/upload/sign/x?token=x",
    "https://[malformed/path?token=x",
    "//evil.example.test/x?token=x","/object/upload/sign/wrong?token=x","/object/upload/sign/{bucket}/{key}",
    "/object/upload/sign/{bucket}/{key}?token=","/object/upload/sign/{bucket}/{key}?token=x&token=y",
    "/object/upload/sign/{bucket}/{key}?token=x#frag"])
async def test_capability_protocol_and_ssrf_rejection(value):
    if value: value=value.format(bucket=BUCKET,key=KEY)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={"url":value}))) as client:
        with pytest.raises(StorageInvalidResponse):
            await SupabaseStorage(SETTINGS,client).create_signed_upload(BUCKET,KEY,mime_type="image/png")


@pytest.mark.parametrize("code,exception", [(404,StorageMissing),(409,StorageConflict),(408,StorageTimeout),
    (504,StorageTimeout),(429,StorageUnavailable),(500,StorageUnavailable),(403,StorageUnavailable),(302,StorageInvalidResponse)])
async def test_provider_status_normalization_no_raw_exception(code,exception):
    def handler(request): return httpx.Response(code,json={"message":"private provider diagnostics"},headers={"Location":"https://evil.example.test"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),follow_redirects=True) as client:
        with pytest.raises(exception) as error:
            await SupabaseStorage(SETTINGS,client).create_signed_upload(BUCKET,KEY,mime_type="image/png")
    assert str(error.value)==""


@pytest.mark.parametrize("transport_error,exception",[(httpx.ReadTimeout,StorageTimeout),(httpx.ConnectError,StorageUnavailable),
    (httpx.RemoteProtocolError,StorageInvalidResponse),(httpx.DecodingError,StorageInvalidResponse)])
async def test_transport_errors_sanitized(transport_error,exception):
    def handler(request): raise transport_error("private credential-looking diagnostic")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider=SupabaseStorage(SETTINGS,client)
        with pytest.raises(exception) as error: await provider.stat_object(BUCKET,KEY)
        assert str(error.value)==""
        with pytest.raises(exception): await provider.create_signed_download(BUCKET,KEY,expires_in=60)
        with pytest.raises(exception):
            async for _ in provider.stream_object(BUCKET,KEY): pass


async def test_stat_and_original_byte_stream_use_only_canonical_authenticated_path():
    calls=[]
    def handler(request):
        calls.append(request)
        assert request.url.path==f"/storage/v1/object/authenticated/{BUCKET}/{KEY}"
        if request.method=="HEAD": return httpx.Response(200,headers={"Content-Length":"3","Content-Type":"image/png"})
        return httpx.Response(200,content=b"abc")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider=SupabaseStorage(SETTINGS,client)
        metadata=await provider.stat_object(BUCKET,KEY)
        assert (metadata.bucket,metadata.key,metadata.byte_size)==(BUCKET,KEY,3)
        assert b"".join([chunk async for chunk in provider.stream_object(BUCKET,KEY)])==b"abc"
        with pytest.raises(StorageInvalidResponse): await provider.stat_object("other",KEY)
        with pytest.raises(StorageInvalidResponse): await provider.stat_object(BUCKET,"https://evil.example.test")
    assert len(calls)==2


@pytest.mark.parametrize("body", [b"not json",b"[]",b"x"*70_000], ids=["malformed", "array", "oversized"])
async def test_unexpected_json_is_protocol_error(body):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,content=body))) as client:
        with pytest.raises(StorageInvalidResponse):
            await SupabaseStorage(SETTINGS,client).create_signed_upload(BUCKET,KEY,mime_type="image/png")


async def test_malformed_provider_error_shape_is_sanitized_protocol_failure():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(400, json={"code": ["private diagnostic"]}))) as client:
        with pytest.raises(StorageInvalidResponse) as error:
            await SupabaseStorage(SETTINGS, client).create_signed_upload(BUCKET, KEY, mime_type="image/png")
    assert str(error.value) == ""
