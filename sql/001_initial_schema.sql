-- NIGHT CLUB AI v1.0 - reviewed schema design only.
--
-- Migration-system decision:
-- These files under sql/ are human-review design drafts and MUST NOT be applied
-- directly to Supabase. After explicit approval, they are translated once into
-- Alembic migrations; backend/migrations/ becomes the executable migration
-- history. Do not apply both systems in parallel.
--
-- This file has not been executed against any database.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;

CREATE TYPE public.member_role AS ENUM ('owner', 'manager', 'editor', 'reviewer', 'operator', 'viewer');
CREATE TYPE public.platform_type AS ENUM ('facebook', 'whatsapp', 'instagram');
CREATE TYPE public.connection_status AS ENUM ('active', 'expired', 'revoked', 'error', 'pending');
CREATE TYPE public.campaign_status AS ENUM ('draft', 'active', 'paused', 'completed', 'archived');
CREATE TYPE public.content_status AS ENUM ('draft', 'in_review', 'changes_requested', 'approved', 'scheduled', 'publishing', 'published', 'failed', 'cancelled');
CREATE TYPE public.asset_kind AS ENUM ('image', 'video', 'document');
CREATE TYPE public.publication_status AS ENUM ('pending', 'leased', 'publishing', 'succeeded', 'retryable_failure', 'permanent_failure', 'cancelled');
CREATE TYPE public.webhook_status AS ENUM ('received', 'processed', 'ignored', 'failed');
CREATE TYPE public.outbox_status AS ENUM ('pending', 'leased', 'delivered', 'retryable_failure', 'dead_letter');
CREATE TYPE public.ai_status AS ENUM ('queued', 'running', 'succeeded', 'failed', 'cancelled');
CREATE TYPE public.whatsapp_direction AS ENUM ('inbound', 'outbound');

CREATE FUNCTION public.set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

CREATE TABLE public.organizations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    slug citext NOT NULL UNIQUE,
    timezone text NOT NULL DEFAULT 'America/Mexico_City',
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE public.profiles (
    id uuid PRIMARY KEY REFERENCES auth.users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    display_name text,
    email citext UNIQUE,
    is_active boolean NOT NULL DEFAULT true,
    last_seen_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE public.organization_members (
    organization_id uuid NOT NULL REFERENCES public.organizations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    user_id uuid NOT NULL REFERENCES public.profiles(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    role public.member_role NOT NULL,
    created_by uuid REFERENCES public.profiles(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, user_id)
);

CREATE TABLE public.platform_connections (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES public.organizations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    platform public.platform_type NOT NULL,
    external_account_id text NOT NULL,
    display_name text NOT NULL,
    capabilities jsonb NOT NULL DEFAULT '{}'::jsonb,
    credentials_ciphertext bytea NOT NULL,
    credential_key_version smallint NOT NULL,
    token_expires_at timestamptz,
    status public.connection_status NOT NULL DEFAULT 'pending',
    last_verified_at timestamptz,
    last_error_code text,
    last_error_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, platform, external_account_id),
    UNIQUE (id, organization_id, platform)
);

CREATE TABLE public.campaigns (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES public.organizations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    name text NOT NULL,
    objective text,
    brief jsonb NOT NULL DEFAULT '{}'::jsonb,
    starts_at timestamptz,
    ends_at timestamptz,
    status public.campaign_status NOT NULL DEFAULT 'draft',
    created_by uuid NOT NULL REFERENCES public.profiles(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT campaigns_valid_date_range CHECK (ends_at IS NULL OR starts_at IS NULL OR ends_at >= starts_at)
);

CREATE TABLE public.assets (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES public.organizations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    storage_bucket text NOT NULL,
    storage_key text NOT NULL UNIQUE,
    kind public.asset_kind NOT NULL,
    mime_type text NOT NULL,
    byte_size bigint NOT NULL CHECK (byte_size > 0),
    sha256 char(64) NOT NULL,
    original_filename text,
    width integer,
    height integer,
    duration_ms integer,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'ready', 'rejected', 'deleted')),
    deleted_at timestamptz,
    uploaded_by uuid NOT NULL REFERENCES public.profiles(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, sha256),
    UNIQUE (id, organization_id),
    CONSTRAINT assets_deleted_state CHECK ((status = 'deleted') = (deleted_at IS NOT NULL))
);

COMMENT ON TABLE public.assets IS 'Assets are soft-deleted by setting status=deleted and deleted_at; physical object deletion is deferred by retention policy.';

