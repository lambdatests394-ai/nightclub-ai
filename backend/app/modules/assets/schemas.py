from datetime import datetime
from uuid import UUID
from backend.app.shared.schemas import ApiSchema


class AssetRead(ApiSchema):
    id: UUID
    kind: str
    mime_type: str
    byte_size: int
    original_filename: str | None
    status: str
    created_at: datetime
