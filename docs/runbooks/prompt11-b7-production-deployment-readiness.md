# Prompt 11 B7 — Production deployment readiness

Date: 2026-09-29. This is a pre-deployment contract, NOT deployment or activation
authorization. All production checks below are future HUMAN / PLATFORM ADMIN
actions. Do not run them during B7. No production state is certified by an offline
validator.

## Authority and certified baseline

Branch: `feature/prompt-11-automation`.
Certification commit: `301fe0f0c8bb2e0bff1c7f8a042e1aa2bb794529`.
B6 report: `docs/PROMPT_11_B6_LIVE_N8N_CERTIFICATION.md`; its execution baseline
is B5 commit `a0746aaeb102dc594fa614e0c9a2a908e4143356`. These are different roles
for the two hashes, not contradictory baseline claims.

Current deployment rules:

- `sql/003_rls.sql` MUST NOT be executed.
- Alembic requires `DATABASE_MIGRATION_URL`; no fallback to `DATABASE_URL`.

The historical migration 0001 docstring instructing an administrator to apply
the frozen SQL is SUPERSEDED by human decision, README and migration 0003.
The fallback wording in ADR-004 is SUPERSEDED by its dated clarification,
ADR-006 and `core/migration_config.py`. Historical migrations remain immutable.
Precedence for documentary ambiguity: executable code, migration safety checks,
approved security/runbook documentation, historical ADRs, historical migration
comments, frozen design SQL. This never permits bypassing a code check.

B6 records n8n 2.40.7 / Node 24.21.0, two controlled manual 200 responses,
zero work, zero Meta calls and zero scheduled executions. The second execution
is a documented, owner-accepted procedural variance. The local DB and isolated
n8n state were later removed; roles were retained. B7 does not recreate them.
B6 validates local interoperability, not production TLS, connectivity or nonempty
publication execution. B2/B3/B4 provide the earlier role/RLS execution evidence.

## Topology and unfinished platform configuration

Target: Railway FastAPI + separate Railway n8n service; Supabase PostgreSQL holds
application data. n8n sends HTTPS/HMAC to FastAPI only. FastAPI holds separate
runtime and scheduler pools; n8n holds neither database credential. Meta is called
only by FastAPI when authorized publication work exists. Cloudflare is optional
future infrastructure, not a dependency; workers and Next.js/Vercel are outside B7.

`infra/railway/` contains a placeholder only. There is no Dockerfile, Procfile or
Railway deployment definition. Thus build selection, production origins, target
port, trusted proxy path, replicas, persistent n8n storage/backups and network/TLS
configuration are operator readiness items, NOT already deployed facts. They
must be recorded and reviewed before activation. Never auto-deploy a feature
branch as a side effect of repository promotion.

## Environment contract matrix

Evidence: `core/config.py`, `core/database.py`, `core/migration_config.py`,
`core/scheduler_database.py`, `modules/automation/publish_due_dependencies.py`,
`modules/integrations/credentials.py`, `modules/integrations/facebook_provider.py`.

Notation: F = FastAPI process (including discovery), M = isolated migration
process, N = n8n. Required means required for the stated operation, not necessarily
required by Pydantic at startup. Names/defaults below are safe in Git; actual
secret values are NEVER safe in Git. No connection strings are included.

