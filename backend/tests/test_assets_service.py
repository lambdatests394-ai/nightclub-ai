"""Asset lifecycle, bounded image validation and explicit commit/capability ordering."""
import asyncio
from dataclasses import replace
from io import BytesIO
from uuid import UUID, uuid4

from PIL import Image, PngImagePlugin
import pytest

from backend.app.modules.assets.errors import AssetConflict, AssetNotFound, AssetProtocolError, AssetUnavailable
from backend.app.modules.assets.storage import StorageConflict, StorageInvalidResponse, StorageMissing, StorageTimeout, StorageUnavailable
from backend.app.modules.assets.verification import decode_image, verify_object
from backend.app.modules.identity.errors import Forbidden
from backend.app.modules.identity.policy import MemberRole
from backend.app.shared.errors import IdempotencyConflict
from backend.tests.assets_fakes import AssetFixture, FakeStorage, intent, picture

pytestmark = pytest.mark.anyio


@pytest.fixture
def case():
    return AssetFixture()


async def pending(case, body=None):
    _, data = await case.coordinator.upload(uuid4(), body or intent())
    return UUID(data["asset"]["id"])


async def test_commit_before_sign_and_replay_ephemeral_never_persisted(case):
    original = case.provider.create_signed_upload
    async def sign(*args, **kwargs):
        assert case.events[-1] == "commit"
        return await original(*args, **kwargs)
    case.provider.create_signed_upload = sign
    key = uuid4()
    first = await case.coordinator.upload(key, intent())
    second = await case.coordinator.upload(key, intent())
    assert first[0] == second[0] == 201
    assert first[1]["asset"] == second[1]["asset"]
    assert first[1]["upload"]["url"] != second[1]["upload"]["url"]
    assert len(case.repository.rows) == 1 and case.audit.write.await_count == 1
    durable = case.idempotency.rows[key][2][1]
    assert set(durable) == {"asset"} and "correlationId" not in str(durable) and "token" not in str(durable)


async def test_commit_failure_prevents_signing_and_rolls_back(case):
    case.fail_commit = True
    with pytest.raises(ConnectionError):
        await case.coordinator.upload(uuid4(), intent())
    assert not case.provider.uploads and not case.repository.rows and not case.idempotency.rows


@pytest.mark.parametrize("error", [StorageUnavailable, StorageTimeout, StorageMissing, StorageInvalidResponse, StorageConflict])
async def test_signing_failure_keeps_durable_pending_and_replay_recovers(case, error):
    key = uuid4()
    case.provider.sign_error = error
    with pytest.raises(AssetUnavailable):
        await case.coordinator.upload(key, intent())
    assert len(case.repository.rows) == 1 and case.idempotency.rows[key][2][0] == 201
    case.provider.sign_error = None
    assert (await case.coordinator.upload(key, intent()))[0] == 201
    assert case.audit.write.await_count == 1


@pytest.mark.parametrize("state", ["pending", "rejected", "deleted"])
async def test_hash_duplicate_conflict(case, state):
    asset_id = await pending(case)
    case.repository.rows[asset_id].status = state
    with pytest.raises(AssetConflict):
        await case.coordinator.upload(uuid4(), intent())


async def test_ready_dedup_no_upload_or_audit_and_incompatible_conflicts(case):
    asset_id = await pending(case)
    await case.coordinator.complete(asset_id, uuid4())
    result = await case.coordinator.upload(uuid4(), intent())
    assert result[0] == 200 and result[1]["upload"] is None
    assert len(case.provider.uploads) == 1 and case.audit.write.await_count == 2
    bad = intent().model_copy(update={"byte_size": 1})
    with pytest.raises(AssetConflict):
        await case.coordinator.upload(uuid4(), bad)
    with pytest.raises(AssetConflict):
        await case.coordinator.upload(uuid4(), intent().model_copy(update={"mime_type": "image/jpeg"}))


async def test_hash_dedup_never_exposes_foreign_tenant(case):
    asset_id = await pending(case)
    case.repository.rows[asset_id].organization_id = uuid4()
    other = await pending(case)
    assert other != asset_id


