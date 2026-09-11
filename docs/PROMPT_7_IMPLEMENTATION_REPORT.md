# Prompt 7 Stage B — Implementation and validation report

Date: 2026-09-11

## Baseline and evidence provenance

Branch: `feature/prompt-7-content-workflow`.
Original baseline and uncommitted HEAD: `418dfff16c342eca4c9b67ae5282d826ab90a097`.

The project owner supplied the successful real local PostgreSQL checkpoint:
`518 passed, 103 skipped, 1 warning`, harness exit code `0`. This report records
that accepted evidence, not a new PostgreSQL execution during finalization.
Finalization adds only this report and `PROMPT_7_SECURITY.md`; the validated
implementation, migration, PostgreSQL tests and harness are preserved.
Commit authorization remains a separate human checkpoint. No commit, push, PR,
merge, tag, deployment or remote Supabase application is claimed.

## HTTP contract

| Method | Path | Success | Permission |
| --- | --- | --- | --- |
| POST | `/api/v1/content` | 201 | `content:write` |
| GET | `/api/v1/content` | 200 | `content:read` |
| GET | `/api/v1/content/{content_id}` | 200 | `content:read` |
| PATCH | `/api/v1/content/{content_id}` | 200 | `content:write` |
| POST | `/api/v1/content/{content_id}/submit-review` | 200 | `content:write` |
| POST | `/api/v1/content/{content_id}/review` | 200 | `content:review` |

Verified JWT and exactly one UUID `X-Organization-Id` select an active membership,
profile and organization. Invalid organization context is 403; absent/foreign
content or references are hidden with 404. Lifecycle, self-review and stale
version conflicts are 409. Invalid bodies/keys are 422. Mutations require exactly
one UUID `Idempotency-Key`. Submit-review accepts no body.

Responses use camelCase and a current-version projection, not revision history,
review records, raw payloads or connection credentials. Collection pagination
uses ascending immutable UUIDs, default limit 50, maximum 100, and `nextCursor`.
Correlation metadata belongs to the current HTTP request, including replays.

| Role | Read | Create/PATCH/submit | Review |
| --- | --- | --- | --- |
| owner | Yes | Yes | Yes |
| manager | Yes | Yes | Yes |
| editor | Yes | Yes | No |
| reviewer | Yes | No | Yes |
| operator | Yes | No | No |
| viewer | Yes | No | No |

## Lifecycle and immutable versions

Create inserts a tenant/actor-owned draft and manual version 1 atomically.
Platform input is limited to Facebook/WhatsApp; neither platform is contacted.
Server-owned fields and asset identifiers are forbidden in request bodies.

PATCH accepts only supplied `body`, `title`, `linkUrl` fields. Empty or semantic
no-op edits are rejected. Only draft, changes_requested and approved are editable.
An edit inserts a new complete version, increments `currentVersionNo`, returns
to draft and clears `approvedVersionNo`; existing versions are never updated.

Submit-review requires draft and moves it to in_review. Review requires the
current version number and in_review status. Approval records that exact version
as approved; changes_requested clears the approved version. Each successful
review appends one decision referencing the exact content/version pair.

Creators cannot review themselves except an owner supplying a nonblank comment.
This exception applies to either review decision and records `ownerOverride` in
structural audit metadata; the comment itself stays out of audit metadata.

## Campaign and connection checks

An optional campaign must exist in the selected tenant and must not be archived.
Every new content mutation checks the linked campaign; archived campaigns block
mutations but not content reads. Committed idempotent replay is not a new mutation.

An optional connection is validated on creation: same tenant, active status and
matching platform. Only `id`, `organization_id`, `platform`, `status` are selected.
PATCH cannot reassign campaign, platform or connection. This is metadata
validation, not token validation or an external provider availability check.

## Root transaction, idempotency and audit

JWT verification precedes the protected root transaction. The existing identity
path establishes transaction-local user context, validates membership, constructs
OrganizationContext, then establishes transaction-local organization context.
The service checks permission before attempting idempotent replay.

One root transaction owns idempotency claim, content/version/review writes,
structural audit, durable response and commit. Repository, domain workflow and
content service do not commit. The function-scoped dependency finishes commit
before HTTP response start; a failed commit produces no successful 2xx response.
Existing sanitized 503 responses retain `Retry-After: 30`.

Idempotency uses the existing durable store, with a content-specific operation
namespace and request fingerprint including organization, actor and payload.
Same authorized request/key replays a committed 200/201 result; changed requests
or hidden actor/tenant collisions return generic 409 without exposing a response.
Concurrent equal keys serialize to one mutation. Failed/uncommitted operations
do not leave durable results. The minimum replay horizon remains 24 hours;
expired keys are not deleted or automatically reused in this stage.

The shared IdempotencyConflict error is domain-neutral, with a backwards-compatible
campaign import alias; the existing store algorithm and Campaign HTTP contract
are unchanged.

Audit actions are exactly `content.created`, `content.version_created`,
`content.submitted_for_review`, `content.approved`, `content.changes_requested`.
Create emits only content.created, not an additional version-created event.
Replays emit no second event. Audit shares the root transaction and stores only
identifiers, actor, status/version, correlation and structural metadata. No body,
title, link or comment values are copied into audit records.

## Concurrency and validation evidence

Lock order is idempotency key, then existing content row, then optional campaign
row. Creation locks its optional campaign after the key. Campaign mutations do
not acquire content locks. Content state/current version is read after acquiring
the content lock. This prevents competing edits from assigning the same version
and competing reviewers from both transitioning in_review.

