# Prompt 8 — Asset and attachment security boundaries

Date: 2026-09-22

## Authorization and transaction ownership

The existing JWT, CurrentUser, OrganizationContext and protected root transaction contracts remain unchanged.
Exactly one UUID organization selector is required. `asset:read` covers all six roles;
`asset:write` covers owner/manager/editor only. Content attachments use existing `content:write`.
Unknown roles and permissions remain denied.

Repositories always constrain organization in addition to RLS. Missing and foreign assets are hidden as 404.
Deleted download targets are also 404. New-key terminal completion and non-ready references are 409.
Authorization occurs before replay; UUID collisions from another actor/tenant never reveal saved response data.

Upload intent, structural audit and durable idempotency response commit in one protected transaction.
`AssetCoordinator` calls signing only after that context exits successfully.
Simulated commit failure tests confirm no signing; signing failure leaves a recoverable pending row and returns 503/Retry-After 30.
Capabilities, signed tokens, temporary headers and correlation IDs are absent from persisted replay bodies.
There is no domain/repository commit or distributed rollback.

## Storage capability boundary

The server alone owns the canonical private bucket and object key. Request schemas forbid authority fields.
Upload is create-only with `upsert=false`; its provider-defined validity is 7200 seconds, not application-configurable.
Signed download TTL defaults to 60 seconds. API responses are no-store and URLs are not persisted.
The configured bucket must independently enforce the JPEG/PNG/WebP allowlist and 10 MiB limit.
No live bucket configuration or validation occurred in Stage B.

The httpx adapter restricts every authenticated request and returned capability to the configured HTTPS origin and exact path.
TLS remains verified, environment proxy use is disabled, redirects are disabled and timeouts are explicit.
Credentials are per-request headers, server-only and excluded from Settings serialization/repr.
No arbitrary provider URL supplied by a client or stored as metadata is fetched.
Raw provider responses and exception messages never become public error details.
Provider protocol faults map to 502, temporary availability faults to 503/Retry-After 30.

Signed capabilities remain bearer capabilities until expiration. Prompt 8 introduces neither revocation nor object deletion.
The service credential is not used for runtime PostgreSQL authentication or to bypass application-table RLS.
The adapter is tested only with mocked HTTP, not a live Supabase service.

## Byte-level image decision

The verifier bounds the stream while counting and hashing actual original bytes.
Declared length, Storage metadata and ETag are not substitutes for SHA-256 verification.
The client digest is an expected digest; a ready asset means it matched the server calculation.
Rejected rows retain the original expectation and do not imply that digest was verified.

JPEG/PNG/WebP must fully decode as static images within 10,485,760 bytes,
8192x8192 axis limits and 20,000,000 decoded pixels. Pillow decompression protection remains enabled.
Malformed/truncated images, APNG/animated WebP and recognized GPS/geolocation metadata are rejected.
GPS EXIF pointers and known geolocation text/XMP markers are checked; benign EXIF is allowed.
No image bytes are stripped or re-encoded. This is not a malware, steganography or exhaustive metadata certification.

Complete holds the asset decision lock inside the root transaction during bounded provider I/O.
Timeout/cancellation rolls back the transition and replay claim. The HTTP decision has a 15-second timeout.
An already-running Python decoding thread cannot be forcibly killed; it receives only bounded bytes and never a DB session,
so an abandoned result cannot change persistence. Concurrency and provider throughput require future operational sizing.

## RLS and append-only attachment closure

Migration 0006 requires exact 0005 version/security state and rejects drift.
Six policies extend the expected total to 24 while preserving the prior 18 and 20 ENABLE/FORCE tables.
No PUBLIC or Supabase-principal grants, SECURITY DEFINER, role membership or role attribute changes are introduced.

Assets INSERT requires the tenant, current uploader, pending image status, valid SHA syntax, media bounds,
canonical destination and empty verification fields. Narrow UPDATE permits only pending -> ready/rejected,
valid dimensions for ready and null dimensions for rejected. Immutable asset identity/destination/digest fields have no UPDATE privilege.
No runtime DELETE exists.

