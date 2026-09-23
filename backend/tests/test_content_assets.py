"""Prompt 8 ordered attachment snapshots; Prompt 7 lifecycle is preserved."""
from uuid import UUID, uuid4
import pytest
from pydantic import ValidationError
from backend.app.modules.content.errors import ContentConflict, ContentReferenceNotFound, InvalidContentRequest
from backend.app.modules.content.schemas import ContentCreate, ContentPatch
from backend.app.platform.enums import ContentStatus
from backend.app.shared.idempotency import fingerprint
from backend.tests.test_content_api import service, draft

pytestmark=pytest.mark.anyio


@pytest.mark.parametrize("schema", [ContentCreate, ContentPatch])
@pytest.mark.parametrize("value", [None, "not-a-list", [str(uuid4())]*2, [str(uuid4()) for _ in range(11)]])
def test_asset_ids_invalid(schema,value):
    payload={"assetIds":value}
    if schema is ContentCreate: payload.update(platform="facebook",body="x")
    with pytest.raises(ValidationError): schema.model_validate(payload)


async def test_post_omitted_assets_is_empty(service):
    content_id=await draft(service)
    assert (await service.read(content_id))["assetIds"]==[]


@pytest.mark.parametrize("action", ["inherit", "empty", "replace", "reorder"])
async def test_patch_snapshot_semantics_and_history(service,action):
    a,b,c=uuid4(),uuid4(),uuid4()
    service.repository.assets.update({a:"ready",b:"ready",c:"ready"})
    content_id=await draft(service,asset_ids=[a,b])
    old=service.repository.versions[content_id,1]
    payload={"title":"new"} if action=="inherit" else {"asset_ids":{"empty":[],"replace":[c],"reorder":[b,a]}[action]}
    expected=[a,b] if action=="inherit" else payload["asset_ids"]
    result=await service.mutate("patch",uuid4(),payload,content_id)
    assert result[1]["assetIds"]==[str(v) for v in expected] and result[1]["currentVersionNo"]==2
    assert await service.repository.asset_ids(old.id)==[a,b]
    assert old.title is None
    assert service.audit.write.call_args.kwargs["asset_count"]==len(expected)
    if action!="inherit": assert "assetIds" in service.audit.write.call_args.kwargs["changed_fields"]


async def test_identical_order_noop_rejected_and_approved_edit_returns_draft(service):
    asset_id=uuid4(); service.repository.assets[asset_id]="ready"
    content_id=await draft(service,asset_ids=[asset_id])
    with pytest.raises(InvalidContentRequest):
        await service.mutate("patch",uuid4(),{"asset_ids":[asset_id]},content_id)
    item=service.repository.items[content_id]; item.status=ContentStatus.APPROVED; item.approved_version_no=1
    result=await service.mutate("patch",uuid4(),{"body":"edited"},content_id)
    assert result[1]["status"]=="draft" and result[1]["approvedVersionNo"] is None
    assert result[1]["assetIds"]==[str(asset_id)]


@pytest.mark.parametrize("status", ["missing","pending","rejected","deleted"])
async def test_asset_reference_validation(service,status):
    asset_id=uuid4()
    if status!="missing": service.repository.assets[asset_id]=status
    with pytest.raises(ContentReferenceNotFound if status=="missing" else ContentConflict):
        await draft(service,asset_ids=[asset_id])


async def test_historical_prompt7_idempotency_replayed_without_rewriting(service):
    key=uuid4(); payload=ContentCreate(platform="facebook",body="historical").model_dump()
    old_payload={k:v for k,v in payload.items() if k!="asset_ids"}
    operation="content:create:collection"
    digest=fingerprint(operation,{"organization":str(service.context.organization_id),"actor":str(service.context.user_id),"payload":old_payload})
    historical={"id":str(uuid4()),"body":"historical","status":"draft"}
    service.idempotency.rows[key]=(operation,digest,(201,historical))
    assert await service.mutate("create",key,payload)==(201,historical)
    assert "assetIds" not in historical and service.repository.items=={}
    service.audit.write.assert_not_awaited()
