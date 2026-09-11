# Prompt 7 Stage B — Security controls and grants

Date: 2026-09-11

Source reviewed: `backend/migrations/versions/20260910_0005_content_business_access.py`.
Revision `20260910_0005` descends from `20260910_0004`. This document describes
the actual migration and code, not additional authorization to apply them remotely.

## Preserved security baseline

All 20 canonical application tables retain ENABLE ROW LEVEL SECURITY and FORCE
ROW LEVEL SECURITY. The runtime `nightclub_api` is LOGIN, NOINHERIT, NOSUPERUSER,
NOCREATEDB, NOCREATEROLE, NOREPLICATION, NOBYPASSRLS, with no role memberships,
no public-schema CREATE and no ownership of application tables. Migration 0005
checks these properties and the exact ten historical policies before granting
anything. It does not create, alter, delete or change credentials of any role.

The three Prompt 5 bootstrap SELECT policies and seven Prompt 6 business policies
remain unchanged; eight additions produce **18 total policies**. The accepted
local PostgreSQL checkpoint and catalog tests confirmed this state. Both local
roles, including the intentionally CREATEDB `alembic_test_user`, are retained.
Neither validation role is an authorization to use remote Supabase.

## Exact new policies

All policies target only `nightclub_api` and are permissive. Here `TENANT` means
the frozen 0004 expression requiring organization_id equal to transaction-local
app.organization_id, an active profile matching app.user_id, membership and an
active organization. Missing context denies rows; malformed UUID casts fail
closed. Child policies traverse RLS-protected parents, not privileged helpers.

| Policy | Table | Operation | Predicate |
| --- | --- | --- | --- |
| `content_items_business_select` | content_items | SELECT | USING TENANT |
| `content_items_business_insert` | content_items | INSERT | WITH CHECK TENANT and constrained creation fields below |
| `content_items_business_update` | content_items | UPDATE | USING TENANT and WITH CHECK TENANT |
| `content_versions_business_select` | content_versions | SELECT | USING an accessible content_items parent in the selected organization |
| `content_versions_business_insert` | content_versions | INSERT | WITH CHECK the same parent, created_by = app.user_id, source = manual, ai_generation_id IS NULL |
| `review_decisions_business_insert` | review_decisions | INSERT | WITH CHECK a same-tenant content parent joined to the referenced content_version, decided_by = app.user_id |
| `platform_connections_content_select` | platform_connections | SELECT | USING TENANT |
| `audit_content_insert` | audit_logs | INSERT | WITH CHECK TENANT, user actor matching app.user_id, content entity and one of the five approved actions |

Content creation additionally requires created_by = app.user_id, status draft,
current_version_no = 1, approved_version_no IS NULL, and NULL scheduled_for,
published_at, external_post_id, last_error_code, last_error_message. An optional
campaign must exist in the selected organization. Archive status and active,
platform-matching connection checks are enforced by the application; they are
not additional predicates silently claimed for this INSERT policy.

## Exact new privileges

All targets are in `public`; grantee is only `nightclub_api`.

| Target | Privilege | Exact scope |
| --- | --- | --- |
| content_items | table SELECT | All columns of this table |
| content_versions | table SELECT | All columns of this table |
| platform_connections | column SELECT | id, organization_id, platform, status |
| content_items | column INSERT | id, organization_id, campaign_id, platform, connection_id, status, current_version_no, approved_version_no, created_by |
| content_versions | column INSERT | id, content_item_id, version_no, body, title, link_url, payload, source, created_by |
| review_decisions | column INSERT | id, content_item_id, content_version_id, decision, comment, decided_by, decided_at |
| content_items | column UPDATE | status, current_version_no, approved_version_no, updated_at |

There are no other new grants. In particular, the existing audit_logs INSERT and
idempotency/Campaign privileges are reused unchanged; audit_content_insert adds
policy authority, not a new table or sequence grant. The migration checks exact
effective table and column privileges across all 20 tables after its changes.

The connection repository selects exactly the four allowed columns. It cannot
SELECT credentials_ciphertext or SELECT * from platform_connections, and does
not materialize a full connection ORM object. Public response schemas likewise
exclude credentials. These four metadata columns are not access to Meta tokens.

