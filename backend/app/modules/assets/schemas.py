from datetime import datetime
from uuid import UUID
from typing import Literal
from pydantic import ConfigDict, Field, model_validator
from backend.app.shared.schemas import ApiSchema
from backend.app.modules.assets.filenames import sanitize_filename


class UploadIntent(ApiSchema):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(min_length=1, max_length=255)
    mime_type: Literal["image/jpeg", "image/png", "image/webp"]
    byte_size: int = Field(strict=True, gt=0, le=10_485_760)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)

    @model_validator(mode="after")
    def safe_name(self):
        sanitize_filename(self.filename, self.mime_type)
        return self


class AssetRead(ApiSchema):
    id: UUID
    kind: str
    mime_type: str
    byte_size: int
    original_filename: str | None
    status: str
    created_at: datetime
    width: int | None = None
    height: int | None = None
