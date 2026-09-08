-- NIGHT CLUB AI v1.0 - reviewed role and RLS design only.
-- This file has not been executed against any database.
--
-- ADR-001 selects a dedicated, non-BYPASSRLS PostgreSQL role: nightclub_api.
-- Phase 2 scope intentionally grants this backend role full table access through
-- RLS while anon/authenticated remain denied. Organization-row policies based on
-- transaction-local request context are explicitly deferred by the approved
-- prompt and must be introduced before any direct multi-tenant client access.

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nightclub_api') THEN
        CREATE ROLE nightclub_api
            LOGIN
            NOINHERIT
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOREPLICATION
            NOBYPASSRLS;
    END IF;
END;
$$;

ALTER ROLE nightclub_api
    NOINHERIT
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOREPLICATION
    NOBYPASSRLS;

GRANT USAGE ON SCHEMA public TO nightclub_api;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
    public.organizations,
    public.profiles,
    public.organization_members,
    public.platform_connections,
    public.campaigns,
    public.assets,
    public.content_items,
    public.content_versions,
    public.content_assets,
    public.review_decisions,
    public.publication_jobs,
    public.publication_attempts,
    public.ai_generation_requests,
    public.webhook_events,
    public.whatsapp_conversations,
    public.whatsapp_messages,
    public.outbox_events,
    public.automation_runs,
    public.idempotency_keys,
    public.audit_logs
TO nightclub_api;
GRANT USAGE, SELECT ON SEQUENCE public.audit_logs_id_seq TO nightclub_api;

REVOKE ALL ON SCHEMA public FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON TABLE
    public.organizations,
    public.profiles,
    public.organization_members,
    public.platform_connections,
    public.campaigns,
    public.assets,
    public.content_items,
    public.content_versions,
    public.content_assets,
    public.review_decisions,
    public.publication_jobs,
    public.publication_attempts,
    public.ai_generation_requests,
    public.webhook_events,
    public.whatsapp_conversations,
    public.whatsapp_messages,
    public.outbox_events,
    public.automation_runs,
    public.idempotency_keys,
    public.audit_logs
FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON SEQUENCE public.audit_logs_id_seq FROM PUBLIC, anon, authenticated, service_role;

ALTER TABLE public.organizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.organization_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.platform_connections ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.campaigns ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.assets ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.content_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.content_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.content_assets ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.review_decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.publication_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.publication_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ai_generation_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.webhook_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.whatsapp_conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.whatsapp_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.outbox_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.automation_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.idempotency_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.audit_logs ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.organizations FORCE ROW LEVEL SECURITY;
ALTER TABLE public.profiles FORCE ROW LEVEL SECURITY;
ALTER TABLE public.organization_members FORCE ROW LEVEL SECURITY;
ALTER TABLE public.platform_connections FORCE ROW LEVEL SECURITY;
ALTER TABLE public.campaigns FORCE ROW LEVEL SECURITY;
ALTER TABLE public.assets FORCE ROW LEVEL SECURITY;
ALTER TABLE public.content_items FORCE ROW LEVEL SECURITY;
ALTER TABLE public.content_versions FORCE ROW LEVEL SECURITY;
ALTER TABLE public.content_assets FORCE ROW LEVEL SECURITY;
ALTER TABLE public.review_decisions FORCE ROW LEVEL SECURITY;
ALTER TABLE public.publication_jobs FORCE ROW LEVEL SECURITY;
ALTER TABLE public.publication_attempts FORCE ROW LEVEL SECURITY;
ALTER TABLE public.ai_generation_requests FORCE ROW LEVEL SECURITY;
ALTER TABLE public.webhook_events FORCE ROW LEVEL SECURITY;
ALTER TABLE public.whatsapp_conversations FORCE ROW LEVEL SECURITY;
ALTER TABLE public.whatsapp_messages FORCE ROW LEVEL SECURITY;
ALTER TABLE public.outbox_events FORCE ROW LEVEL SECURITY;
ALTER TABLE public.automation_runs FORCE ROW LEVEL SECURITY;
ALTER TABLE public.idempotency_keys FORCE ROW LEVEL SECURITY;
ALTER TABLE public.audit_logs FORCE ROW LEVEL SECURITY;

CREATE POLICY organizations_nightclub_api_only ON public.organizations
    FOR ALL TO nightclub_api USING (true) WITH CHECK (true);
CREATE POLICY profiles_nightclub_api_only ON public.profiles
    FOR ALL TO nightclub_api USING (true) WITH CHECK (true);
CREATE POLICY organization_members_nightclub_api_only ON public.organization_members
    FOR ALL TO nightclub_api USING (true) WITH CHECK (true);
CREATE POLICY platform_connections_nightclub_api_only ON public.platform_connections
    FOR ALL TO nightclub_api USING (true) WITH CHECK (true);
CREATE POLICY campaigns_nightclub_api_only ON public.campaigns
    FOR ALL TO nightclub_api USING (true) WITH CHECK (true);
CREATE POLICY assets_nightclub_api_only ON public.assets
    FOR ALL TO nightclub_api USING (true) WITH CHECK (true);
CREATE POLICY content_items_nightclub_api_only ON public.content_items
    FOR ALL TO nightclub_api USING (true) WITH CHECK (true);
CREATE POLICY content_versions_nightclub_api_only ON public.content_versions
    FOR ALL TO nightclub_api USING (true) WITH CHECK (true);
CREATE POLICY content_assets_nightclub_api_only ON public.content_assets
    FOR ALL TO nightclub_api USING (true) WITH CHECK (true);
CREATE POLICY review_decisions_nightclub_api_only ON public.review_decisions
    FOR ALL TO nightclub_api USING (true) WITH CHECK (true);
CREATE POLICY publication_jobs_nightclub_api_only ON public.publication_jobs
    FOR ALL TO nightclub_api USING (true) WITH CHECK (true);
CREATE POLICY publication_attempts_nightclub_api_only ON public.publication_attempts
    FOR ALL TO nightclub_api USING (true) WITH CHECK (true);
CREATE POLICY ai_generation_requests_nightclub_api_only ON public.ai_generation_requests
    FOR ALL TO nightclub_api USING (true) WITH CHECK (true);
CREATE POLICY webhook_events_nightclub_api_only ON public.webhook_events
    FOR ALL TO nightclub_api USING (true) WITH CHECK (true);
CREATE POLICY whatsapp_conversations_nightclub_api_only ON public.whatsapp_conversations
    FOR ALL TO nightclub_api USING (true) WITH CHECK (true);
CREATE POLICY whatsapp_messages_nightclub_api_only ON public.whatsapp_messages
    FOR ALL TO nightclub_api USING (true) WITH CHECK (true);
CREATE POLICY outbox_events_nightclub_api_only ON public.outbox_events
    FOR ALL TO nightclub_api USING (true) WITH CHECK (true);
CREATE POLICY automation_runs_nightclub_api_only ON public.automation_runs
    FOR ALL TO nightclub_api USING (true) WITH CHECK (true);
CREATE POLICY idempotency_keys_nightclub_api_only ON public.idempotency_keys
    FOR ALL TO nightclub_api USING (true) WITH CHECK (true);
CREATE POLICY audit_logs_nightclub_api_only ON public.audit_logs
    FOR ALL TO nightclub_api USING (true) WITH CHECK (true);

COMMIT;
