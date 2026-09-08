from datetime import datetime
from uuid import UUID
from backend.app.shared.schemas import ApiSchema


class WebhookEventRead(ApiSchema):
    id: UUID
    organization_id: UUID | None
    source: str
    event_key: str
    status: str
    resolution_status: str
    received_at: datetime