The campaign lock prevents archive from committing between campaign validation
and the content mutation commit. Its conservative exclusive scope can serialize
different content mutations linked to one campaign. No throughput benchmark or
dedicated simultaneous archive/content stress test is claimed.

Real PostgreSQL coverage in `backend/tests/test_content_postgres.py` includes:

- `test_concurrent_patch_versions_serialize` (two connections; versions 2 and 3).
- `test_concurrent_key_one_mutation_or_conflict` (equal/different requests).
- `test_concurrent_reviewers_one_transition_and_no_failed_claim`.
- `test_stale_review_cannot_approve_new_version`.
- `test_rollback_clears_partial_rows_and_claim` (version/audit/idempotency faults).
- `test_root_pool_one_cleanup` (commit, rollback, SQL error, exception; same backend PID).
- `test_cancellation_releases_content_lock_and_no_context_leak`.
- `test_campaign_archive_blocks_later_content_mutation`.

Audit/review INSERT counts use SQL execution observation paired with successful
root commit, not privileged readback or extra SELECT/test-bypass grants.

## Migration and accepted PostgreSQL checkpoint

Only Prompt 7 migration: `20260910_0005_content_business_access.py`.
Revision: `20260910_0005`; parent: `20260910_0004`. No table redesign.
The ORM uses existing PostgreSQL enums without creating replacement types.
The eight new policies and exact grants are listed in [PROMPT_7_SECURITY.md](PROMPT_7_SECURITY.md).

Accepted real checkpoint:

- Chain 0001 -> 0002 -> 0003 -> 0004 -> 0005: PASS.
- Existing Prompt 5/6 policies unchanged; 18 total policies; 20 ENABLE/FORCE tables.
- Suite: **518 passed, 103 skipped, 1 warning**; harness exit **0**.
- Unsafe downgrade to 0004 blocked with the reviewed compensating-migration requirement.
- Final `ALEMBIC_HEAD = 20260910_0005`.
- Disposable `nightclub_ai_prompt7_test` removed.
- `nightclub_api` and `alembic_test_user` retained unchanged.
- Supabase untouched; frozen `sql/003_rls.sql` not executed.

The independent `validate_prompt7.py` accepts environment-only credentials,
requires explicit loopback host/port/database/roles, refuses an existing test
database, and never provisions or mutates roles. Earlier harnesses stay pinned
to their historical revisions and are not rewritten for 0005.

## Final non-PostgreSQL gates

Run after both documents are present, without PostgreSQL opt-in:

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/test_content_api.py backend/tests/test_content_security_migration.py -q
.\.venv\Scripts\python.exe -m pytest backend/tests -q -ra
.\.venv\Scripts\python.exe -m compileall -q backend scripts/local-dev
.\.venv\Scripts\python.exe -m pip check
git diff --check
```

Finalization execution results: **114 focused passes** and
**456 passed / 165 skipped / 1 warning** in the full non-PostgreSQL suite.
compileall exited 0; pip check reported `No broken requirements found.`;
git diff --check exited 0. The changed-file secret scan found no real credentials;
all 24 delivery files are in scope. SHA-256 comparisons confirmed the 22 existing
implementation/test/harness files were unchanged during documentation finalization.
Migration-history integrity passed. These checks are not a new PostgreSQL run.

The existing warning is `StarletteDeprecationWarning`: using httpx with
starlette.testclient is deprecated in favor of httpx2. No dependency change was
made as part of this documentation-only finalization.
The 165 opt-in skips consist of 12 Prompt 4, 43 Prompt 5, 48 Prompt 6 and 62 Prompt 7
tests. The accepted real run activates the 62 Prompt 7 tests and retains the 103
historical opt-in skips.

## Complete working-tree delivery inventory

Ten tracked modifications:

```text
backend/app/main.py
backend/app/modules/campaigns/errors.py
backend/app/modules/content/models.py
backend/app/modules/content/schemas.py
backend/app/modules/content/state_machine.py
backend/app/modules/content/workflow_service.py
backend/app/modules/identity/policy.py
backend/app/shared/idempotency.py
backend/tests/conftest.py
backend/tests/test_content_workflow_service.py
```

Twelve new implementation/test/harness files plus two finalization documents:

```text
backend/app/api/v1/content.py
backend/app/modules/content/audit.py
backend/app/modules/content/dependencies.py
backend/app/modules/content/errors.py
backend/app/modules/content/repository.py
backend/app/modules/content/service.py
backend/app/shared/errors.py
backend/migrations/versions/20260910_0005_content_business_access.py
backend/tests/test_content_api.py
backend/tests/test_content_postgres.py
backend/tests/test_content_security_migration.py
scripts/local-dev/validate_prompt7.py
docs/PROMPT_7_IMPLEMENTATION_REPORT.md
docs/PROMPT_7_SECURITY.md
```

The legacy workflow tests now assert no nested commit, consistent with root
transaction ownership. Existing pure scheduling helpers remain unexposed; there
is no new scheduling/publication route or runtime grant.

## Limitations and deferred scope

No history/review-list endpoint, assets/storage, AI generation, Instagram API,
scheduling, publishing, WhatsApp integration, workers/n8n, remote infrastructure,
deployment or subsequent-prompt implementation is included. The reserved Instagram
database enum remains for compatibility; create input rejects it.

RLS provides tenant/actor defense in depth, not a replacement for application
RBAC or lifecycle validation. Runtime compromise within a valid tenant context
is not claimed to be prevented by every business rule at SQL level. Replay data
and immutable versions need a future approved retention strategy; no cleanup
worker or production readiness certification is part of this checkpoint.
