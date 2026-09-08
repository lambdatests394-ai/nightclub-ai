from uuid import UUID
from backend.app.shared.schemas import ApiSchema


class AIGenerationCreate(ApiSchema):
    content_item_id: UUID | None = None
    provider: str
    prompt_template_key: str
    brief: dict


class AIGenerationRead(ApiSchema):
    id: UUID
    content_item_id: UUID | None
    provider: str
    model: str
    output: dict | None
    status: str
