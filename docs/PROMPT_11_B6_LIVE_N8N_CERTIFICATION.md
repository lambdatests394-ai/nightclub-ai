# Prompt 11 Stage B6 — Live n8n Integration Certification

## Status

Prompt 11 Stage B6 completed successfully in an isolated local certification environment.

No production infrastructure, Supabase production database, Meta credential, Facebook page, Railway deployment, or public n8n instance was used.

## Baseline

- Branch: `feature/prompt-11-automation`
- Baseline commit: `a0746aaeb102dc594fa614e0c9a2a908e4143356`
- Baseline title: `feat: add versioned n8n publish-due workflow`
- No commit or push was performed during certification.

## Runtime

- Node.js: `v24.21.0`
- n8n: `2.40.7`
- n8n runtime: local isolated instance
- Public exposure: none
- FastAPI binding: `127.0.0.1:8000`
- Workflow: `Night Club AI — Publish Due`
- Workflow remained unpublished/inactive.
- Schedule Trigger did not execute automatically.

## Workflow compatibility

The B5 workflow imported successfully into n8n 2.40.7.

The runtime recognized the expected built-in nodes:

- Schedule Trigger
- Set / Edit Fields
- Crypto
- HTTP Request
- Switch
- Stop And Error

The Crypto node was bound to a temporary local credential named:

`Night Club AI Internal HMAC`

No HMAC secret is included in this report or in the versioned workflow.

## Execution-data settings

Effective local B6 settings included:

- timezone: `Etc/UTC`
- execution logic: `v1`
- failed production execution storage: disabled
- successful production execution storage: disabled
- manual execution storage: disabled
- execution progress storage: disabled
- workflow timeout: 120 seconds

Redaction was unavailable under the local license, so execution persistence was disabled instead.

## PostgreSQL precondition

Disposable database:

`nightclub_ai_prompt11_n8n_live_test`

Validated baseline:

- Alembic head: `20260928_0010`
- RLS policies: 58
- system automation policies: 13
- FORCE RLS application tables: 22
- runtime direct login: `nightclub_api`
- scheduler direct login: `nightclub_scheduler`

Before execution:

- publication_jobs: 0
- publication_attempts: 0
- content_items: 0
- platform_connections: 0

The local-only `scripts/local-dev/auth_stub.sql` fixture was used to provide the Supabase Auth `auth.users` dependency required by migration 0001. It was not treated as a production migration.

## Live HMAC integration

The imported n8n workflow called:

`POST /internal/automation/publish-due`

against the local FastAPI process.

Request characteristics:

- empty request body
- no query parameters
- HMAC-SHA256 authentication
- one timestamp source
- canonical B1/B5 request contract
- redirects disabled
- automatic retries disabled

Two manual workflow executions were performed.

Both returned HTTP 200.

Correlation IDs:

1. `8a52bbe7-e553-4055-972b-ae3fced7a6d0`
2. `940754ad-211a-412c-a28a-0fe291619d8f`

Both were observed in the FastAPI protected-request log.

The FastAPI log did not contain HMAC secrets, request signatures, database passwords, organization IDs, job IDs, or Meta credentials.

## Result

Both manual executions completed with the zero-work result:

- selected: 0
- processed: 0
- published: 0
- reconciledPublished: 0
- retryScheduled: 0
- permanentFailure: 0
- notDue: 0
- leaseUnavailable: 0

No Meta publication request was made.

## Procedural variance

The B6 plan specified one controlled manual workflow execution.

Two manual executions were performed instead.

Both requests were independently HMAC-authenticated and returned HTTP 200.

This did not result in any publication job, publication attempt, content item, platform connection, Meta request, or scheduled n8n execution.

## PostgreSQL post-state

After both executions:

- Alembic head: `20260928_0010`
- publication_jobs: 0
- publication_attempts: 0
- content_items: 0
- platform_connections: 0

Result:

`B6 POST-STATE: PASS`

## Security and cleanup state

- FastAPI stopped cleanly after certification.
- Temporary FastAPI environment secrets were removed from the PowerShell environment.
- The clipboard was sanitized after use.
- No production secret was committed.
- No database role was altered.
- No ACL was altered.
- No RLS policy was altered.
- No migration was added or modified.
- No `pg_hba.conf` change was made.
- No real Meta call occurred.
- No production deployment occurred.

Post-certification cleanup was completed after explicit human approval.

Cleanup verification:

- disposable PostgreSQL database `nightclub_ai_prompt11_n8n_live_test`: removed
- isolated n8n state `%LOCALAPPDATA%\NightClubAI\n8n-b6`: removed
- ports `5678` and `8000`: clear
- `alembic_test_user`: retained unchanged
- `nightclub_api`: retained unchanged
- `nightclub_scheduler`: retained unchanged
- temporary B6 environment variables: cleared
- clipboard: sanitized

## Certification

PROMPT 11 STAGE B6:

LIVE LOCAL n8n RUNTIME + HMAC END-TO-END CERTIFICATION COMPLETE — WORKFLOW STILL INACTIVE — NOT COMMITTED

CERTIFIED:

- REAL n8n -> REAL FastAPI -> REAL PostgreSQL ROLE BOUNDARIES
- 2 MANUAL EXECUTIONS
- 2 HTTP 200 RESPONSES
- ZERO DUE JOBS
- ZERO META CALLS
- ZERO SCHEDULED EXECUTIONS

NEXT CHECKPOINT:

HUMAN REVIEW BEFORE B6 COMMIT AND DEPLOYMENT/ACTIVATION STAGE