@pytest.mark.parametrize("fmt,mime", [("JPEG", "image/jpeg"), ("PNG", "image/png"), ("WEBP", "image/webp")])
async def test_complete_original_bytes_success_and_durable_replay(case, fmt, mime):
    case.provider.data = picture(fmt)
    asset_id = await pending(case, intent(case.provider.data, mime))
    key = uuid4()
    first = await case.coordinator.complete(asset_id, key)
    assert first[1]["asset"]["status"] == "ready"
    assert (first[1]["asset"]["width"], first[1]["asset"]["height"]) == (4, 3)
    assert await case.coordinator.complete(asset_id, key) == first
    assert case.provider.reads == 1 and case.audit.write.await_count == 2
    assert case.provider.stream_closed
    assert (await case.coordinator.download(asset_id))["downloadUrl"]
    assert case.provider.downloads[-1][2] == 60
    with pytest.raises(AssetConflict):
        await case.coordinator.complete(asset_id, uuid4())


@pytest.mark.parametrize("error,expected", [(StorageMissing, AssetConflict), (StorageTimeout, AssetUnavailable),
    (StorageUnavailable, AssetUnavailable), (StorageInvalidResponse, AssetProtocolError), (StorageConflict, AssetConflict)])
async def test_temporary_completion_errors_rollback_no_durable_result(case, error, expected):
    asset_id = await pending(case)
    key = uuid4()
    case.provider.error = error
    with pytest.raises(expected):
        await case.coordinator.complete(asset_id, key)
    assert key not in case.idempotency.rows and case.repository.rows[asset_id].status == "pending"


@pytest.mark.parametrize("kind,code", [("size", "size_mismatch"), ("sha", "sha256_mismatch"),
    ("mime", "mime_mismatch"), ("malformed", "invalid_image"), ("truncated", "invalid_image"),
    ("gif", "unsupported_format"), ("width", "dimension_limit_exceeded"), ("pixels", "dimension_limit_exceeded")])
async def test_terminal_verification_rejections_durable(case, kind, code):
    data, body = picture(), intent()
    if kind == "size": body = body.model_copy(update={"byte_size": 1})
    if kind == "sha": body = body.model_copy(update={"sha256": "0" * 64})
    if kind == "mime": body = intent(data, "image/jpeg")
    if kind == "malformed": data = b"not an image"; body = intent(data)
    if kind == "truncated": data = picture("JPEG")[:-15]; body = intent(data, "image/jpeg")
    if kind == "gif": data = picture("GIF"); body = intent(data)
    if kind == "width": data = picture(size=(8193, 1)); body = intent(data)
    if kind == "pixels": data = picture(size=(5000, 4001)); body = intent(data)
    case.provider.data = data
    asset_id = await pending(case, body)
    key = uuid4()
    result = await case.coordinator.complete(asset_id, key)
    assert result[0] == 200 and result[1]["asset"]["status"] == "rejected"
    assert result[1]["rejectionCode"] == code
    assert result[1]["asset"]["width"] is None and result[1]["asset"]["height"] is None
    assert await case.coordinator.complete(asset_id, key) == result
    assert case.audit.write.await_count == 2
    with pytest.raises(AssetConflict): await case.coordinator.download(asset_id)


async def test_absolute_stream_limit_and_metadata_not_trusted():
    storage = FakeStorage(b"x" * 8192)
    result = await verify_object(storage, bucket="bucket", key="key", expected_size=1, expected_sha="0"*64,
        expected_mime="image/png", max_bytes=4096, timeout=15)
    assert result.rejection_code == "size_limit_exceeded" and storage.stream_closed


@pytest.mark.parametrize("fmt", ["PNG", "WEBP"])
def test_animation_rejected(fmt):
    buf = BytesIO()
    Image.new("RGB", (3, 3), "red").save(buf, format=fmt, save_all=True,
        append_images=[Image.new("RGB", (3, 3), "blue")], duration=100, loop=0)
    assert decode_image(buf.getvalue(), "image/png" if fmt == "PNG" else "image/webp").rejection_code == "animated_image"