No runtime DELETE grant is added or permitted. content_versions has no UPDATE
or DELETE; review_decisions has no SELECT, UPDATE or DELETE; content_items has
no DELETE. Version and review inserts are explicit and do not require RETURNING.
Audit has no UPDATE/DELETE or sequence access. Content campaign/connection/platform
and publication fields cannot be updated through the new column grants.

## Closed surfaces

No SECURITY DEFINER function, policy helper, service-role bypass, role mutation,
PUBLIC business grant, or anon/authenticated/service_role privilege expansion is
introduced. Preflight rejects existing PUBLIC application table/column grants,
public-schema SECURITY DEFINER functions, or application access by those optional
Supabase principals when present. This describes local checks, not a remote audit.

These unrelated tables remain inaccessible to the runtime:

```text
assets
content_assets
publication_jobs
publication_attempts
ai_generation_requests
webhook_events
whatsapp_conversations
whatsapp_messages
outbox_events
automation_runs
```

Unsafe downgrade always raises:
`Unsafe security downgrade blocked; a reviewed compensating migration is required`.
Offline upgrade is also refused because baseline verification requires a database.
Historical migrations 0001–0004 and frozen sql/003_rls.sql are not rewritten;
sql/003_rls.sql is not executed.

## Application authorization and transaction boundary

The existing CurrentUser and OrganizationContext contracts remain intact. JWT,
active profile/organization/membership and exact-one UUID organization selector
precede business access. Explicit content RBAC remains in FastAPI; SQL tenant
policies do not encode the entire role matrix or state machine.

Creation authority, immutable versions and review records, row-lock ordering,
stale-version checks, creator-review denial and the reasoned owner exception are
described in [the implementation report](PROMPT_7_IMPLEMENTATION_REPORT.md).
RLS does not independently implement owner override or every lifecycle transition.

User and organization GUCs are transaction-local. The existing protected root
transaction owns claim, writes, audit, response and commit. Commit failure prevents
2xx response start. Rollback/failure/cancellation cleanup and pool-size-one reuse
are covered by real PostgreSQL tests without changing production policies or roles.

Idempotency remains isolated by actor and tenant. Global key collisions hidden by
RLS return generic conflict, not foreign stored data. Permissions are rechecked
before replay. Only committed success is durable; no expired key reuse or cleanup
is introduced. Stored response bodies intentionally contain tenant content, so
future retention remains security-relevant work rather than an existing worker.

## Audit redaction and secret handling

The only content audit actions are content.created, content.version_created,
content.submitted_for_review, content.approved and content.changes_requested.
The audit row includes tenant/actor/entity/correlation identifiers; its `after`
metadata keys are exactly organizationId, contentId, currentVersionNo,
previousStatus, status, changedFields, ownerOverride. No body/title/link/comment
values, raw request or credentials are copied into this metadata.

The harness takes credentials from the environment, enforces explicit loopback
host, port 5432, exact disposable database and expected local roles, and redacts
DSNs/passwords from subprocess diagnostics. No credential is documented here.
Fixtures use synthetic UUIDs/content and a synthetic zero-byte ciphertext only.
Cleanup removes only the created test database; retained roles are not dropped.
The child removes its own URL variables; it cannot remove a parent's PowerShell
environment variables, whose cleanup remains the operator's responsibility.

## Evidence and remaining boundaries

The project-owner checkpoint reports **518 passed, 103 skipped, 1 warning**, exit
0, head 20260910_0005, 18 policies, successful cleanup and unchanged retained roles.
This finalization preserves that validated implementation and does not rerun
PostgreSQL. `test_content_security_migration.py` covers static guards and scope;
`test_content_api.py` covers unit/ASGI contracts. Real catalog, denied operations,
cross-tenant/actor collisions, rollback, concurrency and context reuse are in
`test_content_postgres.py`. Static tests alone are not claimed as RLS execution.

Audit/review append counts are observed SQL INSERTs paired with root commit, not
privileged audit readback. No remote security certification or production-ready
claim is made. Supabase, Meta, n8n and Railway were not contacted; no application
deployment, Git publication or subsequent-stage implementation is included.