| Variable | Consumer / requirement | Secret; source; Git | Format and validation timing | Rotation/change |
| --- | --- | --- | --- | --- |
| DATABASE_URL | F, required for real execution; not N/M | Secret; dedicated Supabase runtime login injected via Railway; name only | PostgreSQL asyncpg URL; plain PostgreSQL schemes normalize to asyncpg. psycopg runtime URL rejected at import. Empty value permits startup without session factory; connectivity/role checked on use, not health | Replace only runtime credential; restart pool/process under maintenance |
| DATABASE_MIGRATION_URL | M required; not F/N | Secret; separately controlled DDL secret; name only | Explicit PostgreSQL migration URL, normalized to synchronous psycopg; missing value raises, never uses runtime credential | Admin-controlled migration credential lifecycle, never distribute to runtime |
| DATABASE_SCHEDULER_URL | F discovery required; not N/M | Secret; separate scheduler login via Railway; name only | Same asyncpg URL handling as runtime; missing factory or failed discovery/role check yields generic 503 after valid authentication | Replace only scheduler credential; restart its pool/process |
| DATABASE_RUNTIME_EXPECTED_ROLE | F; default nightclub_api | Non-secret; fixed migration role; default safe | Lowercase identifier up to 63 chars; actual role/direct login checked in execution transactions | Do not rename independently of migrations |
| DATABASE_SCHEDULER_EXPECTED_ROLE | F; default nightclub_scheduler | Non-secret; fixed migration role; default safe | Same identifier syntax; actual role/direct login verified for discovery | Do not rename independently |
| N8N_INTERNAL_SECRET | F required to authenticate; N needs same value in Crypto credential, NOT as workflow/environment expression; not M | Secret; controlled Railway secret and n8n credential UI; name only | Exact printable UTF-8, at least 32 bytes, no surrounding whitespace. Empty allowed at startup but fails authentication with 401 | Current/previous overlap, coordinated process restart and credential update |
| N8N_INTERNAL_SECRET_PREVIOUS | F optional overlap only; not N/M | Secret; Railway; name only | Same validation; requires current, must differ; default empty | Remove after approved transition; n8n never signs with two secrets |
| AUTOMATION_HMAC_MAX_SKEW_SECONDS | F; default 300 | Non-secret; approved config; default safe | Integer 30–600 at settings load; keep certified 300 | Changing certified value requires review; maintain synchronized clocks |
| AUTOMATION_PUBLISH_BATCH_SIZE | F; default 8 | Non-secret; approved config; default safe | Integer 1–32 at load; keep 8 | No unreviewed tuning |
| AUTOMATION_PUBLISH_MAX_CONCURRENCY | F; default 4 | Non-secret; approved config; default safe | Integer 1–8, at most batch size; keep 4 | Per coordinator invocation, NOT a global distributed limit |
| META_GRAPH_API_VERSION | F required at coordinator construction, even zero-work; not N/M | Non-secret; owner-approved supported Meta version; syntax safe, no real value frozen here | vN.N; nonempty invalid syntax fails settings; empty passes settings but provider construction fails, yielding 503 | Version review and provider certification before change |
| META_CREDENTIAL_ENCRYPTION_KEY | F required at coordinator construction, even zero-work; not N/M | Secret; Railway secret store; name only | Canonical unpadded base64url of 32 bytes, length 43; decoded by cipher on composition, not settings startup | No dual-key reader exists: do not replace blindly; separate reviewed re-encryption/recovery plan |
| META_CREDENTIAL_KEY_VERSION | F; default 1 | Non-secret; matches stored encrypted envelopes; default safe | Integer at least 1 at settings load; exact match on encryption/decryption | Coordinate with key/envelope migration; no automatic rotation |
| APP_NAME | F optional, default nightclub-ai | Non-secret; config; safe | String; API title | Restart to change |
| APP_ENV | F optional, default development | Non-secret; operator label; safe | String only; production label does NOT enable security controls | Set intentionally; no implicit production mode |
| LOG_LEVEL | F optional, default INFO | Non-secret; Railway config; safe | Logging level consumed in lifespan; invalid value may fail startup | Keep INFO; do not enable verbose HTTP/SQL/credential logs |

Settings are cached per process; assume restart required for application environment
changes. `DATABASE_URL` and migration URL are plain strings in Settings, unlike
the scheduler SecretStr: never log, dump or serialize the Settings object.
Alembic imports database metadata/config; use a dedicated migration environment
without runtime/scheduler secrets, so unrelated engine creation cannot derail DDL.

### Conditional existing application features (not Prompt 11 trigger prerequisites)

The full app mounts existing user APIs too. Do not confuse zero-work trigger
readiness with those features being provisioned:

- Human JWT endpoints need SUPABASE_JWT_ISSUER, SUPABASE_JWT_AUDIENCE,
  SUPABASE_JWKS_URL (non-secret, trusted owner-configured HTTPS endpoints/audience).
  SUPABASE_JWT_ALLOWED_ALGORITHMS defaults to ES256 only; JWT_CLOCK_SKEW_SECONDS
  defaults 60 and SUPABASE_JWKS_CACHE_TTL_SECONDS 300. Missing issuer/audience/JWKS
  does not bypass auth. JWKS fetch is on demand. Review/restart on change.
