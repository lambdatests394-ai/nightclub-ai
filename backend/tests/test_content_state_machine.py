import asyncio
from uuid import uuid4

import pytest

from backend.app.modules.content.state_machine import ContentState, ContentStateMachine, InvalidContentTransition
from backend.app.platform.enums import ContentStatus


def content(status: ContentStatus = ContentStatus.DRAFT) -> ContentState:
    return ContentState(content_id=uuid4(), created_by=uuid4(), status=status)


def run(coroutine):
    return asyncio.run(coroutine)


def test_happy_path_to_published() -> None:
    item = content()
    run(ContentStateMachine.submit_for_review(item))
    effect = run(ContentStateMachine.review(item, decision="approved", decided_by=uuid4()))
    run(ContentStateMachine.schedule(item))
    run(ContentStateMachine.begin_publishing(item))
    run(ContentStateMachine.mark_published(item))

    assert effect.decision == "approved"
    assert item.status is ContentStatus.PUBLISHED


def test_changes_requested_returns_to_draft_when_edited() -> None:
    item = content(ContentStatus.IN_REVIEW)
    run(ContentStateMachine.review(item, decision="changes_requested", decided_by=uuid4()))
    version_no = run(ContentStateMachine.edit(item))

    assert item.status is ContentStatus.DRAFT
    assert version_no == 2
    assert len(item.review_decisions) == 1


def test_editing_approved_content_creates_new_draft_version() -> None:
    item = content(ContentStatus.APPROVED)
    item.approved_version_no = 1

    assert run(ContentStateMachine.edit(item)) == 2
    assert item.status is ContentStatus.DRAFT
    assert item.approved_version_no is None


def test_creator_cannot_review_own_content() -> None:
    item = content(ContentStatus.IN_REVIEW)

    with pytest.raises(InvalidContentTransition, match="cannot review"):
        run(ContentStateMachine.review(item, decision="approved", decided_by=item.created_by))


def test_invalid_schedule_is_rejected() -> None:
    with pytest.raises(InvalidContentTransition):
        run(ContentStateMachine.schedule(content(ContentStatus.DRAFT)))


@pytest.mark.parametrize("status", [state for state in ContentStatus if state is not ContentStatus.DRAFT])
def test_submit_review_rejects_every_other_state(status: ContentStatus) -> None:
    with pytest.raises(InvalidContentTransition):
        run(ContentStateMachine.submit_for_review(content(status)))


@pytest.mark.parametrize("status", [state for state in ContentStatus if state is not ContentStatus.IN_REVIEW])
def test_review_rejects_every_other_state(status: ContentStatus) -> None:
    with pytest.raises(InvalidContentTransition):
        run(ContentStateMachine.review(content(status), decision="approved", decided_by=uuid4()))


@pytest.mark.parametrize(
    "status", [state for state in ContentStatus if state not in {ContentStatus.DRAFT, ContentStatus.CHANGES_REQUESTED, ContentStatus.APPROVED}]
)
def test_edit_rejects_non_editable_states(status: ContentStatus) -> None:
    with pytest.raises(InvalidContentTransition):
        run(ContentStateMachine.edit(content(status)))


def test_invalid_review_decision_does_not_record_or_mutate() -> None:
    item = content(ContentStatus.IN_REVIEW)
    with pytest.raises(InvalidContentTransition):
        run(ContentStateMachine.review(item, decision="rejected", decided_by=uuid4()))
    assert item.status is ContentStatus.IN_REVIEW
    assert item.review_decisions == []


def test_cancel_schedule_preserves_approved_content() -> None:
    item = content(ContentStatus.SCHEDULED)
    run(ContentStateMachine.cancel_schedule(item))

    assert item.status is ContentStatus.APPROVED


def test_permanent_failure_is_terminal() -> None:
    item = content(ContentStatus.PUBLISHING)
    run(ContentStateMachine.mark_permanent_failure(item))

    assert item.status is ContentStatus.FAILED
    with pytest.raises(InvalidContentTransition):
        run(ContentStateMachine.schedule(item))


@pytest.mark.parametrize(
    ("operation", "required_status"),
    [
        (ContentStateMachine.schedule, ContentStatus.APPROVED),
        (ContentStateMachine.cancel_schedule, ContentStatus.SCHEDULED),
        (ContentStateMachine.begin_publishing, ContentStatus.SCHEDULED),
        (ContentStateMachine.mark_published, ContentStatus.PUBLISHING),
        (ContentStateMachine.mark_permanent_failure, ContentStatus.PUBLISHING),
    ],
)
def test_single_source_transitions_reject_all_other_states(operation, required_status: ContentStatus) -> None:
    for status in ContentStatus:
        if status is required_status:
            continue
        with pytest.raises(InvalidContentTransition):
            run(operation(content(status)))
