# Prompt 11 — B7.1 explicit recertification

**B7.1_RESULT: PASS**

## Decision

B7.1 recertifies the reviewed change that defers Meta adapter and credential-cipher
construction until scheduler discovery has selected at least one publication job.
A correctly authenticated empty batch may therefore return HTTP 200 with the eight
business counters at zero without requiring Meta configuration. A nonempty batch
still constructs the same executor and still requires valid Meta configuration
before executor work begins.

This is a new certification boundary. It does not amend or reinterpret B7.

- Historical B7 baseline: `301fe0f0c8bb2e0bff1c7f8a042e1aa2bb794529`.
- B7.1 reviewed baseline: `b228ea865ff4dcbf43f92b5c47caa7584c887367`.
- **HISTORICAL_B7: PRESERVED_AND_SUPERSEDED**.
- The historical B7 validator must fail against the new source with
  `CERTIFIED_SOURCE_DRIFT`.
- The B7.1 validator pins the reviewed implementation, regressions, historical B7
  artifacts, portable workflow, active security surfaces, migrations and SQL.

## Scope and evidence

The reviewed regressions establish these boundaries without external calls:

1. Empty discovery returns 200 and the existing zero-count response envelope.
2. Meta constructors are not called for an empty batch.
3. A selected job with absent, partial or malformed Meta configuration returns the
   existing generic 503 before executor work.
4. Multiple selected jobs share one request-local executor.
5. Discovery failures remain generic 503 responses and cannot masquerade as empty
   successful batches.
6. Authentication and request-shape validation still precede discovery and Meta:
   unsigned requests return 401; signed invalid queries return 422.

Historical migrations and SQL remain frozen. The narrow LF/CRLF allowance for
migrations 0003–0005 accepts only the two exact byte representations whose
normalized text is identical. It does not accept any third digest. Migration hash
guards remain byte-exact.

The portable n8n workflow remains byte-identical and inactive. HMAC behavior,
scheduler/runtime role separation, leases, idempotency, concurrency, error
redaction and response shape remain within the previously reviewed contract.

## Authoritative deployment constraints

`sql/003_rls.sql` MUST NOT be executed.

Alembic requires `DATABASE_MIGRATION_URL`; no fallback to `DATABASE_URL`.

B7.1 is offline source evidence. It does not inspect Railway, Supabase, n8n, live
roles, live migrations, current counters, secrets or deployed images. The PR must
remain draft until this recertification and its complete test suite are recorded.

**PRODUCTION_STATE: NOT_VERIFIED; ACTIVATION: NOT_AUTHORIZED**

`REAL_META_CALLS=0; REAL_N8N_CALLS=0; PRODUCTION_MUTATIONS=0; WORKFLOW_ACTIVATIONS=0`

