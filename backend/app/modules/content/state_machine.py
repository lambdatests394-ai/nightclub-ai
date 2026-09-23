"""Pure application service for the approved content lifecycle."""

from dataclasses import dataclass, field
from uuid import UUID

from backend.app.platform.enums import ContentStatus


class InvalidContentTransition(ValueError):
    """Raised when an operation is invalid for the current content state."""


@dataclass
class ContentState:
    content_id: UUID
    created_by: UUID
    status: ContentStatus = ContentStatus.DRAFT
    current_version_no: int = 1
    approved_version_no: int | None = None
    review_decisions: list["ReviewDecisionEffect"] = field(default_factory=list)


@dataclass(frozen=True)
class ReviewDecisionEffect:
    content_id: UUID
    version_no: int
    decision: str
    decided_by: UUID
    comment: str | None


class ContentStateMachine:
    """Applies state transitions without persistence or HTTP concerns."""

    @staticmethod
    async def submit_for_review(content: ContentState) -> None:
        ContentStateMachine._require(content, ContentStatus.DRAFT)
        content.status = ContentStatus.IN_REVIEW

    @staticmethod
    async def review(content: ContentState, *, decision: str, decided_by: UUID, comment: str | None = None, reviewer_role: str | None = None) -> ReviewDecisionEffect:
        ContentStateMachine._require(content, ContentStatus.IN_REVIEW)
        if decided_by == content.created_by and not (reviewer_role == "owner" and comment and comment.strip()):
            raise InvalidContentTransition("content creators cannot review their own content")
        if decision == "approved":
            content.status = ContentStatus.APPROVED
            content.approved_version_no = content.current_version_no
        elif decision == "changes_requested":
            content.status = ContentStatus.CHANGES_REQUESTED
            content.approved_version_no = None
        else:
            raise InvalidContentTransition("unsupported review decision")
        effect = ReviewDecisionEffect(content.content_id, content.current_version_no, decision, decided_by, comment)
        content.review_decisions.append(effect)
        return effect

    @staticmethod
    async def edit(content: ContentState) -> int:
        """Create the next version; an approved edit intentionally resets approval."""
        if content.status not in {ContentStatus.DRAFT, ContentStatus.CHANGES_REQUESTED, ContentStatus.APPROVED}:
            raise InvalidContentTransition("content cannot be edited in its current state")
        content.current_version_no += 1
        content.approved_version_no = None
        content.status = ContentStatus.DRAFT
        return content.current_version_no

    @staticmethod
    async def schedule(content: ContentState) -> None:
        ContentStateMachine._require(content, ContentStatus.APPROVED)
        content.status = ContentStatus.SCHEDULED

    @staticmethod
    async def cancel_schedule(content: ContentState) -> None:
        """ADR-003: cancelling a job preserves approved content for rescheduling."""
        ContentStateMachine._require(content, ContentStatus.SCHEDULED)
        content.status = ContentStatus.APPROVED

    @staticmethod
    async def begin_publishing(content: ContentState) -> None:
        ContentStateMachine._require(content, ContentStatus.SCHEDULED)
        content.status = ContentStatus.PUBLISHING

    @staticmethod
    async def mark_published(content: ContentState) -> None:
        ContentStateMachine._require(content, ContentStatus.PUBLISHING)
        content.status = ContentStatus.PUBLISHED

    @staticmethod
    async def mark_permanent_failure(content: ContentState) -> None:
        """ADR-003: permanent provider failure requires deliberate human recovery."""
        ContentStateMachine._require(content, ContentStatus.PUBLISHING)
        content.status = ContentStatus.FAILED

    @staticmethod
    def _require(content: ContentState, expected: ContentStatus) -> None:
        if content.status != expected:
            raise InvalidContentTransition(f"expected {expected}, got {content.status}")
