"""Create the approved Night Club AI v1.0 indexes.

Executable translation of sql/002_indexes.sql; the frozen design file is not
read or executed by this revision.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260907_0002"
down_revision: str | None = "20260907_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


INDEXES: list[tuple[str, str, list[str]]] = [
    ("idx_organization_members_user_organization", "organization_members", ["user_id", "organization_id"]),
    ("idx_organization_members_created_by", "organization_members", ["created_by"]),
    ("idx_platform_connections_organization_platform_status", "platform_connections", ["organization_id", "platform", "status"]),
    ("idx_campaigns_organization_status_starts_at", "campaigns", ["organization_id", "status", "starts_at"]),
    ("idx_campaigns_created_by", "campaigns", ["created_by"]),
    ("idx_assets_organization_status", "assets", ["organization_id", "status"]),
    ("idx_assets_uploaded_by", "assets", ["uploaded_by"]),
    ("idx_content_items_organization_status_scheduled_for", "content_items", ["organization_id", "status", "scheduled_for"]),
    ("idx_content_items_campaign_id", "content_items", ["campaign_id"]),
    ("idx_content_items_connection_id", "content_items", ["connection_id"]),
    ("idx_content_items_created_by", "content_items", ["created_by"]),
    ("idx_ai_generation_requests_organization_created_at", "ai_generation_requests", ["organization_id", sa.text("created_at DESC")]),
    ("idx_ai_generation_requests_content_item_organization", "ai_generation_requests", ["content_item_id", "organization_id"]),
    ("idx_ai_generation_requests_created_by", "ai_generation_requests", ["created_by"]),
    ("idx_content_versions_ai_generation_id", "content_versions", ["ai_generation_id"]),
    ("idx_content_versions_created_by", "content_versions", ["created_by"]),
    ("idx_content_assets_content_item_organization", "content_assets", ["content_item_id", "organization_id"]),
    ("idx_content_assets_asset_organization", "content_assets", ["asset_id", "organization_id"]),
    ("idx_review_decisions_content_item_decided_at", "review_decisions", ["content_item_id", sa.text("decided_at DESC")]),
    ("idx_review_decisions_content_item_version", "review_decisions", ["content_item_id", "content_version_id"]),
    ("idx_review_decisions_decided_by", "review_decisions", ["decided_by"]),
    ("idx_publication_jobs_due", "publication_jobs", ["status", "scheduled_for", "next_attempt_at"]),
    ("idx_publication_jobs_content_item_version", "publication_jobs", ["content_item_id", "content_version_id"]),
    ("idx_publication_jobs_created_by", "publication_jobs", ["created_by"]),
    ("idx_publication_attempts_job_id", "publication_attempts", ["publication_job_id"]),
    ("idx_webhook_events_organization_received_at", "webhook_events", ["organization_id", sa.text("received_at DESC")]),
    ("idx_webhook_events_resolution_status_received_at", "webhook_events", ["resolution_status", "received_at"]),
    ("idx_whatsapp_conversations_organization_last_message_at", "whatsapp_conversations", ["organization_id", sa.text("last_message_at DESC")]),
    ("idx_whatsapp_conversations_connection_id", "whatsapp_conversations", ["connection_id"]),
    ("idx_whatsapp_messages_conversation_received_at", "whatsapp_messages", ["conversation_id", sa.text("received_at DESC")]),
    ("idx_whatsapp_messages_sent_by", "whatsapp_messages", ["sent_by"]),
    ("idx_outbox_events_status_available_at", "outbox_events", ["status", "available_at"]),
    ("idx_outbox_events_organization_id", "outbox_events", ["organization_id"]),
    ("idx_automation_runs_outbox_event_id", "automation_runs", ["outbox_event_id"]),
    ("idx_idempotency_keys_expires_at", "idempotency_keys", ["expires_at"]),
    ("idx_idempotency_keys_organization_actor", "idempotency_keys", ["organization_id", "actor_id"]),
    ("idx_idempotency_keys_actor_id", "idempotency_keys", ["actor_id"]),
    ("idx_audit_logs_organization_created_at", "audit_logs", ["organization_id", sa.text("created_at DESC")]),
    ("idx_audit_logs_entity", "audit_logs", ["entity_type", "entity_id", sa.text("created_at DESC")]),
    ("idx_audit_logs_correlation_id", "audit_logs", ["correlation_id"]),
]


def upgrade() -> None:
    for name, table, columns in INDEXES:
        op.create_index(name, table, columns, schema="public")
    op.create_index(
        "uq_publication_jobs_one_active_job_per_content_item",
        "publication_jobs",
        ["content_item_id"],
        unique=True,
        schema="public",
        postgresql_where=sa.text("status IN ('pending', 'leased', 'publishing')"),
    )


def downgrade() -> None:
    op.drop_index("uq_publication_jobs_one_active_job_per_content_item", table_name="publication_jobs", schema="public")
    for name, table, _columns in reversed(INDEXES):
        op.drop_index(name, table_name=table, schema="public")
