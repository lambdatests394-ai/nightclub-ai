# Prompt 11 B7 — Deployment readiness gate

Date: 2026-09-29.
Branch: `feature/prompt-11-automation`.
Baseline: `301fe0f0c8bb2e0bff1c7f8a042e1aa2bb794529`.

## Status and scope of certification

PASS — B7 static deployment-contract gate complete; no production activation.
This report does not certify production infrastructure, provisioned secrets or
activation. No platform was queried or changed. Production deployment and
activation remain separate human checkpoints.

## Human resolution of historical documentation

Current deployment rules:

- `sql/003_rls.sql` MUST NOT be executed.
- Alembic requires `DATABASE_MIGRATION_URL`; no fallback to `DATABASE_URL`.

The migration 0001 docstring's administrator instruction to apply historical SQL
is SUPERSEDED, not actionable. Its bytes and all historical migrations are unchanged.
ADR-004 retains its original history and now appends the 2026-09-29 supersession.
Executable authority is migration_config.py and migrations/env.py; migration 0003
rejects preexisting policies. ADR-006 had already documented this separation.

Other stale prose was assessed, not silently rewritten: README's early roadmap
says integrations are unimplemented; B5's README/runbook describe live validation
as future work. Those are earlier delivery snapshots, superseded for current
status by B6 evidence and this report. The B6 report's NOT COMMITTED closing line
describes certification-time state; its inclusion in the baseline commit proves
the subsequent checkpoint. None overrides current code or authorizes deployment.

## Evidence reviewed

- core/config.py, core/database.py, core/scheduler_database.py,
  core/database_security.py, core/migration_config.py and app/main.py.
- api/internal/automation.py; modules/automation/internal_auth.py, coordinator.py,
  discovery.py, publish_due_dependencies.py, executor.py, repository.py and audit.py.
- modules/identity/policy.py; integrations/credentials.py, facebook_provider.py
  and dependencies.py; storage and AI composition constructors.
- migrations/env.py, alembic.ini, requirements.txt; migrations 0001, 0003, 0009,
  0010 and the historical grant chain. Actual 0010 filename ends in
  `system_automation_execution_access.py`; auth helper lives under automation,
  not core/internal_auth.py. These filename differences do not change contracts.
- .env.example, README, ADR-001/004/006, local PostgreSQL runbook, n8n README,
  B5 JSON/validator/runbook and B6 live certification report.
- infra/railway contains only a placeholder; no deployment/start manifest exists.
- Focused HMAC/config/workflow/API/coordinator/discovery tests; B2/B3 regression
  source evidence for role and tenant isolation. No PostgreSQL suite rerun.

## Preserved B1–B6 contract

HMAC-SHA256 uses exact method/path/timestamp/body digest, current/previous secrets,
300-second configured skew and constant-time comparisons. Internal request is
zero-body/no-query; order is authenticate, shape-check, composition, discovery,
execution. Status outcomes remain 401/422/generic 503/200.

Scheduler is direct-login, read-only discovery. API installs transaction-local
system_automation context, organization UUID and empty user ID, with system/NULL
actor audit. Durable organization-owned commands survive creator revocation;
inactive organizations terminate ORGANIZATION_INACTIVE before publication.
Limits remain batch 8 / concurrency 4. No new system assets access, automation_runs
or outbox_events behavior. Their preexisting schema declarations are not new
execution use. No replica-wide distributed concurrency guarantee is claimed.

Source target is 20260928_0010, 58 policies / 13 system / 22 FORCE tables from
certified B6. This is historical/local evidence, NOT a production catalog query.
B6 recorded two manual 200s with zero work, accepted by the owner, n8n 2.40.7 /
Node 24.21.0, disabled persistence where redaction was unavailable, and subsequent
approved cleanup. B7 does not repeat live execution.

## Deployment contract and operator gates

The [B7 runbook](runbooks/prompt11-b7-production-deployment-readiness.md) contains
the per-variable consumer/requiredness/secrecy/source/format/validation/rotation/Git
matrix, conditional existing-feature configuration, and production sequence.

- **Credential isolation:** runtime, migration and scheduler URLs are independent.
  Runtime uses asyncpg; migration uses psycopg. n8n receives only its HMAC credential,
  never application DB or Meta credentials. Migration secret is absent from API.
- **Provisioning:** administrators create two nonowner LOGIN/NOINHERIT roles with
  all elevated flags false and no memberships/effective CREATE. Alembic validates
  posture and applies reviewed grants/policies; it creates neither role/password.
  Managed auth.users must already exist; local auth_stub is forbidden remotely.