def test_known_gps_rejected_benign_exif_accepted():
    exif = Image.Exif(); exif[0x010F] = "Synthetic camera"
    assert decode_image(picture("JPEG", exif=exif), "image/jpeg").rejection_code is None
    exif[0x8825] = {1: "N", 2: (1.0, 2.0, 3.0)}
    assert decode_image(picture("JPEG", exif=exif), "image/jpeg").rejection_code == "gps_metadata_present"
    info = PngImagePlugin.PngInfo(); info.add_text("XML:com.adobe.xmp", '<exif:GPSLatitude>1</exif:GPSLatitude>')
    assert decode_image(picture(pnginfo=info), "image/png").rejection_code == "gps_metadata_present"
    info = PngImagePlugin.PngInfo(); info.add_text("GPS", "synthetic position")
    assert decode_image(picture(pnginfo=info), "image/png").rejection_code == "gps_metadata_present"


async def test_wrong_location_is_protocol_error(case):
    asset_id = await pending(case)
    original = case.provider.stat_object
    async def wrong(*args): return replace(await original(*args), key="wrong")
    case.provider.stat_object = wrong
    with pytest.raises(AssetProtocolError): await case.coordinator.complete(asset_id, uuid4())


async def test_timeout_and_cancellation_do_not_persist_completion(case):
    asset_id = await pending(case)
    started = asyncio.Event()
    async def blocked(*args):
        started.set()
        await asyncio.Event().wait()
    case.provider.stat_object = blocked
    key = uuid4()
    task = asyncio.create_task(case.coordinator.complete(asset_id, key))
    await started.wait(); task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert case.repository.rows[asset_id].status == "pending" and key not in case.idempotency.rows
    case.service.settings = case.settings.model_copy(update={"asset_verification_timeout_seconds": .01})
    with pytest.raises(AssetUnavailable): await case.coordinator.complete(asset_id, uuid4())


async def test_replay_still_requires_authorization_and_request_fingerprint(case):
    key = uuid4()
    await case.coordinator.upload(key, intent())
    with pytest.raises(IdempotencyConflict):
        await case.coordinator.upload(key, intent(filename="different.png"))
    case.service.context = replace(case.context, role=MemberRole.VIEWER)
    with pytest.raises(Forbidden): await case.coordinator.upload(key, intent())


@pytest.mark.parametrize("state,error", [("pending", AssetConflict), ("rejected", AssetConflict), ("deleted", AssetNotFound)])
async def test_download_terminal_and_pending_restrictions(case, state, error):
    asset_id = await pending(case)
    case.repository.rows[asset_id].status = state
    with pytest.raises(error): await case.coordinator.download(asset_id)
    assert not case.provider.downloads


async def test_foreign_absent_hidden_and_canonical_destination_checked(case):
    asset_id = await pending(case)
    case.repository.rows[asset_id].storage_key = "arbitrary/path"
    with pytest.raises(AssetProtocolError): await case.coordinator.complete(asset_id, uuid4())
    case.repository.rows[asset_id].organization_id = uuid4()
    with pytest.raises(AssetNotFound): await case.coordinator.complete(asset_id, uuid4())
    with pytest.raises(AssetNotFound): await case.coordinator.download(uuid4())


async def test_completion_commit_failure_rolls_back_verified_result(case):
    asset_id = await pending(case)
    key = uuid4()
    case.fail_commit = True
    with pytest.raises(ConnectionError): await case.coordinator.complete(asset_id, key)
    assert case.repository.rows[asset_id].status == "pending" and key not in case.idempotency.rows


async def test_exact_ten_mebibyte_bound_stops_stream():
    maximum = 10_485_760
    storage = FakeStorage(b"x" * (maximum + 1))
    result = await verify_object(storage, bucket="bucket", key="key", expected_size=maximum,
        expected_sha="0"*64, expected_mime="image/png", max_bytes=maximum, timeout=15)
    assert result.rejection_code == "size_limit_exceeded" and storage.stream_closed


def test_decompression_bomb_protection_is_not_disabled(monkeypatch):
    # Lower only this test's Pillow threshold so a tiny synthetic image triggers it.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 5)
    assert decode_image(picture(size=(4, 3)), "image/png").rejection_code == "dimension_limit_exceeded"


@pytest.mark.parametrize("kind", ["video", "document"])
async def test_reserved_database_media_not_enabled_through_download_or_dedup(case, kind):
    asset_id = await pending(case)
    case.repository.rows[asset_id].kind = kind
    case.repository.rows[asset_id].status = "ready"
    with pytest.raises(AssetConflict): await case.coordinator.download(asset_id)
    with pytest.raises(AssetConflict): await case.coordinator.upload(uuid4(), intent())