- Storage needs SUPABASE_URL and secret SUPABASE_SERVICE_ROLE_KEY only when its
  adapter is used. The key is never a DB runtime credential and never goes to n8n.
  STORAGE_BUCKET and asset/TTL settings retain existing defaults. This is not
  permission to provision storage or use service-role RLS bypass for automation.
- Existing OAuth requires META_APP_ID, META_APP_SECRET, META_OAUTH_REDIRECT_URI
  and META_OAUTH_STATE_KEY in addition to crypto/version configuration. App secret
  and state key are secrets; identifier/HTTPS redirect are configuration. These
  are not consumed by the internal zero-job trigger. OAuth/Meta provisioning and
  their rotations remain separately controlled.
- OPENAI_API_KEY / GEMINI_API_KEY are secrets for optional AI providers, not
  automation requirements. Models, rates, AI limits and budgets are non-secret
  settings. No provider is called during B7.

The remaining `.env.example` names CORS_ALLOWED_ORIGINS, PUBLIC_APP_URL,
META_WEBHOOK_VERIFY_TOKEN, META_PAGE_ID, META_WABA_ID, META_PHONE_NUMBER_ID,
META_BUSINESS_ACCOUNT_ID, N8N_BASE_URL, N8N_OUTBOUND_WEBHOOK_URL, SENTRY_DSN,
OTEL_EXPORTER_OTLP_ENDPOINT and RAILWAY_ENVIRONMENT are not Settings fields or
Prompt 11 runtime requirements. Do not claim that merely setting them enables
CORS, monitoring, webhooks or orchestration.

### n8n and platform configuration boundary

No n8n service environment declaration exists in this repository. Therefore no
additional environment variable is declared a repository-proven required n8n
variable by B7. The proven application integration inputs are: Crypto credential
**Night Club AI Internal HMAC / Hmac Secret**, HTTPS target URL, and the workflow
settings below. These are configured in the imported instance, not an env fallback.

The platform owner must separately freeze the pinned n8n 2.40.7 service setup,
persistent state and credential-encryption material, backup/restore, authenticated
editor access, port and proxy configuration before deploying. Credential-encryption
material and owner credentials are secrets; neither belongs in Git or FastAPI.
Do not guess variable names, enable public webhooks, or reuse application DB roles
for n8n's own storage. Node 24.21.0 is the B6 evidence, not a floating version.
Any platform-specific env manifest remains a pre-deployment human deliverable.

## PostgreSQL provisioning boundary and migration order

HUMAN / PLATFORM ADMIN ACTION REQUIRED, outside B7:

1. Select an independent Night Club AI production Supabase project and DDL
   owner/migration identity; do not reuse local test credentials or alembic_test_user.
2. Provision nightclub_api and nightclub_scheduler externally BEFORE the chain
   reaches their checks (0003 and 0009 respectively). Both must be LOGIN,
   NOINHERIT, NOSUPERUSER, NOCREATEDB, NOCREATEROLE, NOREPLICATION, NOBYPASSRLS,
   with no memberships and no ownership of application tables. Passwords/login
   access and database CONNECT are administrative responsibilities, not Alembic.
3. Remove unexpected effective schema CREATE through an approved admin process;
   inspect PUBLIC/default grants too. Scheduler cannot CREATE in any non-system
   schema; API cannot CREATE in public. Do not blindly revoke managed platform
   privileges. If the managed project cannot satisfy the checks, STOP for review.
4. Confirm managed auth.users already exists and the DDL identity can reference
   it. NEVER apply the local auth_stub fixture to Supabase. Check pgcrypto/citext
   availability/permissions; 0001 requests these extensions. DDL identity must
   own/manage the application objects and their grants, separately from runtime.
5. Record initial catalog/head, backup and restore point. Use only an approved
   compatible baseline; never stamp a head to skip verification. On a new empty
   application schema, execute the complete chain in a controlled migration job.
   The target command, for later approval only, is:

   ```text
   python -m alembic upgrade 20260928_0010
   ```

