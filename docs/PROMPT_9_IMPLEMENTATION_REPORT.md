# Prompt 9 — Implementation Report

Date: 2026-09-23

Status: Prompt 9 Stage B implementation complete and validated locally against real PostgreSQL.

## Validated baseline

- Branch: `feature/prompt-9-ai-generation`
- Baseline HEAD: `88a335c91f8bf3184b6fa5746885488778167d78`

This evidence describes the local Prompt 9 working tree at that baseline. It does not claim a commit, push, merge, tag, remote deployment, or production certification.

## Scope

Prompt 9 adds a controlled AI generation vertical slice with exactly three public endpoints:

- `POST /api/v1/ai/generations`
- `GET /api/v1/ai/generations/{generation_id}`
- `POST /api/v1/ai/generations/{generation_id}/apply`

Generation is allowed only for content in `draft` or `changes_requested` and produces exactly three variants using prompt template `facebook_event_v1`, version 1. A human must select and apply a variant explicitly. Applying it is single-use: it creates an immutable `ContentVersion` with `source="ai"`, records `ai_generation_id`, inherits the title, link, and ordered assets, replaces only the body, and returns the content to `draft` for the normal human review path.

AI cannot approve, schedule, or publish. Prompt 9 does not implement image generation, Meta publication, WhatsApp AI, or n8n automation. FastAPI continues to own business rules and state transitions; n8n remains orchestration-only and outside this implementation.

## Transaction boundaries

Generation uses three independent root transactions:

1. Acceptance validates authorization/configuration/content/budget, writes the queued generation and structural audit, and commits the durable `201 queued` idempotency response.
2. Execution claim locks the generation and commits `queued -> running`.
3. Terminalization locks the generation and commits either success or controlled failure.

Provider I/O occurs only between transactions 2 and 3. A success updates the generation, increments the organization/day ledger with an atomic PostgreSQL upsert, and writes `ai.generation_succeeded` in one transaction. Any failure in that transaction rolls back both the success and its charge. Failed generations contribute zero spend.

The durable generate replay remains the original `201` acceptance with `status=queued`. Replays do not duplicate rows, requested audit events, provider calls for non-queued generations, content versions, attachment snapshots, application audit events, or ledger charges.

## SQL NULL persistence corrections

### Initial generation insert

`ai_generation_requests.output` is JSONB. Passing Python `None` through the SQLAlchemy JSONB binder represented the value as JSONB `null`, not PostgreSQL SQL NULL. The `ai_generation_business_insert` RLS policy correctly and intentionally requires `output IS NULL`; JSONB `null` is not accepted as equivalent.

The validated correction makes `AIRepository.create_generation()` omit `output` from the initial queued INSERT, so PostgreSQL stores SQL NULL. This was a persistence-binding correction, not an RLS-policy defect; the policy semantics remain unchanged.

### Failed transition

`AIRepository.finish_failure()` previously assigned `output=None`, which likewise bound as JSONB `null`. The unchanged `running -> failed` database trigger contract correctly requires `NEW.output IS NULL`.

The validated correction makes `finish_failure()` leave `output` out of the UPDATE. Because the preceding `running` row already contains SQL NULL, omission preserves SQL NULL through the failed transition. `finish_success()` remains unchanged and continues to store successful validated provider output as JSONB.

## Providers

FastAPI depends on a typed provider-neutral port. OpenAI is the default provider; Gemini is the explicit second provider. The selected provider is the only adapter invoked and there is no automatic fallback. Provider, model, and credential configuration remain server-side.

The concrete OpenAI Responses and Gemini Interactions adapters use the shared hardened `httpx.AsyncClient`, fixed HTTPS origins, explicit timeouts, redirects disabled, proxy environment disabled, strict structured output, and `store=false`.

No provider SDK was added. Raw prompts, raw provider payloads, response bodies, exceptions, headers, and credentials are neither persisted nor audited. Live OpenAI/Gemini validation was not performed.

## Budget amendment and daily ledger

`ai_generation_requests` intentionally remains tenant-and-creator scoped under RLS. Organization-wide daily budgeting therefore uses the separately approved minimal `ai_daily_usage` aggregate containing only organization, UTC date, aggregate estimated cost, and timestamps.

Admission reads already committed successful spend for the organization/current UTC day. Replays are resolved before the threshold check. Failed, queued, and running generations contribute zero. Success persistence and the ledger update are atomic. The ledger is monotonic and concurrent increments use PostgreSQL `ON CONFLICT DO UPDATE` arithmetic.

This is an admission threshold, not a reservation system. Concurrent generations accepted before terminal costs commit can exceed the configured threshold. Reservations and cleanup workers remain deferred.

## Migration 0007

- Revision: `20260923_0007`
- Parent: `20260922_0006`
- Migration chain `0001 -> 0007`: PASS
- `HISTORICAL_POLICIES_UNCHANGED: PASS`
- Historical policies preserved: 24
- New policies: 6
- `TOTAL_POLICIES: 30`
- `APPLICATION_TABLES: 21`
- `ALEMBIC_HEAD: 20260923_0007`
- New persistent table: `ai_daily_usage`
- Unique partial index: one `content_versions.ai_generation_id` application
- Database closure: queued/running/terminal transitions, terminal field shape, selected generated body, and ledger monotonicity
- Downgrade: intentionally blocked as unsafe

The official harness intentionally ran `alembic downgrade 20260922_0006`. Migration 0007 blocked it with `Unsafe security downgrade blocked; a reviewed compensating migration is required`. This is EXPECTED SECURITY GUARD BEHAVIOR, not a validation failure. The harness continued afterward and completed with exit status 0; migration 0007 does not support an automatic downgrade.

Migrations 0001–0006 and frozen `sql/001`, `sql/002`, and `sql/003` are not modified. `sql/003_rls.sql` was not executed.

## Validation status

### Non-PostgreSQL/local validation

Provider and application tests use local fakes and `httpx.MockTransport`; they make no internet requests. The final non-PostgreSQL suite recorded `726 passed, 253 skipped, 1 existing warning`.

### Official real PostgreSQL validation

Prompt 9 was validated locally against real PostgreSQL:

- Official suite: `744 passed, 235 skipped, 0 failed`
- Harness exit status: `0`
- Migration chain `0001 -> 0007`: PASS
- `HISTORICAL_POLICIES_UNCHANGED: PASS`
- `TOTAL_POLICIES: 30`
- `APPLICATION_TABLES: 21`
- `ALEMBIC_HEAD: 20260923_0007`

`scripts/local-dev/validate_prompt9.py` used only the disposable `nightclub_ai_prompt9_test` database. The database was removed after validation, and the retained local roles `nightclub_api` and `alembic_test_user` remained unchanged.

Supabase, Meta, n8n, Railway, live OpenAI, and live Gemini were not contacted. No production deployment or production security certification occurred, and Prompt 9 Stage C did not start.

## Deferred items

- Recovery worker for generations left `running` by process termination.
- Budget reservations/strict concurrent cap.
- Ledger retention/cleanup worker.
- Provider retry policy (Prompt 9 performs no automatic retry or fallback).