- **Startup:** documented explicit one-worker Uvicorn command, PORT expansion,
  container bind behind HTTPS edge, no reload/access-log/proxy-header trust by
  default. No naive X-Forwarded-Proto check. Platform configuration is not present
  or silently assumed. Health endpoints do NOT prove database readiness.
- **Failure timing:** bad URL scheme can fail import; unavailable scheduler fails
  authenticated execution with 503. Runtime connection is only exercised when
  work opens a transaction. Zero-work 200 does not prove runtime DB readiness.
  Meta version/cipher are required even before zero-work discovery; missing HMAC
  can coexist with healthy startup but returns 401.
- **n8n:** portable JSON unchanged/inactive; same 11 node versions, canonical
  signing and 200/401/422/503/fallback routes. Production uses HTTPS, no body/query,
  no redirects/retries and verified TLS. No live configuration was performed.
- **Storage/privacy:** production n8n state, encryption material, backups and
  editor authentication must be independently specified. No service env manifest
  exists, so B7 invents no required n8n variable. Confirm effective redaction or
  disable error persistence; keep manual/success/progress saving disabled.
- **Rotation:** documented current/previous HMAC transition, single signer and
  safe process restarts; no key generated or rotated. Meta envelope key is not
  dual-key: rotation requires a separate reviewed data/key migration plan.
- **Executor guard:** exactly one API replica, one worker, one schedule instance;
  verify platform settings and avoid rollout overlap with active triggers.
- **Zero-state gate:** independent privileged read-only census, no due jobs or
  attempts, no automatic executions or Meta calls; producer activity held to avoid
  a check/use race. Verify both DB roles independently and compare structural
  catalog invariants. Invalid-HMAC and signed-invalid-shape probes are future
  controlled checks, not B7 operations.
- **Execution/abort:** phases A–G cover deploy inactive, infrastructure proof,
  one manual zero-work request, human review, explicit activation, observation,
  and abort/deactivation. Rollback never weakens role/RLS controls or automatically
  retries an ambiguous publication. Security downgrade stays blocked.

## Remaining readiness items, not completed production claims

Before deployment/activation the owner must approve the concrete platform manifest,
origins/TLS/proxy path, Python build and pinned n8n runtime, database endpoint/TLS
and role provisioning, durable n8n storage, backup/restore and log retention,
secrets placement, one-replica topology, monitoring/abort ownership and empty-safe
verification window. No production credential or connection is needed to document
these requirements. If any cannot satisfy the frozen checks, stop before deployment.

No new executable B1–B6 contradiction was found in the inspected paths. PostgreSQL
transaction-mode pooling, multi-replica publication and actual Meta publication
remain outside this certification. Production availability is not asserted.

## Local validation results

Executed locally, without PostgreSQL opt-in or live services:

```text
B7_OFFLINE_READINESS: PASS
OFFLINE_WORKFLOW_CONTRACT: PASS
193 passed in 29.15s
py_compile (B7 validator and tests): PASS
git diff --check: PASS
Secret scan: 5 scoped files, 0 candidates
Trailing-whitespace scan: 0 findings
```

The focused command is recorded in the B7 runbook and includes B7, automation
configuration/HMAC, B5 workflow and B4 API/coordinator/discovery tests. No full
PostgreSQL certification was repeated. The B5 validator's NOT_CERTIFIED runtime
label describes that tool's offline scope, not a revocation of B6 human evidence.

The initial B7 raw comparison encountered the known historical CRLF materialization
in migrations 0003–0005. The checker now compares content with CRLF/LF equivalence
without writing those files; the executable 0009/0010 SHA-256 guards remain exact
and pass. Negative tests prove both distinctions. ADR-004 was normalized to LF
only within its authorized edit; its final diff is 21 added lines, zero removals,
and preserves the original historical text.

Final scope: one tracked documentation modification and four new B7 files.
Nothing staged. Application, migration and SQL content unchanged; portable B5
workflow is byte-identical to the certified baseline. HEAD and branch unchanged.

## Safety and Git boundary

REAL_META_CALLS=0
REAL_N8N_CALLS=0
PRODUCTION_MUTATIONS=0
WORKFLOW_ACTIVATIONS=0

No application source, workflow, frozen SQL, migration, ACL, RLS, role or pg_hba change.
No staging, commit, push, PR, merge, tag, deploy, activation or production secret.
Windows status noise is assessed via content diffs; no index/EOL repair performed.

Intended files: ADR-004 appended clarification; this report; B7 runbook; offline
B7 validator; focused validator tests. Test mutations affect temporary fixtures
only, never the actual certified source tree.
