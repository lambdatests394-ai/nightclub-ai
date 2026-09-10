# ADR-007: Campaign transaction, durable replay and tenant security

Date: 2026-09-10

Status: Frozen contract and Stage B implementation approved by the project owner.
Independent real local PostgreSQL validation passed human review on 2026-09-10.

## Context

Prompt 5 closes identity bootstrap/RLS but grants no campaign writes. Prompt 6
is exclusively the campaign business vertical slice, without content, providers,
workers or deployment. Existing tables and frozen migrations remain authoritative.

## Decision

- Five `/api/v1/campaigns` endpoints; mandatory single UUID X-Organization-Id.
  Invalid selection/membership is 403; absent/foreign campaign after validated
  selection is indistinguishable 404. CurrentUser/OrganizationContext stay frozen.
- Explicit read/write/archive RBAC: all six roles / owner-manager-editor /
  owner-manager. RBAC remains FastAPI responsibility, not a SQL role engine.
- Create draft, reject PATCH status/ownership/identity fields, lock rows before
  PATCH/archive decisions, archived immutable. Repeated archive is 200 without
  a duplicate archive audit. No activate/pause/complete or runtime DELETE.
- Preserve one protected AsyncSession/root transaction and Prompt 5 cleanup.
  Campaign service is a FastAPI `scope="function"` yield dependency. Installed
  FastAPI 0.136.3 routing exits its function stack before `response(..., send)`.
  Its exit commits; commit errors propagate before the HTTP response starts.
- Claim global UUID keys via PostgreSQL INSERT ON CONFLICT DO NOTHING. Require
  READ COMMITTED (fail with safe 503 otherwise), then read a visible actor/tenant
  match. Hidden/mismatching keys produce the same generic 409. Lock order is
  idempotency key then one campaign row; services never commit independently.
- Fingerprint operation/resource and canonical semantic payload; preserve PATCH
  omitted/null distinction and normalize input timestamps to UTC. Authorization
  precedes lookup. Store only 2xx business data, not request correlation metadata.
  A claimed in-progress row and all other writes roll back together on failure.
- expires_at = claim creation time + 24h. This is a minimum replay horizon, not
  a reuse deadline. Expired completed keys still replay. No cleanup worker here.
- Audit INSERT is part of that transaction; no free-text name/objective/brief
  values. Core inline INSERT avoids implicit RETURNING. The approved GENERATED
  ALWAYS AS IDENTITY mapping is explicit. No SELECT/UPDATE/DELETE on audit and
  no direct sequence grants; validate identity insertion against real PostgreSQL.
- New 0004 extends only campaigns/idempotency/audit grants and RLS. It checks the
  exact three-policy 0003 baseline, effective privileges, nonowner runtime and
  20 ENABLE/FORCE tables before DDL. Downgrade is blocked. No SECURITY DEFINER.
- Historical Prompt 5 harness upgrades explicitly to 0003. Independent Prompt 6
  harness uses only nightclub_ai_prompt6_test and retains both local roles.

## Consequences

Concurrency and durable replay depend on PostgreSQL, not a process-local cache.
Global key collision yields only a generic conflict; it never authorizes reading
another actor's response. Audit replay/no-op never repeats the original event.
Commit-before-response is tested with actual FastAPI ASGI response-start events
and the production transaction cleanup, injecting only session commit I/O.

RLS trusts the server-established GUCs, as in ADR-006; compromised runtime
credentials or arbitrary SQL are not covered. Direct SQL UPDATE authorization
is tenant isolation, not the full campaign lifecycle/RBAC engine. API is the only
supported business entry point. No direct Supabase client or service-role path.

No optimistic version column: row locks serialize concurrent operations and
PATCH applies supplied fields only. Pagination is ascending immutable UUID with
an opaque UUID cursor; it is not a cross-request snapshot or authorization token.

Real Prompt 6 PostgreSQL validation passed through the independent local harness:
0003 -> 0004, ten policies with unchanged bootstrap, effective grants, concurrency,
atomic rollback and connection-context cleanup. The harness completed with exit 0,
390 passed / 55 expected skips, head 20260910_0004, blocked unsafe downgrade and
an empty disposable-database remainder. Both retained roles remained unchanged.
No production/Supabase validation or deployment is claimed.
