from backend.app.core.database import Base
import backend.app.platform.model_registry  # noqa: F401


EXPECTED_TABLES = {
    "organizations", "profiles", "organization_members", "platform_connections", "campaigns",
    "assets", "content_items", "content_versions", "content_assets", "review_decisions",
    "publication_jobs", "publication_attempts", "ai_generation_requests", "webhook_events",
    "whatsapp_conversations", "whatsapp_messages", "outbox_events", "automation_runs",
    "idempotency_keys", "audit_logs",
}


def test_all_approved_tables_have_orm_mappings() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_platform_connection_output_model_has_no_credentials_field() -> None:
    from backend.app.modules.integrations.schemas import PlatformConnectionRead

    assert "credentials_ciphertext" not in PlatformConnectionRead.model_fields
