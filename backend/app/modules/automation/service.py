"""Transactional scheduling core; it persists work but never contacts providers."""

from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from backend.app.modules.automation.models import PublicationJob
from backend.app.modules.automation.schemas import PublicationActionRead
from backend.app.modules.content.errors import ContentConflict, ContentNotFound, InvalidContentRequest
from backend.app.modules.content.state_machine import ContentState, ContentStateMachine, InvalidContentTransition
from backend.app.modules.identity.policy import Permission, require_permission
from backend.app.platform.enums import ContentStatus
from backend.app.shared.idempotency import fingerprint


class PublicationService:
    def __init__(self, context, content_repository, publication_repository,
                 connection_repository, idempotency, audit, *, clock=None):
        self.context = context
        self.content_repository = content_repository
        self.publication_repository = publication_repository
        self.connection_repository = connection_repository
        self.idempotency = idempotency
        self.audit = audit
        self.clock = clock or (lambda: datetime.now(UTC))

    def _semantic(self, content_id, **extra):
        return {
            "organization": str(self.context.organization_id),
            "actor": str(self.context.user_id),
            "contentId": str(content_id),
            **extra,
        }

    @staticmethod
    def _state(item):
        return ContentState(
            item.id, item.created_by, item.status,
            item.current_version_no, item.approved_version_no,
        )

    @staticmethod
    def _safe_link(link_url):
        if link_url is None:
            return
        try:
            parsed = urlsplit(link_url)
            hostname = parsed.hostname
        except (TypeError, UnicodeError, ValueError):
            raise ContentConflict() from None
        if (not isinstance(link_url, str) or parsed.scheme != "https" or not hostname
                or parsed.username is not None or parsed.password is not None
                or any(ch.isspace() or ord(ch) < 32 for ch in link_url)):
            raise ContentConflict()

    @staticmethod
    def _constraint_name(error):
        original = getattr(error, "orig", None)
        for candidate in (original, getattr(original, "__cause__", None)):
            diagnostic = getattr(candidate, "diag", None)
            name = getattr(diagnostic, "constraint_name", None)
            if name:
                return name
            name = getattr(candidate, "constraint_name", None)
            if name:
                return name
        return None

    async def _locked_content(self, content_id):
        item = await self.content_repository.get_item(content_id)
        if item is None:
            raise ContentNotFound()
        return item

    async def _validated_enqueue_inputs(self, item, requested_version, now):
        if item.platform != "facebook" or item.status != ContentStatus.APPROVED:
            raise ContentConflict()
        if (item.approved_version_no is None
                or item.approved_version_no != item.current_version_no
                or requested_version != item.current_version_no):
            raise ContentConflict()
        if item.connection_id is None:
            raise ContentConflict()
        version = await self.content_repository.version(item.id, item.current_version_no)
        if version is None:
            raise ContentConflict()
        connection = await self.connection_repository.get_facebook_connection(item.connection_id)
        if (connection is None or connection.platform != "facebook"
                or connection.status != "active"
                or (connection.token_expires_at is not None
                    and connection.token_expires_at.astimezone(UTC) <= now)):
            raise ContentConflict()
        self._safe_link(version.link_url)
        if await self.content_repository.asset_ids(version.id):
            raise ContentConflict()
        if await self.publication_repository.active_for_content(item.id, lock=True) is not None:
            raise ContentConflict()
        return version

    @staticmethod
    def _response(item, job, *, scheduled_for):
        return PublicationActionRead(
            content_id=item.id, content_status=str(item.status),
            version_no=item.current_version_no, publication_job_id=job.id,
            publication_job_status=job.status, scheduled_for=scheduled_for,
            connection_id=item.connection_id,
        ).model_dump(mode="json", by_alias=True)

    async def schedule(self, content_id, key, request):
        require_permission(self.context, Permission.FACEBOOK_PUBLISH)
        now = self.clock().astimezone(UTC)
        scheduled_for = request.scheduled_for.astimezone(UTC)
        if scheduled_for <= now:
            raise InvalidContentRequest()
        operation = f"content:schedule:{content_id}"
        semantic = self._semantic(
            content_id, versionNo=request.version_no,
            scheduledFor=scheduled_for.isoformat(),
        )
        replay = await self.idempotency.claim(key, operation, fingerprint(operation, semantic))
        if replay is not None:
            return replay
        return await self._enqueue(
            content_id, key, request.version_no, scheduled_for, now=now,
            action="content.scheduled", immediate=False,
        )

    async def publish_now(self, content_id, key):
        require_permission(self.context, Permission.FACEBOOK_PUBLISH)
        operation = f"content:publish-now:{content_id}"
        semantic = self._semantic(content_id)
        replay = await self.idempotency.claim(key, operation, fingerprint(operation, semantic))
        if replay is not None:
            return replay
        scheduled_for = self.clock().astimezone(UTC)
        item = await self._locked_content(content_id)
        return await self._enqueue_locked(
            item, key, item.current_version_no, scheduled_for, now=scheduled_for,
            action="content.publish_requested", immediate=True,
        )

    async def _enqueue(self, content_id, key, version_no, scheduled_for, *, now, action, immediate):
        item = await self._locked_content(content_id)
        return await self._enqueue_locked(
            item, key, version_no, scheduled_for, now=now,
            action=action, immediate=immediate,
        )

    async def _enqueue_locked(self, item, key, version_no, scheduled_for, *, now, action, immediate):
        version = await self._validated_enqueue_inputs(item, version_no, now)
        state = self._state(item)
        previous = state.status
        try:
            await ContentStateMachine.schedule(state)
            job = PublicationJob(
                id=uuid4(), content_item_id=item.id, content_version_id=version.id,
                idempotency_key=key, scheduled_for=scheduled_for, status="pending",
                attempt_count=0, lease_token=None, lease_expires_at=None,
                published_external_id=None, next_attempt_at=None,
                created_by=self.context.user_id,
            )
            await self.publication_repository.add_pending(job)
            await self.content_repository.save_publication_state(state, scheduled_for)
        except InvalidContentTransition:
            raise ContentConflict() from None
        except IntegrityError as error:
            # Only the certified active-job uniqueness race is a domain conflict.
            # Other integrity failures remain unexpected database failures.
            constraint = self._constraint_name(error)
            if constraint != "uq_publication_jobs_one_active_job_per_content_item":
                raise
            raise ContentConflict() from None
        item.status = state.status
        item.scheduled_for = scheduled_for
        await self.audit.write(
            action, content_id=item.id, job_id=job.id, version_no=state.current_version_no,
            previous_status=previous, status=state.status, job_status=job.status,
            scheduled_for=scheduled_for, immediate=immediate,
        )
        body = self._response(item, job, scheduled_for=scheduled_for)
        await self.idempotency.complete(key, 201, body)
        return 201, body

    async def cancel_schedule(self, content_id, key):
        require_permission(self.context, Permission.FACEBOOK_PUBLISH)
        operation = f"content:cancel-schedule:{content_id}"
        replay = await self.idempotency.claim(
            key, operation, fingerprint(operation, self._semantic(content_id))
        )
        if replay is not None:
            return replay
        item = await self._locked_content(content_id)
        if item.status != ContentStatus.SCHEDULED:
            raise ContentConflict()
        job = await self.publication_repository.active_for_content(item.id, lock=True)
        if job is None or job.status != "pending":
            raise ContentConflict()
        state = self._state(item)
        previous = state.status
        try:
            await ContentStateMachine.cancel_schedule(state)
        except InvalidContentTransition:
            raise ContentConflict() from None
        if not await self.publication_repository.cancel_pending(job.id):
            raise ContentConflict()
        await self.content_repository.save_publication_state(state, None)
        job.status = "cancelled"
        item.status, item.scheduled_for = state.status, None
        await self.audit.write(
            "content.schedule_cancelled", content_id=item.id, job_id=job.id,
            version_no=state.current_version_no, previous_status=previous,
            status=state.status, job_status=job.status, scheduled_for=None,
            immediate=False,
        )
        body = self._response(item, job, scheduled_for=None)
        await self.idempotency.complete(key, 200, body)
        return 200, body
