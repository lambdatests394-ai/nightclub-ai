# Prompt 6 Stage B implementation report

Date: 2026-09-10

## Delivery status update

The numbered sections below preserve the Stage B pre-commit implementation snapshot
that was reviewed before publication. After that checkpoint, the implementation was
committed as `947b8f2484483ee56cd3655e67d42f86c19cb7f4`, pushed to
`feature/prompt-6-campaign-api`, and opened as PR #4 targeting `develop`. The PR
code/security review passed; this documentation-only follow-up reconciles status
wording. PR #4 remains unmerged pending explicit human authorization. No Supabase
deployment occurred and Prompt 7 has not started.

## 1. Branch and baseline

Active branch: `feature/prompt-6-campaign-api`.

HEAD/baseline: `c85643dd3f635e2205b05f7b41a9e9adbaadc95b`.
Starting develop matched origin/develop, working tree was clean, and main/develop
trees matched. main remains `7a5f5465431a31ae519c6a27d1839d938751353c`;
develop remains the baseline. Branch creation required sandbox permission;
the first denied attempt changed no refs and the authorized retry succeeded.

## 2. Complete changed/new file scope

12 tracked modified files:

- README.md
- backend/app/main.py
- backend/app/modules/audit/models.py
- backend/app/modules/campaigns/models.py
- backend/app/modules/campaigns/schemas.py
- backend/app/modules/identity/dependencies.py
- backend/app/modules/identity/policy.py
- backend/app/shared/models.py
- backend/tests/conftest.py
- backend/tests/test_database_security.py
- docs/runbooks/local-postgres-validation.md
- scripts/local-dev/validate_prompt5.py

15 new files (including this report):

- backend/app/api/v1/campaigns.py
- backend/app/modules/audit/writer.py
- backend/app/modules/campaigns/dependencies.py
- backend/app/modules/campaigns/errors.py
- backend/app/modules/campaigns/repository.py
- backend/app/modules/campaigns/service.py
- backend/app/shared/idempotency.py
- backend/migrations/versions/20260910_0004_campaign_business_access.py
- backend/tests/test_campaign_security_migration.py
- backend/tests/test_campaigns.py
- backend/tests/test_campaigns_postgres.py
- docs/PROMPT_6_SECURITY.md
- docs/PROMPT_6_IMPLEMENTATION_REPORT.md
- docs/adr/ADR-007-campaign-transaction-idempotency-and-security.md
- scripts/local-dev/validate_prompt6.py

## 3. Implementation

Five campaign endpoints under /api/v1 with validated unique organization header,
opaque cursor pagination, strict mutation schemas, draft creation, supplied-only
PATCH, and row-locked archive/updates. ORM aligned to existing campaign_status,
timestamptz and audit GENERATED ALWAYS AS IDENTITY; no table redesign.

## 4. RBAC actually implemented

| Permission | owner | manager | editor | reviewer | operator | viewer |
| --- | --- | --- | --- | --- | --- | --- |
| campaign:read | yes | yes | yes | yes | yes | yes |
| campaign:write | yes | yes | yes | no | no | no |
| campaign:archive | yes | yes | no | no | no | no |

All explicit sets, no owner bypass or automatic future-permission expansion.
Frozen identity values and existing identity HTTP behavior are unchanged.

## 5. RLS/grants implemented and validated

0004 contains seven new policies on campaigns/idempotency/audit, preserving three
bootstrap policies and 20 ENABLE/FORCE tables. Runtime gets campaign and key
SELECT/INSERT, named UPDATE columns only, audit INSERT only. No DELETE, audit
read/update, or sequence grant. INSERT generated identity uses the internal
sequence without direct nextval access; the real harness validated that path.
Role/policy/effective-privilege baseline checks precede DDL; downgrade blocked.
Exact policy/grant definitions are recorded in PROMPT_6_SECURITY.md.

## 6. Idempotency and concurrency

PostgreSQL global UUID claim, READ COMMITTED verification, actor/tenant-scoped
lookup and generic 409 for hidden/mismatched collisions. Canonical operation,
resource and semantic payload fingerprint. A key lock always precedes the single
campaign row lock. Only 2xx data is completed within the root transaction.
Authentication/authorization and all failed/uncommitted operations are excluded
from durable replay. Fresh correlation metadata on each request.
Expiry = creation + 24h; expired keys are still replayed, never reused/deleted.
Concurrency behavior passed the independent real PostgreSQL suite; the unit mocks
remain supporting tests and are not presented as the database proof.

## 7. Commit-before-response proof

Installed FastAPI 0.136.3 was inspected locally: its function dependency stack
exits before response(scope, receive, send). Campaign dependency explicitly uses
scope=function and reuses the original protected root/session cleanup.

