from datetime import datetime
from uuid import UUID
from backend.app.shared.schemas import ApiSchema


class PublicationJobRead(ApiSchema):
    id: UUID
    content_item_id: UUID
    content_version_id: UUID
    scheduled_for: datetime
    status: str
    attempt_count: int
