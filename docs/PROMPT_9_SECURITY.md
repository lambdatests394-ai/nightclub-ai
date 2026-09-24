# Prompt 9 — Security Model

Date: 2026-09-23

Status: Prompt 9 Stage B security controls validated locally against real PostgreSQL.

## Authority boundaries

JWT verification and active organization membership are established before any AI transaction. `X-Organization-Id` must occur exactly once. `ai:generate` is restricted to owner, manager, and editor. Generation reads additionally require the creator through both repository predicates and RLS, preserving tenant isolation and creator-scoped visibility.

The runtime role remains `nightclub_api`: non-owner, non-superuser, no role memberships, no schema create, and `NOBYPASSRLS`. FORCE RLS remains enabled. No `service_role`, `BYPASSRLS`, `SECURITY DEFINER`, role mutation, or grant widening beyond the approved minimum was introduced.

## RLS and grants

Migration 0007 preserves all 24 historical policies and adds exactly six:

- `ai_generation_business_select`
- `ai_generation_business_insert`
- `ai_generation_business_update`
- `content_versions_ai_insert`
- `audit_ai_insert`
- `ai_daily_usage_business_all`

All 21 application tables use ENABLE RLS and FORCE RLS. Generation selection requires both current tenant and creator. The ledger policy is organization scoped and contains no content or actor data. Runtime has no DELETE privilege. Ledger UPDATE is limited to aggregate cost and update timestamp; generation UPDATE is limited to operational terminal fields; content version receives only the approved AI generation FK insert privilege.

The AI content-version policy binds generation, content, organization, creator, succeeded status, next version number, allowed content state, empty payload, and a body that exactly matches one of the normalized generated variants. A partial unique index enforces single use.

## SQL NULL state invariants

`ai_generation_business_insert` remains fail-closed and intentionally requires `ai_generation_requests.output IS NULL` for a queued insert. Because `output` is JSONB, JSONB `null` is not equivalent to PostgreSQL SQL NULL. The repository therefore omits `output` from the initial INSERT so PostgreSQL stores SQL NULL. The RLS policy was correct and remains unchanged.

The failed-transition trigger likewise remains unchanged and correctly requires `NEW.output IS NULL`. `finish_failure()` omits `output` from its UPDATE, preserving the SQL NULL already present in the running row. Successful output persistence remains JSONB: `finish_success()` is unchanged and stores only validated provider output.

## Provider and secret safety

Provider origins are constants. API keys are `SecretStr` fields excluded from representation and serialization. Keys are sent only in the provider-prescribed header, never a query parameter. Both adapters set `store=false`, use strict structured output, disable redirects, inherit TLS verification, ignore proxy environment, and send no assets or external retrieval URLs.

Output must contain exactly three distinct trimmed variants of at most 2200 Unicode characters and bounded safety flags. Invalid, blocked, timeout, authentication, rate-limit, availability, and protocol results map to controlled codes. No provider secrets are stored in generated records. Raw provider responses, raw payloads, and exception text are discarded; only approved normalized output and redacted structural metadata persist. Tests use local fakes and `httpx.MockTransport`, so no live provider calls occur.

## Audit privacy

AI audit events contain structural IDs, provider/model/template identifiers, state, token counts, estimated cost, selected index, and controlled error code only. They exclude briefs, event details, generated copy, title, link, asset names, prompts/instructions, credentials, headers, provider bodies, and exceptions. Audit and mutations share the same root transaction.

## Idempotency and accounting

Both mutations require one UUID `Idempotency-Key`. Generate fingerprints tenant, actor, content, resolved provider, template, and validated brief. Apply fingerprints tenant, actor, generation, and selected index. Cross-actor/tenant collisions remain generic conflicts through the shared global key arbitration.

Only committed 2xx results are durable. Successful generation accounting and the organization-scoped `ai_daily_usage` increment share one terminal transaction and use an atomic upsert. Terminal state cannot transition again, preventing double charge. Failed generations preserve SQL NULL output and create zero ledger spend. The ledger cannot decrease or be re-keyed.

## Validation evidence

### Non-PostgreSQL/local validation

The final non-PostgreSQL suite recorded `726 passed, 253 skipped, 1 existing warning`. Provider tests used only local fakes and `httpx.MockTransport`.

### Official real PostgreSQL validation

The security model was validated locally against real PostgreSQL:

- Official suite: `744 passed, 235 skipped, 0 failed`
- Harness exit status: `0`
- Migration chain `0001 -> 0007`: PASS
- `HISTORICAL_POLICIES_UNCHANGED: PASS`
- `TOTAL_POLICIES: 30`
- `APPLICATION_TABLES: 21`
- `ALEMBIC_HEAD: 20260923_0007`

The disposable `nightclub_ai_prompt9_test` database was removed after validation. The retained local roles `nightclub_api` and `alembic_test_user` remained unchanged.

The harness intentionally attempted `alembic downgrade 20260922_0006`; migration 0007 rejected it with `Unsafe security downgrade blocked; a reviewed compensating migration is required`. This is EXPECTED SECURITY GUARD BEHAVIOR. Validation continued and the harness completed with exit status 0; no automatic security downgrade is supported.

## Remaining boundaries

Prompt 9 deliberately has no automatic provider retry/fallback, no recovery worker, and no strict budget reservation. Real provider calls were not performed. No Supabase, Meta, n8n, Railway, or other remote deployment service was contacted. This is local real-PostgreSQL validation only; it does not claim production deployment or production security certification, and Stage C has not started.
