"""Create the approved Night Club AI v1.0 schema.

This revision is the executable translation of sql/001_initial_schema.sql.
The sql/ files remain frozen design references and are never read or executed by
Alembic. Role creation, grants, and RLS in sql/003_rls.sql are deliberately
excluded: an environment administrator applies that one-time provisioning script.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision: str = "20260907_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = pg.UUID(as_uuid=True)
NOW = sa.text("now()")
UUID_DEFAULT = sa.text("gen_random_uuid()")
EMPTY_JSON = sa.text("'{}'::jsonb")


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    ]


def _enum(name: str, *values: str) -> pg.ENUM:
    value = pg.ENUM(*values, name=name, create_type=False)
    value.create(op.get_bind(), checkfirst=False)
    return value


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    member_role = _enum("member_role", "owner", "manager", "editor", "reviewer", "operator", "viewer")
    platform_type = _enum("platform_type", "facebook", "whatsapp", "instagram")
    connection_status = _enum("connection_status", "active", "expired", "revoked", "error", "pending")
    campaign_status = _enum("campaign_status", "draft", "active", "paused", "completed", "archived")
    content_status = _enum("content_status", "draft", "in_review", "changes_requested", "approved", "scheduled", "publishing", "published", "failed", "cancelled")
    asset_kind = _enum("asset_kind", "image", "video", "document")
    publication_status = _enum("publication_status", "pending", "leased", "publishing", "succeeded", "retryable_failure", "permanent_failure", "cancelled")
    webhook_status = _enum("webhook_status", "received", "processed", "ignored", "failed")
    outbox_status = _enum("outbox_status", "pending", "leased", "delivered", "retryable_failure", "dead_letter")
    ai_status = _enum("ai_status", "queued", "running", "succeeded", "failed", "cancelled")
    whatsapp_direction = _enum("whatsapp_direction", "inbound", "outbound")

    op.execute("""CREATE FUNCTION public.set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN NEW.updated_at = now(); RETURN NEW; END; $$""")

    op.create_table("organizations",
        sa.Column("id", UUID, primary_key=True, server_default=UUID_DEFAULT), sa.Column("name", sa.Text, nullable=False),
        sa.Column("slug", pg.CITEXT, nullable=False), sa.Column("timezone", sa.Text, nullable=False, server_default="America/Mexico_City"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()), *_timestamps(),
        sa.UniqueConstraint("slug"), schema="public")
    op.create_table("profiles",
        sa.Column("id", UUID, primary_key=True), sa.Column("display_name", sa.Text), sa.Column("email", pg.CITEXT),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()), sa.Column("last_seen_at", sa.DateTime(timezone=True)), *_timestamps(),
        sa.ForeignKeyConstraint(["id"], ["auth.users.id"], onupdate="RESTRICT", ondelete="RESTRICT"), sa.UniqueConstraint("email"), schema="public")
    op.create_table("organization_members",
        sa.Column("organization_id", UUID, primary_key=True), sa.Column("user_id", UUID, primary_key=True),
        sa.Column("role", member_role, nullable=False), sa.Column("created_by", UUID), *_timestamps(),
        sa.ForeignKeyConstraint(["organization_id"], ["public.organizations.id"], onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["public.profiles.id"], onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["public.profiles.id"], onupdate="RESTRICT", ondelete="RESTRICT"), schema="public")
    op.create_table("platform_connections",
        sa.Column("id", UUID, primary_key=True, server_default=UUID_DEFAULT), sa.Column("organization_id", UUID, nullable=False),
        sa.Column("platform", platform_type, nullable=False), sa.Column("external_account_id", sa.Text, nullable=False),
        sa.Column("display_name", sa.Text, nullable=False), sa.Column("capabilities", pg.JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("credentials_ciphertext", sa.LargeBinary, nullable=False), sa.Column("credential_key_version", sa.SmallInteger, nullable=False),
        sa.Column("token_expires_at", sa.DateTime(timezone=True)), sa.Column("status", connection_status, nullable=False, server_default="pending"),
        sa.Column("last_verified_at", sa.DateTime(timezone=True)), sa.Column("last_error_code", sa.Text), sa.Column("last_error_at", sa.DateTime(timezone=True)), *_timestamps(),
        sa.ForeignKeyConstraint(["organization_id"], ["public.organizations.id"], onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.UniqueConstraint("organization_id", "platform", "external_account_id"), sa.UniqueConstraint("id", "organization_id", "platform"), schema="public")
    op.create_table("campaigns",
        sa.Column("id", UUID, primary_key=True, server_default=UUID_DEFAULT), sa.Column("organization_id", UUID, nullable=False),
        sa.Column("name", sa.Text, nullable=False), sa.Column("objective", sa.Text), sa.Column("brief", pg.JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("starts_at", sa.DateTime(timezone=True)), sa.Column("ends_at", sa.DateTime(timezone=True)),
        sa.Column("status", campaign_status, nullable=False, server_default="draft"), sa.Column("created_by", UUID, nullable=False), *_timestamps(),
        sa.CheckConstraint("ends_at IS NULL OR starts_at IS NULL OR ends_at >= starts_at", name="campaigns_valid_date_range"),
        sa.ForeignKeyConstraint(["organization_id"], ["public.organizations.id"], onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["public.profiles.id"], onupdate="RESTRICT", ondelete="RESTRICT"), schema="public")
    op.create_table("assets",
        sa.Column("id", UUID, primary_key=True, server_default=UUID_DEFAULT), sa.Column("organization_id", UUID, nullable=False),
        sa.Column("storage_bucket", sa.Text, nullable=False), sa.Column("storage_key", sa.Text, nullable=False), sa.Column("kind", asset_kind, nullable=False),
        sa.Column("mime_type", sa.Text, nullable=False), sa.Column("byte_size", sa.BigInteger, nullable=False), sa.Column("sha256", sa.CHAR(64), nullable=False),
        sa.Column("original_filename", sa.Text), sa.Column("width", sa.Integer), sa.Column("height", sa.Integer), sa.Column("duration_ms", sa.Integer),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"), sa.Column("deleted_at", sa.DateTime(timezone=True)), sa.Column("uploaded_by", UUID, nullable=False), *_timestamps(),
        sa.CheckConstraint("byte_size > 0"), sa.CheckConstraint("status IN ('pending','ready','rejected','deleted')"),
        sa.CheckConstraint("(status = 'deleted') = (deleted_at IS NOT NULL)", name="assets_deleted_state"),
        sa.ForeignKeyConstraint(["organization_id"], ["public.organizations.id"], onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["uploaded_by"], ["public.profiles.id"], onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.UniqueConstraint("storage_key"), sa.UniqueConstraint("organization_id", "sha256"), sa.UniqueConstraint("id", "organization_id"), schema="public")
    op.create_table("content_items",
        sa.Column("id", UUID, primary_key=True, server_default=UUID_DEFAULT), sa.Column("organization_id", UUID, nullable=False),
        sa.Column("campaign_id", UUID), sa.Column("platform", platform_type, nullable=False), sa.Column("connection_id", UUID),
        sa.Column("status", content_status, nullable=False, server_default="draft"), sa.Column("current_version_no", sa.Integer, nullable=False, server_default="1"),
        sa.Column("approved_version_no", sa.Integer), sa.Column("scheduled_for", sa.DateTime(timezone=True)), sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("external_post_id", sa.Text), sa.Column("last_error_code", sa.Text), sa.Column("last_error_message", sa.Text), sa.Column("created_by", UUID, nullable=False), *_timestamps(),
        sa.CheckConstraint("platform IN ('facebook','whatsapp')"), sa.CheckConstraint("current_version_no > 0"),
        sa.CheckConstraint("approved_version_no IS NULL OR approved_version_no <= current_version_no", name="content_items_approved_version_is_current_or_older"),
        sa.ForeignKeyConstraint(["organization_id"], ["public.organizations.id"], onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["campaign_id"], ["public.campaigns.id"], onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["public.profiles.id"], onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["connection_id", "organization_id", "platform"], ["public.platform_connections.id", "public.platform_connections.organization_id", "public.platform_connections.platform"], onupdate="RESTRICT", ondelete="RESTRICT", name="content_items_connection_same_organization_and_platform"),
        sa.UniqueConstraint("id", "organization_id"), schema="public")
    op.create_table("ai_generation_requests",
        sa.Column("id", UUID, primary_key=True, server_default=UUID_DEFAULT), sa.Column("organization_id", UUID, nullable=False), sa.Column("content_item_id", UUID),
        sa.Column("provider", sa.Text, nullable=False), sa.Column("model", sa.Text, nullable=False), sa.Column("prompt_template_key", sa.Text, nullable=False),
        sa.Column("prompt_template_version", sa.Integer, nullable=False), sa.Column("input_redacted", pg.JSONB, nullable=False), sa.Column("output", pg.JSONB),
        sa.Column("provider_request_id", sa.Text), sa.Column("input_tokens", sa.Integer), sa.Column("output_tokens", sa.Integer), sa.Column("estimated_cost_usd", sa.Numeric(12,6)),
        sa.Column("status", ai_status, nullable=False, server_default="queued"), sa.Column("error_code", sa.Text), sa.Column("created_by", UUID, nullable=False), *_timestamps(),
        sa.CheckConstraint("prompt_template_version > 0"), sa.CheckConstraint("input_tokens IS NULL OR input_tokens >= 0"),
        sa.CheckConstraint("output_tokens IS NULL OR output_tokens >= 0"), sa.CheckConstraint("estimated_cost_usd IS NULL OR estimated_cost_usd >= 0"),
        sa.ForeignKeyConstraint(["organization_id"], ["public.organizations.id"], onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["content_item_id", "organization_id"], ["public.content_items.id", "public.content_items.organization_id"], onupdate="RESTRICT", ondelete="RESTRICT", name="ai_generation_requests_content_same_organization"),
        sa.ForeignKeyConstraint(["created_by"], ["public.profiles.id"], onupdate="RESTRICT", ondelete="RESTRICT"), sa.UniqueConstraint("id", "organization_id"), schema="public")
    op.create_table("content_versions",
        sa.Column("id", UUID, primary_key=True, server_default=UUID_DEFAULT), sa.Column("content_item_id", UUID, nullable=False), sa.Column("version_no", sa.Integer, nullable=False),
        sa.Column("body", sa.Text, nullable=False), sa.Column("title", sa.Text), sa.Column("link_url", sa.Text), sa.Column("payload", pg.JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("source", sa.Text, nullable=False), sa.Column("ai_generation_id", UUID), sa.Column("created_by", UUID, nullable=False), *_timestamps(),
        sa.CheckConstraint("version_no > 0"), sa.CheckConstraint("source IN ('manual','ai','import')"),
        sa.ForeignKeyConstraint(["content_item_id"], ["public.content_items.id"], onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["ai_generation_id"], ["public.ai_generation_requests.id"], onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["public.profiles.id"], onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.UniqueConstraint("content_item_id", "version_no"), sa.UniqueConstraint("content_item_id", "id"), schema="public")
    op.create_table("content_assets",
        sa.Column("content_version_id", UUID, primary_key=True), sa.Column("asset_id", UUID, primary_key=True), sa.Column("content_item_id", UUID, nullable=False),
        sa.Column("organization_id", UUID, nullable=False), sa.Column("position", sa.SmallInteger, nullable=False, server_default="0"), *_timestamps(),
        sa.CheckConstraint("position >= 0"), sa.UniqueConstraint("content_version_id", "position"),
        sa.ForeignKeyConstraint(["content_item_id", "content_version_id"], ["public.content_versions.content_item_id", "public.content_versions.id"], onupdate="RESTRICT", ondelete="CASCADE", name="content_assets_version_belongs_to_item"),
        sa.ForeignKeyConstraint(["content_item_id", "organization_id"], ["public.content_items.id", "public.content_items.organization_id"], onupdate="RESTRICT", ondelete="RESTRICT", name="content_assets_item_same_organization"),
        sa.ForeignKeyConstraint(["asset_id", "organization_id"], ["public.assets.id", "public.assets.organization_id"], onupdate="RESTRICT", ondelete="RESTRICT", name="content_assets_asset_same_organization"), schema="public")
    op.create_table("review_decisions",
        sa.Column("id", UUID, primary_key=True, server_default=UUID_DEFAULT), sa.Column("content_item_id", UUID, nullable=False), sa.Column("content_version_id", UUID, nullable=False),
        sa.Column("decision", sa.Text, nullable=False), sa.Column("comment", sa.Text), sa.Column("decided_by", UUID, nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW), *_timestamps(),
        sa.CheckConstraint("decision IN ('approved','changes_requested')"),
        sa.ForeignKeyConstraint(["content_item_id", "content_version_id"], ["public.content_versions.content_item_id", "public.content_versions.id"], onupdate="RESTRICT", ondelete="RESTRICT", name="review_decisions_version_belongs_to_item"),
        sa.ForeignKeyConstraint(["decided_by"], ["public.profiles.id"], onupdate="RESTRICT", ondelete="RESTRICT"), schema="public")
    op.create_table("publication_jobs",
        sa.Column("id", UUID, primary_key=True, server_default=UUID_DEFAULT), sa.Column("content_item_id", UUID, nullable=False), sa.Column("content_version_id", UUID, nullable=False),
        sa.Column("idempotency_key", UUID, nullable=False), sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", publication_status, nullable=False, server_default="pending"), sa.Column("attempt_count", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("lease_token", UUID), sa.Column("lease_expires_at", sa.DateTime(timezone=True)), sa.Column("published_external_id", sa.Text),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)), sa.Column("created_by", UUID, nullable=False), *_timestamps(),
        sa.CheckConstraint("attempt_count >= 0"), sa.UniqueConstraint("idempotency_key"),
        sa.ForeignKeyConstraint(["content_item_id"], ["public.content_items.id"], onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["content_item_id", "content_version_id"], ["public.content_versions.content_item_id", "public.content_versions.id"], onupdate="RESTRICT", ondelete="RESTRICT", name="publication_jobs_version_belongs_to_item"),
        sa.ForeignKeyConstraint(["created_by"], ["public.profiles.id"], onupdate="RESTRICT", ondelete="RESTRICT"), schema="public")
    op.create_table("publication_attempts",
        sa.Column("id", UUID, primary_key=True, server_default=UUID_DEFAULT), sa.Column("publication_job_id", UUID, nullable=False), sa.Column("attempt_no", sa.SmallInteger, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False), sa.Column("finished_at", sa.DateTime(timezone=True)), sa.Column("request_fingerprint", sa.CHAR(64), nullable=False),
        sa.Column("provider_request_id", sa.Text), sa.Column("provider_response", pg.JSONB), sa.Column("outcome", sa.Text, nullable=False),
        sa.Column("error_code", sa.Text), sa.Column("error_message", sa.Text), *_timestamps(),
        sa.CheckConstraint("attempt_no > 0"), sa.CheckConstraint("outcome IN ('succeeded','retryable_failure','permanent_failure')"),
        sa.CheckConstraint("finished_at IS NULL OR finished_at >= started_at", name="publication_attempts_valid_finish_time"),
        sa.ForeignKeyConstraint(["publication_job_id"], ["public.publication_jobs.id"], onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.UniqueConstraint("publication_job_id", "attempt_no"), schema="public")
    op.create_table("webhook_events",
        sa.Column("id", UUID, primary_key=True, server_default=UUID_DEFAULT), sa.Column("organization_id", UUID), sa.Column("source", sa.Text, nullable=False),
        sa.Column("event_key", sa.Text, nullable=False), sa.Column("headers", pg.JSONB, nullable=False), sa.Column("payload", pg.JSONB, nullable=False),
        sa.Column("payload_sha256", sa.CHAR(64), nullable=False), sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("status", webhook_status, nullable=False, server_default="received"), sa.Column("resolution_status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("processed_at", sa.DateTime(timezone=True)), sa.Column("error_code", sa.Text), *_timestamps(),
        sa.CheckConstraint("source IN ('meta')"), sa.CheckConstraint("resolution_status IN ('pending','resolved','quarantined')"),
        sa.ForeignKeyConstraint(["organization_id"], ["public.organizations.id"], onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.UniqueConstraint("source", "event_key"), sa.UniqueConstraint("source", "payload_sha256"), schema="public")
    op.create_table("whatsapp_conversations",
        sa.Column("id", UUID, primary_key=True, server_default=UUID_DEFAULT), sa.Column("organization_id", UUID, nullable=False), sa.Column("connection_id", UUID, nullable=False),
        sa.Column("wa_id", sa.Text, nullable=False), sa.Column("contact_name", sa.Text), sa.Column("last_message_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.Text, nullable=False, server_default="open"), *_timestamps(),
        sa.ForeignKeyConstraint(["organization_id"], ["public.organizations.id"], onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["connection_id"], ["public.platform_connections.id"], onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.UniqueConstraint("connection_id", "wa_id"), schema="public")
    op.create_table("whatsapp_messages",
        sa.Column("id", UUID, primary_key=True, server_default=UUID_DEFAULT), sa.Column("conversation_id", UUID, nullable=False),
        sa.Column("direction", whatsapp_direction, nullable=False), sa.Column("meta_message_id", sa.Text), sa.Column("message_type", sa.Text, nullable=False),
        sa.Column("body", pg.JSONB, nullable=False), sa.Column("delivery_status", sa.Text), sa.Column("sent_by", UUID),
        sa.Column("sent_at", sa.DateTime(timezone=True)), sa.Column("received_at", sa.DateTime(timezone=True)), *_timestamps(),
        sa.ForeignKeyConstraint(["conversation_id"], ["public.whatsapp_conversations.id"], onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["sent_by"], ["public.profiles.id"], onupdate="RESTRICT", ondelete="RESTRICT"), sa.UniqueConstraint("meta_message_id"), schema="public")
    op.create_table("outbox_events",
        sa.Column("id", UUID, primary_key=True, server_default=UUID_DEFAULT), sa.Column("organization_id", UUID), sa.Column("aggregate_type", sa.Text, nullable=False),
        sa.Column("aggregate_id", UUID, nullable=False), sa.Column("event_type", sa.Text, nullable=False), sa.Column("payload", pg.JSONB, nullable=False),
        sa.Column("idempotency_key", UUID, nullable=False), sa.Column("status", outbox_status, nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.SmallInteger, nullable=False, server_default="0"), sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("lease_token", UUID), sa.Column("lease_expires_at", sa.DateTime(timezone=True)), sa.Column("delivered_at", sa.DateTime(timezone=True)), sa.Column("last_error", sa.Text), *_timestamps(),
        sa.CheckConstraint("attempt_count >= 0"), sa.ForeignKeyConstraint(["organization_id"], ["public.organizations.id"], onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.UniqueConstraint("idempotency_key"), schema="public")
    op.create_table("automation_runs",
        sa.Column("id", UUID, primary_key=True, server_default=UUID_DEFAULT), sa.Column("outbox_event_id", UUID), sa.Column("workflow_name", sa.Text, nullable=False),
        sa.Column("n8n_execution_id", sa.Text), sa.Column("status", sa.Text, nullable=False), sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)), sa.Column("details", pg.JSONB, nullable=False, server_default=EMPTY_JSON), *_timestamps(),
        sa.CheckConstraint("status IN ('started','succeeded','failed')"), sa.CheckConstraint("finished_at IS NULL OR finished_at >= started_at", name="automation_runs_valid_finish_time"),
        sa.ForeignKeyConstraint(["outbox_event_id"], ["public.outbox_events.id"], onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.UniqueConstraint("workflow_name", "n8n_execution_id"), schema="public")
    op.create_table("idempotency_keys",
        sa.Column("key", UUID, primary_key=True), sa.Column("organization_id", UUID), sa.Column("actor_id", UUID), sa.Column("operation", sa.Text, nullable=False),
        sa.Column("request_hash", sa.CHAR(64), nullable=False), sa.Column("response_status", sa.Integer), sa.Column("response_body", pg.JSONB),
        sa.Column("state", sa.Text, nullable=False), sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False), *_timestamps(),
        sa.CheckConstraint("state IN ('in_progress','completed','failed')"),
        sa.ForeignKeyConstraint(["organization_id"], ["public.organizations.id"], onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["public.profiles.id"], onupdate="RESTRICT", ondelete="RESTRICT"), schema="public")
    op.create_table("audit_logs",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True), sa.Column("organization_id", UUID), sa.Column("actor_type", sa.Text, nullable=False),
        sa.Column("actor_id", sa.Text), sa.Column("action", sa.Text, nullable=False), sa.Column("entity_type", sa.Text, nullable=False), sa.Column("entity_id", UUID),
        sa.Column("correlation_id", UUID), sa.Column("ip", pg.INET), sa.Column("user_agent", sa.Text), sa.Column("before", pg.JSONB), sa.Column("after", pg.JSONB), *_timestamps(),
        sa.CheckConstraint("actor_type IN ('user','system','n8n','meta')"),
        sa.ForeignKeyConstraint(["organization_id"], ["public.organizations.id"], onupdate="RESTRICT", ondelete="RESTRICT"), schema="public")

    op.execute("COMMENT ON TABLE public.assets IS 'Assets are soft-deleted by setting status=deleted and deleted_at; physical object deletion is deferred by retention policy.'")
    op.execute("COMMENT ON COLUMN public.audit_logs.before IS 'Never store credentials, tokens, secrets, or complete WhatsApp message bodies in audit snapshots.'")
    op.execute("COMMENT ON COLUMN public.audit_logs.after IS 'Never store credentials, tokens, secrets, or complete WhatsApp message bodies in audit snapshots.'")
    op.execute("COMMENT ON COLUMN public.webhook_events.resolution_status IS 'Pending/quarantine state for safe tenant resolution; use quarantined when no unique organization/connection can be identified.'")

    op.execute("""CREATE FUNCTION public.enforce_content_asset_ready() RETURNS trigger LANGUAGE plpgsql AS $$ DECLARE asset_status text; BEGIN SELECT status INTO asset_status FROM public.assets WHERE id = NEW.asset_id AND organization_id = NEW.organization_id; IF asset_status IS DISTINCT FROM 'ready' THEN RAISE EXCEPTION 'content assets must reference a ready asset'; END IF; RETURN NEW; END; $$""")
    op.execute("""CREATE FUNCTION public.prevent_attached_asset_unavailability() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.status <> 'ready' AND EXISTS (SELECT 1 FROM public.content_assets WHERE asset_id = NEW.id) THEN RAISE EXCEPTION 'an attached asset cannot transition away from ready'; END IF; RETURN NEW; END; $$""")
    op.execute("""CREATE FUNCTION public.enforce_facebook_publication_job() RETURNS trigger LANGUAGE plpgsql AS $$ DECLARE item_platform public.platform_type; BEGIN SELECT platform INTO item_platform FROM public.content_items WHERE id = NEW.content_item_id; IF item_platform IS DISTINCT FROM 'facebook' THEN RAISE EXCEPTION 'publication jobs are limited to Facebook content in v1'; END IF; RETURN NEW; END; $$""")
    op.execute("CREATE TRIGGER content_assets_require_ready_asset BEFORE INSERT OR UPDATE OF asset_id, organization_id ON public.content_assets FOR EACH ROW EXECUTE FUNCTION public.enforce_content_asset_ready()")
    op.execute("CREATE TRIGGER assets_prevent_unavailable_attached_asset BEFORE UPDATE OF status ON public.assets FOR EACH ROW EXECUTE FUNCTION public.prevent_attached_asset_unavailability()")
    op.execute("CREATE TRIGGER publication_jobs_require_facebook_content BEFORE INSERT OR UPDATE OF content_item_id ON public.publication_jobs FOR EACH ROW EXECUTE FUNCTION public.enforce_facebook_publication_job()")

    tables = ["organizations", "profiles", "organization_members", "platform_connections", "campaigns", "assets", "content_items", "ai_generation_requests", "content_versions", "content_assets", "review_decisions", "publication_jobs", "publication_attempts", "webhook_events", "whatsapp_conversations", "whatsapp_messages", "outbox_events", "automation_runs", "idempotency_keys", "audit_logs"]
    for table in tables:
        op.execute(f"CREATE TRIGGER {table}_set_updated_at BEFORE UPDATE ON public.{table} FOR EACH ROW EXECUTE FUNCTION public.set_updated_at()")


def downgrade() -> None:
    raise NotImplementedError("rollback via compensating migration, not destructive schema reversal")
