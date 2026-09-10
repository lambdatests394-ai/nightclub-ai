# Prompt 6 security contract

Date: 2026-09-10

Status: Stage B implementation and independent real local PostgreSQL validation
passed human review. The supplied harness evidence is recorded below. No credentials
were requested, inferred, printed or saved. No commit/push had yet occurred.

## HTTP and identity

All endpoints below are under `/api/v1`. JWT verification precedes persistence.
Exactly one UUID X-Organization-Id is mandatory on every campaign endpoint.
Missing, duplicate, malformed, inactive, foreign or no-membership selection gives
403 ACCESS_DENIED. Body/query/path cannot substitute for the header. CurrentUser
and OrganizationContext contracts remain unchanged. Personal identity endpoints
keep their original behavior.

| Endpoint | Success | Roles |
| --- | --- | --- |
| GET /campaigns | 200 | all six |
| GET /campaigns/{campaign_id} | 200 | all six |
| POST /campaigns | 201 | owner, manager, editor |
| PATCH /campaigns/{campaign_id} | 200 | owner, manager, editor |
| POST /campaigns/{campaign_id}/archive | 200 | owner, manager |

Create accepts name, objective, brief, startsAt, endsAt. Name is nonempty, at
most 200 characters; brief is a JSON object; timestamps require an explicit
timezone. Unknown ownership/identity/status fields are rejected, not ignored.
PATCH accepts only those business fields and distinguishes absent from null;
name/brief cannot be null. Resulting combined date range is validated. Archive
has no body. Schemas retain existing camelCase/Python-name handling.

Envelope: data plus meta.correlationId. List additionally has meta.nextCursor.
Pagination: ascending UUID, limit 1..100 (default 50), existing opaque base64 UUID
cursor format. No filtering/search/snapshot guarantee. A cursor never supplies
tenant authority. Absent/foreign campaign gives the same 404 after valid context.

Create always draft. PATCH status forbidden. Archived PATCH gives 409; repeat
archive gives 200 without new campaign.archived audit. No other transitions.
Other errors: 401 auth, 403 authorization, 409 generic key/domain conflict,
422 syntax/schema/cursor, safe 503 identity/auth unavailable. Errors retain
application/problem+json and correlationId. Both existing 503 codes retain
Retry-After: 30; 200/401/403 do not acquire that header.

## Transaction and replay

JWT -> protected root -> runtime role verification -> user GUC -> identity and
membership -> organization context/GUC -> RBAC -> key claim -> campaign row
lock/mutation -> audit -> stored 2xx data -> commit -> HTTP response.

The campaign dependency uses function scope on pinned FastAPI 0.136.3. No new
session inside services/repositories and no intermediate commit. Existing
shielded cleanup, cancellation invalidation and pool protections are reused.

UUID Idempotency-Key is mandatory exactly once for each mutation. Fingerprint
binds operation/resource and canonical payload, including PATCH field presence;
keys are global UUID PKs but readable only within tenant+actor. ON CONFLICT DO
NOTHING arbitrates claims. Hidden or mismatching collision gives generic 409.
READ COMMITTED is checked explicitly before claim; other isolation fails closed.
Every request acquires the key before the campaign row, avoiding reverse ordering.

Only successfully committed 2xx responses replay. Authorization occurs again
before lookup. All error/uncommitted claims disappear with rollback. Replay
persists beyond process/session lifetime and generates fresh correlation metadata.
expires_at equals creation + 24h; there is no automatic deletion or reuse after
expiry. No durable retention job is introduced.

## Security migration 20260910_0004

Parent 20260909_0003. Baseline validation refuses policy drift, wrong role powers,
ownership, missing FORCE/RLS, unexpected column/table/sequence privileges, or
unexpected SECURITY DEFINER functions. No auto-repair. The three bootstrap
policies remain unchanged; seven policies are added (10 total).

| Table | Added policies | Runtime privileges |
| --- | --- | --- |
| campaigns | SELECT, INSERT, UPDATE | SELECT, INSERT; UPDATE name/objective/brief/starts_at/ends_at/status/updated_at |
| idempotency_keys | SELECT, INSERT, UPDATE | SELECT, INSERT; UPDATE state/response_status/response_body/updated_at |
| audit_logs | INSERT | INSERT only |

Every added policy requires a matching non-null transaction-local tenant and
active profile, organization and membership. INSERT campaigns additionally binds
created_by to user and status to draft. Idempotency binds actor to user. Audit
binds user actor, campaign entity and one of the three approved actions.
SELECT/UPDATE use USING; INSERT/UPDATE use WITH CHECK. No global-null escape.

No runtime DELETE anywhere, no audit SELECT/UPDATE, no direct audit sequence
privilege. Identity-generated INSERT is tested in the real-PG harness without
granting nextval/setval/RETURNING access. Other 14 business tables remain closed;
the original three identity tables remain SELECT-only. All 20 retain FORCE/RLS.
No role creation/alteration, SECURITY DEFINER, service-role bypass, worker policy,
table redesign, or frozen migration edit. Automatic downgrade is blocked.

## Audit

campaign.created / campaign.updated / campaign.archived only. Structural after
metadata: campaign/organization IDs, status, start/end timestamps, changed field
names. Actor and correlation are separate columns. No name/objective/brief values,
headers, credentials, raw SQL errors or JWTs. No-op archive/replay creates no new
event. Audit failure rolls back the campaign and claim. Core INSERT is inline,
with no RETURNING or extra audit read grant.

## Evidence and limits

- New tests: test_campaigns.py, test_campaign_security_migration.py,
  test_campaigns_postgres.py. Historical identity/content suites remain in place.
- ASGI test_real_function_dependency_commit_before_response observes commit
  before response-start; injected failure emits 503, never 2xx, and closes session.
- Real harness verifies 0003 -> 0004, unchanged bootstrap, all effective grants,
  catalog, direct runtime attacks, concurrency, atomic rollback, pooling/cancel
  cleanup, blocked downgrade and disposable database removal.
- Audit SQL execution events count INSERTs without adding an audit SELECT grant
  or privileged test bypass. This evidence is paired with successful commits and
  rollback tests; it is not a privileged readback of audit table rows.
- Human-reviewed real PostgreSQL evidence: validate_prompt6.py exit 0; clean
  0002 -> 0003 -> 0004; BOOTSTRAP_UNCHANGED PASS; 10 policies; 390 passed /
  55 expected skips / 1 existing warning; audit minimum privileges, concurrent
  key/RLS behavior, atomic rollback, row-lock races and context/pool cleanup PASS.
- The unsafe 0004 -> 0003 downgrade was blocked with the approved error. The
  subsequent catalog check passed (1 passed, 47 deselected); database head remained
  20260910_0004. Disposable database remainder was empty, and nightclub_api plus
  alembic_test_user were retained unchanged.
- Prompt 5 harness remains pinned to 0003 and uses its own historical tests.
- GUC trust, retained local roles and no remote deployment follow ADR-006/007.
  No content, providers, workers, retention cleanup, or Prompt 7 work is included.
