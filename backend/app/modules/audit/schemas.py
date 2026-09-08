from datetime import datetime
from uuid import UUID
from backend.app.shared.schemas import ApiSchema


class AuditLogRead(ApiSchema):
    id: int
    organization_id: UUID | None
    actor_type: str
    actor_id: str | None
    action: str
    entity_type: str
    entity_id: UUID | None
    correlation_id: UUID | None
    created_at: datetime
