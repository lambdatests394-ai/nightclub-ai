"""Prompt 7 only: drafts, immutable versions and human review."""
from datetime import UTC, datetime
from uuid import uuid4

from backend.app.modules.content.errors import ContentConflict, ContentNotFound, ContentReferenceNotFound, InvalidContentRequest
from backend.app.modules.content.models import ContentVersion
from backend.app.modules.content.schemas import ContentCurrentRead
from backend.app.modules.content.state_machine import ContentState, ContentStateMachine, InvalidContentTransition
from backend.app.modules.content.workflow_service import ContentWorkflowService
from backend.app.modules.identity.policy import Permission, require_permission
from backend.app.modules.identity.service import decode_cursor, encode_cursor
from backend.app.platform.enums import ContentStatus
from backend.app.shared.idempotency import fingerprint


class ContentService:
    def __init__(self, context, repository, idempotency, audit):
        self.context, self.repository, self.idempotency, self.audit = context, repository, idempotency, audit

    @staticmethod
    def serialize(row):
        item, version = row
        return ContentCurrentRead(
            id=item.id, campaign_id=item.campaign_id, connection_id=item.connection_id,
            platform=item.platform, status=item.status, current_version_no=item.current_version_no,
            approved_version_no=item.approved_version_no, body=version.body, title=version.title,
            link_url=version.link_url, created_by=item.created_by,
            created_at=item.created_at, updated_at=item.updated_at,
        ).model_dump(mode="json", by_alias=True)

    async def read(self, content_id):
        require_permission(self.context, Permission.CONTENT_READ)
        row = await self.repository.current(content_id)
        if row is None:
            raise ContentNotFound()
        return self.serialize(row)

    async def page(self, cursor, limit):
        require_permission(self.context, Permission.CONTENT_READ)
        if not 1 <= limit <= 100:
            raise InvalidContentRequest()
        rows = await self.repository.page(decode_cursor(cursor), limit + 1)
        page = rows[:limit]
        return [self.serialize(row) for row in page], encode_cursor(page[-1][0].id) if len(rows) > limit else None

    async def check_campaign(self, campaign_id):
        if campaign_id is not None:
            campaign = await self.repository.campaign(campaign_id)
            if campaign is None:
                raise ContentReferenceNotFound()
            if campaign.status == "archived":
                raise ContentConflict()

    async def mutate(self, action, key, payload, content_id=None):
        if action not in {"create", "patch", "submit-review", "review"}:
            raise InvalidContentRequest()
        require_permission(self.context, Permission.CONTENT_REVIEW if action == "review" else Permission.CONTENT_WRITE)
        operation = f"content:{action}:{content_id or 'collection'}"
        semantic = {"organization": str(self.context.organization_id), "actor": str(self.context.user_id),
                    "payload": {k: str(v) if k in {"campaign_id", "connection_id"} and v is not None else v for k, v in payload.items()}}
        replay = await self.idempotency.claim(key, operation, fingerprint(operation, semantic))
        if replay is not None:
            return replay
        try:
            if action == "create":
                content_id = uuid4()
                await self.check_campaign(payload["campaign_id"])
                if payload["connection_id"] is not None:
                    connection = await self.repository.connection(payload["connection_id"])
                    if connection is None:
                        raise ContentReferenceNotFound()
                    if connection.platform != payload["platform"] or connection.status != "active":
                        raise ContentConflict()
                await self.repository.create_item(content_id=content_id, actor_id=self.context.user_id,
                    campaign_id=payload["campaign_id"], platform=payload["platform"], connection_id=payload["connection_id"])
                state = ContentState(content_id, self.context.user_id)
                await self.repository.add_content_version(ContentVersion(
                    id=uuid4(), content_item_id=content_id, version_no=1, body=payload["body"],
                    title=payload["title"], link_url=payload["link_url"], source="manual", payload={},
                    created_by=self.context.user_id,
                ))
                await self.audit.write("content.created", state, changed_fields=("body", "title", "link_url"))
            else:
                item = await self.repository.get_item(content_id)
                if item is None:
                    raise ContentNotFound()
                await self.check_campaign(item.campaign_id)
                row = await self.repository.current(content_id)
                if row is None:
                    # Missing current version is storage corruption, not a new draft.
                    from backend.app.modules.identity.errors import IdentityUnavailable
                    raise IdentityUnavailable()
                version = row[1]
                state = ContentState(item.id, item.created_by, item.status, item.current_version_no, item.approved_version_no)
                previous = str(state.status)
                workflow = ContentWorkflowService(self.repository)
                if action == "patch":
                    if state.status not in {ContentStatus.DRAFT, ContentStatus.CHANGES_REQUESTED, ContentStatus.APPROVED}:
                        raise ContentConflict()
                    if not payload:
                        raise InvalidContentRequest()
                    values = {field: getattr(version, field) for field in ("body", "title", "link_url")}
                    changed = [field for field, value in payload.items() if value != values[field]]
                    if not changed:
                        raise InvalidContentRequest()
                    values.update(payload)
                    await workflow.edit(state, created_by=self.context.user_id, **values)
                    await self.audit.write("content.version_created", state, previous_status=previous, changed_fields=changed)
                elif action == "submit-review":
                    await ContentStateMachine.submit_for_review(state)
                    await self.repository.save_content_state(state)
                    await self.audit.write("content.submitted_for_review", state, previous_status=previous)
                else:
                    if payload["version_no"] != state.current_version_no:
                        raise ContentConflict()
                    override = state.created_by == self.context.user_id and self.context.role == "owner"
                    await workflow.review(state, content_version_id=version.id, decision=payload["decision"],
                        decided_by=self.context.user_id, decided_at=datetime.now(UTC), comment=payload["comment"],
                        reviewer_role=self.context.role)
                    action_name = "content.approved" if payload["decision"] == "approved" else "content.changes_requested"
                    await self.audit.write(action_name, state, previous_status=previous, owner_override=override)
        except InvalidContentTransition:
            raise ContentConflict() from None
        result = self.serialize(await self.repository.current(content_id))
        status = 201 if action == "create" else 200
        await self.idempotency.complete(key, status, result)
        return status, result