CREATE TABLE public.content_items (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES public.organizations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    campaign_id uuid REFERENCES public.campaigns(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    platform public.platform_type NOT NULL CHECK (platform IN ('facebook', 'whatsapp')),
    connection_id uuid,
    status public.content_status NOT NULL DEFAULT 'draft',
    current_version_no integer NOT NULL DEFAULT 1 CHECK (current_version_no > 0),
    approved_version_no integer,
    scheduled_for timestamptz,
    published_at timestamptz,
    external_post_id text,
    last_error_code text,
    last_error_message text,
    created_by uuid NOT NULL REFERENCES public.profiles(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, organization_id),
    CONSTRAINT content_items_approved_version_is_current_or_older CHECK (approved_version_no IS NULL OR approved_version_no <= current_version_no),
    CONSTRAINT content_items_connection_same_organization_and_platform
        FOREIGN KEY (connection_id, organization_id, platform)
        REFERENCES public.platform_connections (id, organization_id, platform)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE public.ai_generation_requests (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES public.organizations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    content_item_id uuid,
    provider text NOT NULL,
    model text NOT NULL,
    prompt_template_key text NOT NULL,
    prompt_template_version integer NOT NULL CHECK (prompt_template_version > 0),
    input_redacted jsonb NOT NULL,
    output jsonb,
    provider_request_id text,
    input_tokens integer CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens integer CHECK (output_tokens IS NULL OR output_tokens >= 0),
    estimated_cost_usd numeric(12, 6) CHECK (estimated_cost_usd IS NULL OR estimated_cost_usd >= 0),
    status public.ai_status NOT NULL DEFAULT 'queued',
    error_code text,
    created_by uuid NOT NULL REFERENCES public.profiles(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, organization_id),
    CONSTRAINT ai_generation_requests_content_same_organization
        FOREIGN KEY (content_item_id, organization_id)
        REFERENCES public.content_items (id, organization_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE public.content_versions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    content_item_id uuid NOT NULL REFERENCES public.content_items(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    version_no integer NOT NULL CHECK (version_no > 0),
    body text NOT NULL,
    title text,
    link_url text,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    source text NOT NULL CHECK (source IN ('manual', 'ai', 'import')),
    ai_generation_id uuid REFERENCES public.ai_generation_requests(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    created_by uuid NOT NULL REFERENCES public.profiles(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (content_item_id, version_no),
    UNIQUE (content_item_id, id)
);

CREATE TABLE public.content_assets (
    content_version_id uuid NOT NULL,
    content_item_id uuid NOT NULL,
    organization_id uuid NOT NULL,
    asset_id uuid NOT NULL,
    position smallint NOT NULL DEFAULT 0 CHECK (position >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (content_version_id, asset_id),
    UNIQUE (content_version_id, position),
    CONSTRAINT content_assets_version_belongs_to_item
        FOREIGN KEY (content_item_id, content_version_id)
        REFERENCES public.content_versions (content_item_id, id)
        ON UPDATE RESTRICT ON DELETE CASCADE,
    CONSTRAINT content_assets_item_same_organization
        FOREIGN KEY (content_item_id, organization_id)
        REFERENCES public.content_items (id, organization_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT content_assets_asset_same_organization
        FOREIGN KEY (asset_id, organization_id)
        REFERENCES public.assets (id, organization_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE FUNCTION public.enforce_content_asset_ready()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    asset_status text;
BEGIN
    SELECT status INTO asset_status
    FROM public.assets
    WHERE id = NEW.asset_id AND organization_id = NEW.organization_id;

    IF asset_status IS DISTINCT FROM 'ready' THEN
        RAISE EXCEPTION 'content assets must reference a ready asset';
    END IF;

    RETURN NEW;
END;
$$;

CREATE FUNCTION public.prevent_attached_asset_unavailability()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status <> 'ready'
       AND EXISTS (SELECT 1 FROM public.content_assets WHERE asset_id = NEW.id) THEN
        RAISE EXCEPTION 'an attached asset cannot transition away from ready';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER content_assets_require_ready_asset
BEFORE INSERT OR UPDATE OF asset_id, organization_id ON public.content_assets
FOR EACH ROW EXECUTE FUNCTION public.enforce_content_asset_ready();

CREATE TRIGGER assets_prevent_unavailable_attached_asset
BEFORE UPDATE OF status ON public.assets
FOR EACH ROW EXECUTE FUNCTION public.prevent_attached_asset_unavailability();

CREATE TABLE public.review_decisions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    content_item_id uuid NOT NULL,
    content_version_id uuid NOT NULL,
    decision text NOT NULL CHECK (decision IN ('approved', 'changes_requested')),
    comment text,
    decided_by uuid NOT NULL REFERENCES public.profiles(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    decided_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT review_decisions_version_belongs_to_item
        FOREIGN KEY (content_item_id, content_version_id)
        REFERENCES public.content_versions (content_item_id, id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE public.publication_jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    content_item_id uuid NOT NULL REFERENCES public.content_items(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    content_version_id uuid NOT NULL,
    idempotency_key uuid NOT NULL UNIQUE,
    scheduled_for timestamptz NOT NULL,
    status public.publication_status NOT NULL DEFAULT 'pending',
    attempt_count smallint NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    lease_token uuid,
    lease_expires_at timestamptz,
    published_external_id text,
    next_attempt_at timestamptz,
    created_by uuid NOT NULL REFERENCES public.profiles(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT publication_jobs_version_belongs_to_item
        FOREIGN KEY (content_item_id, content_version_id)
        REFERENCES public.content_versions (content_item_id, id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE FUNCTION public.enforce_facebook_publication_job()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    item_platform public.platform_type;
BEGIN
    SELECT platform INTO item_platform
    FROM public.content_items
    WHERE id = NEW.content_item_id;

    IF item_platform IS DISTINCT FROM 'facebook' THEN
        RAISE EXCEPTION 'publication jobs are limited to Facebook content in v1';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER publication_jobs_require_facebook_content
BEFORE INSERT OR UPDATE OF content_item_id ON public.publication_jobs
FOR EACH ROW EXECUTE FUNCTION public.enforce_facebook_publication_job();

CREATE TABLE public.publication_attempts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    publication_job_id uuid NOT NULL REFERENCES public.publication_jobs(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    attempt_no smallint NOT NULL CHECK (attempt_no > 0),
    started_at timestamptz NOT NULL,
    finished_at timestamptz,
    request_fingerprint char(64) NOT NULL,
    provider_request_id text,
    provider_response jsonb,
    outcome text NOT NULL CHECK (outcome IN ('succeeded', 'retryable_failure', 'permanent_failure')),
    error_code text,
    error_message text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (publication_job_id, attempt_no),
    CONSTRAINT publication_attempts_valid_finish_time CHECK (finished_at IS NULL OR finished_at >= started_at)
);

CREATE TABLE public.webhook_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid REFERENCES public.organizations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    source text NOT NULL CHECK (source IN ('meta')),
    event_key text NOT NULL,
    headers jsonb NOT NULL,
    payload jsonb NOT NULL,
    payload_sha256 char(64) NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    status public.webhook_status NOT NULL DEFAULT 'received',
    resolution_status text NOT NULL DEFAULT 'pending' CHECK (resolution_status IN ('pending', 'resolved', 'quarantined')),
    processed_at timestamptz,
    error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source, event_key),
    UNIQUE (source, payload_sha256)
);

COMMENT ON COLUMN public.webhook_events.resolution_status IS 'Pending/quarantine state for safe tenant resolution; use quarantined when no unique organization/connection can be identified.';

CREATE TABLE public.whatsapp_conversations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES public.organizations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    connection_id uuid NOT NULL REFERENCES public.platform_connections(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    wa_id text NOT NULL,
    contact_name text,
    last_message_at timestamptz,
    status text NOT NULL DEFAULT 'open',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (connection_id, wa_id)
);

CREATE TABLE public.whatsapp_messages (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL REFERENCES public.whatsapp_conversations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    direction public.whatsapp_direction NOT NULL,
    meta_message_id text UNIQUE,
    message_type text NOT NULL,
    body jsonb NOT NULL,
    delivery_status text,
    sent_by uuid REFERENCES public.profiles(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    sent_at timestamptz,
    received_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE public.outbox_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid REFERENCES public.organizations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    aggregate_type text NOT NULL,
    aggregate_id uuid NOT NULL,
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    idempotency_key uuid NOT NULL UNIQUE,
    status public.outbox_status NOT NULL DEFAULT 'pending',
    attempt_count smallint NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_token uuid,
    lease_expires_at timestamptz,
    delivered_at timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE public.automation_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    outbox_event_id uuid REFERENCES public.outbox_events(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    workflow_name text NOT NULL,
    n8n_execution_id text,
    status text NOT NULL CHECK (status IN ('started', 'succeeded', 'failed')),
    started_at timestamptz NOT NULL,
    finished_at timestamptz,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workflow_name, n8n_execution_id),
    CONSTRAINT automation_runs_valid_finish_time CHECK (finished_at IS NULL OR finished_at >= started_at)
);

CREATE TABLE public.idempotency_keys (
    key uuid PRIMARY KEY,
    organization_id uuid REFERENCES public.organizations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    actor_id uuid REFERENCES public.profiles(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    operation text NOT NULL,
    request_hash char(64) NOT NULL,
    response_status integer,
    response_body jsonb,
    state text NOT NULL CHECK (state IN ('in_progress', 'completed', 'failed')),
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE public.audit_logs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    organization_id uuid REFERENCES public.organizations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    actor_type text NOT NULL CHECK (actor_type IN ('user', 'system', 'n8n', 'meta')),
    actor_id text,
    action text NOT NULL,
    entity_type text NOT NULL,
    entity_id uuid,
    correlation_id uuid,
    ip inet,
    user_agent text,
    before jsonb,
    after jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON COLUMN public.audit_logs.before IS 'Never store credentials, tokens, secrets, or complete WhatsApp message bodies in audit snapshots.';
COMMENT ON COLUMN public.audit_logs.after IS 'Never store credentials, tokens, secrets, or complete WhatsApp message bodies in audit snapshots.';

DO $$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'organizations', 'profiles', 'organization_members', 'platform_connections',
        'campaigns', 'assets', 'content_items', 'ai_generation_requests',
        'content_versions', 'content_assets', 'review_decisions', 'publication_jobs',
        'publication_attempts', 'webhook_events', 'whatsapp_conversations',
        'whatsapp_messages', 'outbox_events', 'automation_runs', 'idempotency_keys',
        'audit_logs'
    ]
    LOOP
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE UPDATE ON public.%I FOR EACH ROW EXECUTE FUNCTION public.set_updated_at()',
            table_name || '_set_updated_at', table_name
        );
    END LOOP;
END;
$$;

COMMIT;
