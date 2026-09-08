from datetime import UTC, datetime
from uuid import uuid4

from backend.app.modules.content.schemas import ContentRead
from backend.app.modules.integrations.schemas import PlatformConnectionRead
from backend.app.platform.enums import ContentStatus


def test_content_schema_serializes_camel_case() -> None:
    schema = ContentRead(
        id=uuid4(), status=ContentStatus.DRAFT, current_version_no=2,
        approved_version_no=None, platform="facebook", created_at=datetime.now(UTC)
    )

    payload = schema.model_dump(by_alias=True)
    assert payload["currentVersionNo"] == 2
    assert "current_version_no" not in payload


def test_connection_output_never_contains_credentials_ciphertext() -> None:
    source = {
        "id": uuid4(), "organization_id": uuid4(), "platform": "facebook",
        "external_account_id": "page", "display_name": "Night Club",
        "capabilities": {}, "status": "active", "token_expires_at": None,
        "last_verified_at": None, "credentials_ciphertext": b"must-not-leak",
    }

    payload = PlatformConnectionRead.model_validate(source).model_dump(by_alias=True)
    assert "credentialsCiphertext" not in payload
    assert "credentials_ciphertext" not in payload
