-- NIGHT CLUB AI v1.0 - reviewed index design only.
-- Primary-key and UNIQUE constraint indexes are declared in 001_initial_schema.sql.
-- This file adds indexes for every foreign-key access path not already covered
-- and for the documented high-frequency operational queries.
-- This file has not been executed against any database.

BEGIN;

CREATE INDEX idx_organization_members_user_organization
    ON public.organization_members (user_id, organization_id);
CREATE INDEX idx_organization_members_created_by
    ON public.organization_members (created_by);

CREATE INDEX idx_platform_connections_organization_platform_status
    ON public.platform_connections (organization_id, platform, status);

CREATE INDEX idx_campaigns_organization_status_starts_at
    ON public.campaigns (organization_id, status, starts_at);
CREATE INDEX idx_campaigns_created_by
    ON public.campaigns (created_by);

CREATE INDEX idx_assets_organization_status
    ON public.assets (organization_id, status);
CREATE INDEX idx_assets_uploaded_by
    ON public.assets (uploaded_by);

CREATE INDEX idx_content_items_organization_status_scheduled_for
    ON public.content_items (organization_id, status, scheduled_for);
CREATE INDEX idx_content_items_campaign_id
    ON public.content_items (campaign_id);
CREATE INDEX idx_content_items_connection_id
    ON public.content_items (connection_id);
CREATE INDEX idx_content_items_created_by
    ON public.content_items (created_by);

CREATE INDEX idx_ai_generation_requests_organization_created_at
    ON public.ai_generation_requests (organization_id, created_at DESC);
CREATE INDEX idx_ai_generation_requests_content_item_organization
    ON public.ai_generation_requests (content_item_id, organization_id);
CREATE INDEX idx_ai_generation_requests_created_by
    ON public.ai_generation_requests (created_by);

CREATE INDEX idx_content_versions_ai_generation_id
    ON public.content_versions (ai_generation_id);
CREATE INDEX idx_content_versions_created_by
    ON public.content_versions (created_by);

CREATE INDEX idx_content_assets_content_item_organization
    ON public.content_assets (content_item_id, organization_id);
CREATE INDEX idx_content_assets_asset_organization
    ON public.content_assets (asset_id, organization_id);

CREATE INDEX idx_review_decisions_content_item_decided_at
    ON public.review_decisions (content_item_id, decided_at DESC);
CREATE INDEX idx_review_decisions_content_item_version
    ON public.review_decisions (content_item_id, content_version_id);
CREATE INDEX idx_review_decisions_decided_by
    ON public.review_decisions (decided_by);

CREATE INDEX idx_publication_jobs_due
    ON public.publication_jobs (status, scheduled_for, next_attempt_at);
CREATE INDEX idx_publication_jobs_content_item_version
    ON public.publication_jobs (content_item_id, content_version_id);
CREATE INDEX idx_publication_jobs_created_by
    ON public.publication_jobs (created_by);
CREATE UNIQUE INDEX uq_publication_jobs_one_active_job_per_content_item
    ON public.publication_jobs (content_item_id)
    WHERE status IN ('pending', 'leased', 'publishing');

CREATE INDEX idx_publication_attempts_job_id
    ON public.publication_attempts (publication_job_id);

CREATE INDEX idx_webhook_events_organization_received_at
    ON public.webhook_events (organization_id, received_at DESC);
CREATE INDEX idx_webhook_events_resolution_status_received_at
    ON public.webhook_events (resolution_status, received_at);

CREATE INDEX idx_whatsapp_conversations_organization_last_message_at
    ON public.whatsapp_conversations (organization_id, last_message_at DESC);
CREATE INDEX idx_whatsapp_conversations_connection_id
    ON public.whatsapp_conversations (connection_id);
CREATE INDEX idx_whatsapp_messages_conversation_received_at
    ON public.whatsapp_messages (conversation_id, received_at DESC);
CREATE INDEX idx_whatsapp_messages_sent_by
    ON public.whatsapp_messages (sent_by);

CREATE INDEX idx_outbox_events_status_available_at
    ON public.outbox_events (status, available_at);
CREATE INDEX idx_outbox_events_organization_id
    ON public.outbox_events (organization_id);

CREATE INDEX idx_automation_runs_outbox_event_id
    ON public.automation_runs (outbox_event_id);
CREATE INDEX idx_idempotency_keys_expires_at
    ON public.idempotency_keys (expires_at);
CREATE INDEX idx_idempotency_keys_organization_actor
    ON public.idempotency_keys (organization_id, actor_id);
CREATE INDEX idx_idempotency_keys_actor_id
    ON public.idempotency_keys (actor_id);

CREATE INDEX idx_audit_logs_organization_created_at
    ON public.audit_logs (organization_id, created_at DESC);
CREATE INDEX idx_audit_logs_entity
    ON public.audit_logs (entity_type, entity_id, created_at DESC);
CREATE INDEX idx_audit_logs_correlation_id
    ON public.audit_logs (correlation_id);

COMMIT;
