# Prompt 11 B7.1 recertification runbook

## Purpose

Use this runbook to verify the explicit B7.1 source certification for the empty
publish-due batch correction. It is an offline review step. It performs no
deployment, database connection, n8n execution, Meta call or production mutation.

- Historical B7 baseline: `301fe0f0c8bb2e0bff1c7f8a042e1aa2bb794529`.
- Reviewed B7.1 baseline: `b228ea865ff4dcbf43f92b5c47caa7584c887367`.
- Expected relationship: **HISTORICAL_B7: PRESERVED_AND_SUPERSEDED**.

## Offline procedure

1. Keep PR #17 in draft state.
2. Run the historical B7 validator and require
   `B7_OFFLINE_READINESS: FAIL (CERTIFIED_SOURCE_DRIFT)`. Any PASS means the
   historical certification was weakened or rewritten and is a stop condition.
3. Run `python scripts/local-dev/validate_prompt11_b7_1_recertification.py`.
4. Require `B7_1_RECERTIFICATION: PASS` and the exact reviewed baseline above.
5. Run the B7/B7.1, publish-due API, coordinator, discovery, HMAC, migration and
   full backend test suites. Record results in the draft PR.
6. Recheck that the PR remains draft. Do not merge, deploy, publish or activate
   the n8n schedule as part of B7.1.

The B7.1 validator must reject unreviewed changes to production composition,
credentials, the portable workflow, historical B7 evidence, migrations, SQL or
the authoritative constraints below.

## Authoritative deployment constraints

`sql/003_rls.sql` MUST NOT be executed.

Alembic requires `DATABASE_MIGRATION_URL`; no fallback to `DATABASE_URL`.

The expected offline result is **B7.1_RESULT: PASS**. It does not certify current
production data, credentials, deployment topology or n8n instance state.

**PRODUCTION_STATE: NOT_VERIFIED; ACTIVATION: NOT_AUTHORIZED**

`REAL_META_CALLS=0; REAL_N8N_CALLS=0; PRODUCTION_MUTATIONS=0; WORKFLOW_ACTIVATIONS=0`