test_real_function_dependency_commit_before_response executes the actual route,
dependency and production get_db_session with mocked session I/O. ASGI response
start events assert exactly [commit, 201] on success and [commit, 503] on injected
commit failure; no successful response is sent in the failure case. Rollback and
close are awaited. Both parameterizations PASS. This is HTTP transaction-boundary
proof, not a claim that PostgreSQL was exercised in these two tests.

## 8. Audit

Append-only inline Core INSERT, no implicit RETURNING. Structural IDs, actor,
status, dates and changed field names; no name/objective/brief values. Same root
transaction as mutation and replay result. Repeat archive and replay emit no
second event. Unit SQL compilation/redaction tests pass; real minimum-grant
INSERT, rollback and committed INSERT counting passed the opt-in real-PG tests.

## 9. Tests executed

Full suite without PostgreSQL opt-in:

```text
342 passed, 103 skipped, 1 warning in 5.27s
```

Skipped: 12 historical Prompt 4 + 43 historical Prompt 5 + 48 new Prompt 6
PostgreSQL tests. The warning is the existing Starlette/httpx TestClient
deprecation. No PostgreSQL role provisioning or destructive validation rerun.

The initial focused new run passed 61 tests / 48 opt-in skips; an additional
21 unit/HTTP cases were then added and included in the 342-pass full run.
Final focused run: `82 passed in 2.07s` (exit 0).
Identity, content and database security regression suites passed.

## 10. Real PostgreSQL result

PASS by the human-reviewed independent local checkpoint:

```text
validate_prompt6.py: exit 0
0002 -> 0003: PASS
0003 -> 0004: PASS
BOOTSTRAP_UNCHANGED: PASS
TOTAL_POLICIES: 10
390 passed, 55 skipped, 1 warning
expected security downgrade 0004 -> 0003: BLOCKED
post-refusal catalog: 1 passed, 47 deselected
ALEMBIC_HEAD: 20260910_0004
TEST_DATABASE_REMAINING: []
nightclub_api: retained unchanged
alembic_test_user: retained unchanged
```

The exact approved downgrade refusal was: `Unsafe security downgrade blocked; a
reviewed compensating migration is required`. Supabase was not contacted. No
credential value is present in this report or repository.

## 11. Alembic head

Local revision discovery (`python -m alembic heads`):

```text
20260910_0004 (head)
```

Validated database head: `20260910_0004`. Its parent is 20260909_0003. Migrations
0001/2/3 and frozen SQL 001/2/3 are unchanged. Historical SQL 003 was not executed.

## 12. Historical isolation

validate_prompt5.py now calls upgrade 20260909_0003 explicitly, not head.
The existing mocked harness-flow test changes only that expected command.
Prompt 5 catalog/RLS test file is unchanged; none of its security expectations
were altered to fit 0004. New AST-based test verifies the pin and separate harness.

## 13. Quality and secret checks

- compileall backend and scripts/local-dev: PASS.
- pip check: `No broken requirements found.`
- git diff --check: PASS; Git emits existing Windows LF/CRLF normalization notices.
- ORM alignment and SQL compilation checks: PASS (not real migration validation).
- Changed/new-file secret scan: no real credentials detected. Two URL-pattern
  matches in runbook lines 21/79 were confirmed unchanged from HEAD and contain
  explicit angle-bracket password placeholders. No values were printed.
- Frozen SQL/migrations, auth_stub and content service/state machine: unchanged.
- No third-party service, Supabase, Meta, n8n or deployment access occurred.

## 14. Working tree

Intentionally dirty: 12 tracked modifications + 15 untracked files, 27 total.
Nothing staged. No commit, push, PR, merge, tag, rebase or branch deletion.
main/develop refs remain unchanged. No role/password changes.

## 15. Risks/deferred limits

The real PostgreSQL gate is closed: migration baseline, effective privileges,
generated identity insert, unique-key/RLS concurrency, rollback and pool/context
cleanup passed the independent harness and human checkpoint.

Audit INSERT count evidence uses execution events plus transaction success,
not a privileged table readback; no test bypass policy is added. Pagination is
stable ordering, not a snapshot across changing data. Error replay is deliberately
not durable. No automatic idempotency expiry cleanup; storage growth is deferred
to future retention work. No arbitrary-SQL/compromised-credential protection is
claimed beyond the approved GUC trust model. No deployment or remote validation.

## 16. Final verdict

PROMPT 6 STAGE B — PASS / READY FOR HUMAN REVIEW

Implementation, safe checks and real local PostgreSQL validation passed human
review. Commit authorization remains a separate operation. No Prompt 7 work or push.