ContentAsset SELECT is tenant scoped through its item/version. INSERT additionally requires:

- Matching current organization for item and ready asset.
- Current ContentVersion of a draft ContentItem, created by the current user.
- Position 0 through 9 and the existing composite FKs/unique constraints.
- `attachment_creation_xid = pg_current_xact_id()`.
- `created_at = transaction_timestamp()`.

The xid8 column is added without a default, then receives its future-insert default.
Historical rows stay NULL. Application INSERTs omit the marker; it is absent from public schemas;
runtime cannot explicitly INSERT or UPDATE it. Existing table SELECT implicitly permits internal policy evaluation/read,
but no additional marker write privilege is granted. ContentAsset UPDATE/DELETE and broad ContentVersion UPDATE remain unavailable.

The full XID closes attachments after the creating transaction; the timestamp adds a restore/import guard.
This is enforced by the policy, not xmin, a custom GUC or a privileged function.
RLS constrains row/result authority, while FastAPI still owns workflow sequencing and the actual image verification decision.
Transaction-local GUCs are not protection against an attacker who already holds unrestricted runtime SQL execution;
that existing Prompt 5 trust boundary is unchanged.

## Audit and replay redaction

Actions: `asset.upload_intent_created`, `asset.ready`, `asset.rejected`.
Only structural IDs, states, MIME, byte count, dimensions and controlled rejection codes are copied.
No filenames, bytes, signed URLs, tokens, credentials, parser messages or provider JSON are copied.
Content version audits may record `assetIds` as a changed-field name and `assetCount`, never filenames or file content.
Replay writes no duplicate asset/audit rows. Existing audit access remains append-only without runtime SELECT/UPDATE/DELETE.

## Evidence and remaining boundaries

Executed focused non-PostgreSQL suite: **196 passed**.
Executed full non-PostgreSQL suite: **653 passed, 233 skipped, 1 existing warning**.
The 233 skips are opt-in database tests; 63 belong to Prompt 8.
Tests exercise actual ASGI composition, commit failure before signing, provider failures, replay capability separation,
RBAC, validators, real Pillow decoding, structural audit projection, static migration guards and historical content replay compatibility.

Real local PostgreSQL validation: **PASS — `validate_prompt8.py` exited 0**.
Migration `20260922_0006` was successfully exercised against local PostgreSQL and remained the final Alembic head.
The Prompt 8 PostgreSQL suite completed with **716 passed, 170 skipped, 2 warnings in 22.73s**, exit status 0.
The 170 skips are opt-in suites for earlier PostgreSQL harnesses not enabled by `--prompt8-postgres`.

The catalog verified exactly 24 policies, with all 18 historical Prompt 5/6/7 policies preserved unchanged.
Real RLS security and attachment immutability tests exercised actor/tenant authority, pending/terminal updates,
historical NULL closure, same-transaction attachment success, post-commit denial, current-version/draft/creator checks,
immutable marker privileges, rollback, concurrency and connection reuse/cancellation.
Wrong-XID/old-timestamp fixtures changed only a column default in the disposable DB as its owner,
restored it in `finally`, and retained real runtime INSERT/RLS checks without changing policies, FORCE, grants or roles.

The expected 0006 -> 0005 unsafe downgrade was blocked successfully with the approved compensating-migration guard.
Post-downgrade catalog validation completed with **1 passed, 62 deselected**, exit status 0.
Runtime connectivity passed. The disposable `nightclub_ai_prompt8_test` database was removed and
`TEST_DATABASE_REMAINING: []` was confirmed. Retained roles `nightclub_api` and `alembic_test_user` remained unchanged.
No frozen SQL or historical migration was changed or executed.
No Supabase, Meta, n8n or Railway access occurred; no commit, push, PR, merge or tag was performed.
This local checkpoint does not imply production security certification, live Supabase validation or deployment validation.
