import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

from backend.app.modules.content.state_machine import ContentState
from backend.app.modules.content.workflow_service import ContentWorkflowService
from backend.app.platform.enums import ContentStatus


def test_review_persists_decision_and_state_in_one_unit_of_work() -> None:
    uow = AsyncMock()
    service = ContentWorkflowService(uow)
    content = ContentState(uuid4(), uuid4(), ContentStatus.IN_REVIEW)

    record = asyncio.run(service.review(
        content, content_version_id=uuid4(), decision="approved", decided_by=uuid4(), decided_at=datetime.now(UTC)
    ))

    assert record.decision == "approved"
    uow.add_review_decision.assert_called_once_with(record)
    uow.save_content_state.assert_called_once_with(content)
    uow.commit.assert_called_once_with()


def test_editing_approved_content_persists_new_draft_version() -> None:
    uow = AsyncMock()
    content = ContentState(uuid4(), uuid4(), ContentStatus.APPROVED, approved_version_no=1)

    version = asyncio.run(ContentWorkflowService(uow).edit(content, body="updated", created_by=uuid4()))

    assert version.version_no == 2
    assert content.status is ContentStatus.DRAFT
    assert content.approved_version_no is None
    uow.add_content_version.assert_called_once_with(version)
    uow.commit.assert_called_once_with()


def test_scheduling_approved_content_persists_publication_job() -> None:
    uow = AsyncMock()
    content = ContentState(uuid4(), uuid4(), ContentStatus.APPROVED, approved_version_no=1)

    job = asyncio.run(ContentWorkflowService(uow).schedule(
        content, content_version_id=uuid4(), scheduled_for=datetime.now(UTC),
        scheduled_by=uuid4(), idempotency_key=uuid4(),
    ))

    assert content.status is ContentStatus.SCHEDULED
    assert job.status == "pending"
    uow.add_publication_job.assert_called_once_with(job)
    uow.commit.assert_called_once_with()