6. 0001/0002 define schema/indexes; 0003–0008 establish and validate business
   RLS/grants. 0009 validates 43 existing policies and scheduler posture, grants
   only schema USAGE plus column SELECT on publication_jobs (id, content_item_id,
   status, scheduled_for, next_attempt_at, lease_expires_at) and content_items
   (id, organization_id), and adds two SELECT policies. No table-level scheduler
   SELECT or DML grants. 0010 adds 13 tenant-local API system policies, with no new
   runtime/scheduler ACLs. It verifies the 45-policy baseline and preserves it.
7. Verify head 20260928_0010; 58 policies total / 13 system policies / 22 canonical
   ENABLE+FORCE tables; verify exact identities, expressions, roles and effective
   privileges, not counts alone. Unexpected public policies or privileged functions
   may correctly abort these migrations; do not drop them to force success.

Role attributes are validated, NOT silently repaired. Direct current_user =
session_user is checked at discovery/execution; no SET ROLE workaround. Neither
role is a superuser, service role, table owner or bypass principal. No runtime
DELETE. No SECURITY DEFINER helper. Security downgrades are deliberately blocked;
use reviewed compensating migrations, never edit frozen revisions.

Supabase connectivity, IPv4/IPv6, server-certificate verification and DDL rights
must be proven for the chosen endpoint. The runtime drivers are asyncpg; migration
driver is psycopg. Do not paste psycopg-specific TLS query options into asyncpg
without checking driver compatibility. No transaction-mode pooler is certified
(ADR-006); changing connection mode requires validating role identity, prepared
statements and transaction-local context. See [Supabase connection guidance](https://supabase.com/docs/guides/database/connecting-to-postgres).

## FastAPI / Railway readiness

Proposed explicit Linux-container start contract (documentation only; not configured):

```text
python -m uvicorn backend.app.main:app --host 0.0.0.0 --port "$PORT" --workers 1 --no-access-log --no-proxy-headers
```

Use a reviewed start-command shell that expands PORT; do not assume JSON/exec-form
commands expand it. Python 3.12 and `requirements.txt` are the backend build inputs.
No reload. The container bind is behind the controlled Railway edge, unlike B6's
loopback bind. The app does not read PORT itself. Configure a matching target port
and health path; do not copy README's development `--reload` command into production.
See [Railway start commands](https://docs.railway.com/deployments/start-command) and
[public networking](https://docs.railway.com/networking/public-networking).

HTTPS must terminate at the approved edge with valid certificates and no alternate
public plaintext backend route. n8n must directly address the HTTPS canonical
path, without relying on HTTP-to-HTTPS redirects. The no-proxy-headers start
contract avoids trusting caller-supplied forwarding headers. If another feature
requires forwarded metadata, separately record trusted proxy addresses/hops and
edge sanitization; never implement naive X-Forwarded-Proto authorization. Cloudflare
would add another trust hop and requires its own review.

`/health/live` returns status ok; `/health/ready` is only the preserved application
scaffold check. Neither proves database connectivity, role posture, Meta config
or production readiness. Lifespan creates a shared HTTPX client (verify=True,
follow_redirects=False, trust_env=False), storage/AI registries and optional JWT
verifier; it does not connect/probe both databases. Scheduler pool is disposed on
shutdown. Runtime pool lifetime is process-scoped, with no explicit disposal in
main lifespan; drain in-flight work before process shutdown.

| Condition | Actual behavior; activation implication |
| --- | --- |
| Bad runtime/scheduler URL scheme | Engine creation at import fails; do not expose connection exception details |
| Missing scheduler URL, bad connectivity or unsafe scheduler role | Valid HMAC call reaches discovery and returns generic 503 |
| Missing runtime URL or unsafe runtime role | Execution fails when a job opens its transaction; a ZERO-JOB 200 does not prove this connection works |
| Missing/invalid crypto key or empty Meta version | Composition fails with generic 503 after HMAC/shape validation, before discovery; some malformed settings fail earlier |
| Missing HMAC current secret | Startup may succeed; request authentication returns 401 |
| Inactive organization | Executor terminates permanently with ORGANIZATION_INACTIVE before provider publication |

Always separately verify both real login connections and catalog posture before
activation; health or zero-work HMAC alone cannot replace this. System execution
uses transaction-local app.execution_context=system_automation, organization ID,
and empty user ID; audit uses actor_type=system and actor_id=NULL. This is the
existing nightclub_api principal with existing column ACLs, not a new column-
isolated role. Only trusted application paths install system context. Durable
scheduled commands belong to the organization; creator revocation does not cancel
them. Explicit cancellation is required. Assets, automation_runs and outbox_events
are not new system-execution surfaces.

Protected internal responses receive Cache-Control: no-store and X-Correlation-Id.
The protected log allowlist is event, correlationId, endpoint and status only for
internal paths; no headers/body/query or exception text. Disable unsanitized proxy,
HTTP client and Uvicorn access logs; the application allowlist is not proof of
platform log hygiene. Generic 401/422/503 responses expose no internal details.
Correctly authenticated nonempty-body/query requests produce 422; a mismatched
signature produces 401 first. Do not interpret an unsigned malformed request as
a contract-validation test.

## Single-executor v1 guard

Require EXACTLY ONE API replica and ONE Uvicorn worker, one n8n scheduler instance
and one imported schedule. No extra worker service. Railway supports replicas;
the repository has no platform setting preventing accidental scaling. Verify
regional replica count, process command and absence of duplicate deployments.
See [Railway scaling guidance](https://docs.railway.com/deployments/optimize-performance).

Concurrency 4 is per call; no distributed global semaphore or multi-replica
certification is claimed. Leases/reconciliation do not establish that broader
guarantee. Freeze schedule/manual triggers and drain in-flight calls during
rollouts/rollbacks, including overlapping old/new deployments. A 90-second HTTP
timeout and 120-second workflow timeout do not automatically cancel backend work;
never blindly re-execute after an uncertain result.

## n8n readiness and rotation

Import the portable artifact with all 11 original nodes and versions: Schedule
1.2, Set 3.4, Crypto 2, HTTP Request 4.2, Switch 3.2, Stop And Error 1. Workflow
name is Night Club AI — Publish Due. One-minute schedule, UTC (B6 displayed the
equivalent Etc/UTC), active=false. Do not export instance IDs/credentials back to Git.

Configure the Crypto Hmac Secret in the UI using the controlled current secret;
HTTP Request has no attached credential. Replace only the imported URL origin
with approved HTTPS; never alter the committed placeholder. Timestamp generated
once, reused in canonical payload/header; UTF-8 LF, no trailing newline; empty-
body SHA-256; HMAC-SHA256 hex with version prefix. Two custom headers only:
X-N8N-Timestamp and X-N8N-Signature. No JWT, tenant, Cookie or Idempotency-Key.
No query/body, redirects false, TLS verification on, timeout 90000 ms, retries off.
Full response and Never Error on. 200 emits eight counters plus correlationId;
401/422/503/other route to the four fixed errors from B5, including 3xx as other.

Workflow timeout 120, executionOrder v1, no progress/manual/success saving.
The portable artifact requests error saving all and redactionPolicy all. B6's
license did not enforce redaction, so error saving was disabled in the live
instance. Before deployment execution require effective redaction OR disabled
error persistence; prefer the stronger B6 no-persistence profile. Never assume
imported redaction settings are enforced. Restrict editor access, logs and backups.
Credential storage still needs secure persistence even when execution saving is off.

HMAC rotation plan ONLY: pause triggers/drain; provision new backend current,
retain old as previous; restart the single API safely; update the one n8n Crypto
credential; verify new signer in an approved zero-work window; remove previous
after the reviewed overlap and clock-skew window, restart safely, then review
reactivation. Maximum accepted skew stays 300 seconds. No fallback signer in n8n,
no secret values/signatures in reports. HMAC does not provide nonce-based replay
prevention inside the window; keep signing material private and rely on the
certified business leases/idempotency, not a new n8n retry scheme.

## Pre-activation zero-state gate and first execution plan

All phases below are future, separately authorized. B7 runs none of them.

### Phase A — Deploy inactive

Approve platform manifest, dependencies, dedicated secrets, backups, URL/TLS,
role provisioning and migration sequence. Deploy one API process and n8n with
workflow imported/configured but inactive/unpublished. Keep all automatic and
manual triggers disabled until the next gate. No production secret in Git.

### Phase B — Infrastructure and security evidence

Record deployment/version IDs (without credential IDs), exact head, catalog
58/13/22 plus identities/grants/FORCE posture, two direct-login checks, connection
TLS evidence, worker/replica counts, health, proxy trust and sanitized log policy.
Test invalid HMAC -> 401 and a correctly signed body/query violation -> 422 in a
separately approved non-business-mutating check. Capture only status/correlation.
Verify no alternate proxy route bypasses the same endpoint authentication.

### Phase C — One controlled zero-work manual run

Before any valid trigger, establish an independently checked empty-safe dataset
with zero publication_jobs and zero publication_attempts; preferably also zero
content_items and platform_connections. A runtime RLS-hidden count of zero is
NOT proof of emptiness: use an authorized administrative read-only census, plus
the scheduler eligibility predicate. Freeze producer activity so jobs cannot
arrive between the check and call. Do not delete production data to satisfy this.
If an empty-safe window cannot be established, STOP for a separate reviewed plan.

Execute once through n8n, still unpublished. Expect HTTP 200, all eight counters
zero and one correlationId matching the protected log. Recheck zero jobs/attempts,
zero Meta calls, zero automatic executions and unchanged role/catalog posture.
Independent role checks remain required because no job exercises runtime SQL.

### Phase D — Human review

Collect sanitized evidence; observe for more than one cadence interval that no
automatic execution appears. Saved-execution history alone is insufficient if
persistence is disabled; correlate UI state and protected API logs/metrics.
Confirm active=false/unpublished and no duplicate workflows/instances.

### Phase E — Separate activation authorization

Only the owner may authorize activation after A–D pass, approved job inventory,
Meta permissions/configuration and operator ownership of alerts/abort procedures
are reviewed. B7 does not authorize the first real Facebook publication.

### Phase F — Observe the first automatic run

Observe exactly the authorized schedule instance, counters, correlation,
publication outcomes and audit. Legitimate work after activation may change
business counts; compare against the approved inventory, not an eternal-zero rule.

### Phase G — Abort / deactivate

Before activation, any failure means DO NOT ACTIVATE. After later authorized
activation, stop future triggers and pause producer/manual requests; drain and
inspect in-flight work before rollback. Never replay ambiguous publication calls.

Abort on wrong head/policy/FORCE state; privileged, member, owner or schema-CREATE
role; scheduler write access; failed runtime context; nonempty body/query; bad
HMAC/skew; redirects; disabled TLS checks; HTTP retries; unexpected activation;
multiple API replicas/workers; unexpected jobs/attempts/Meta calls; exposed
secrets/headers in logs; missing correlation; or unexpected HTTP status.
Preserve sanitized evidence and escalate. An HTTP 503 can follow partial batch
progress; it is not proof of no side effects. No automatic production repair,
role elevation, schema downgrade or security bypass. Restore only a reviewed
compatible application release; schema/security reversal requires a reviewed
compensating migration and its own approval.

## Evidence checklist and offline verification

Record owner approvals; code/workflow versions; build/start/replica settings;
database migration identity separation; role/catalog/TLS checks; effective n8n
storage/privacy settings; inactive state and observation interval; status/counters/
correlation only; pre/post census and Meta/network evidence; rollback contact and
backup verification. Never record DSNs, passwords, secret values, signatures,
credential IDs or raw execution exports.

```powershell
.\.venv\Scripts\python.exe scripts/local-dev/validate_prompt11_b7_deployment_readiness.py
.\.venv\Scripts\python.exe -m pytest backend/tests/test_prompt11_b7_deployment_readiness.py backend/tests/test_automation_config.py backend/tests/test_automation_internal_auth.py backend/tests/test_prompt11_n8n_workflow_contract.py backend/tests/test_publish_due_api.py backend/tests/test_publish_due_coordinator.py backend/tests/test_publish_due_discovery.py -q
```

The B7 validator uses local Git objects and source/JSON data only, never .env,
application startup, network, SQL or a deployed platform. It checks the certified
baseline, the full B5 workflow contract, excluded automation surfaces and active
deployment-document rules. Its documentation checks detect defined unsafe command
and imperative patterns; they are not a universal natural-language proof.
Offline PASS certifies the contract/artifacts, not completion of A–G.
