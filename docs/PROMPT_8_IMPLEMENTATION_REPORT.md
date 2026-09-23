# Prompt 8 Stage B — Asset lifecycle and private storage

Date: 2026-09-22

## Checkpoint

Implementation is **COMPLETE** and ready for commit review; no commit, push, PR, merge or tag was created.
Branch: `feature/prompt-8-assets-storage`.
Base and current HEAD: `8fb39c45947e6e82982eefbdd56868fe5c223d39`.
The dirty, unstaged working tree is the intended delivery at this checkpoint.

Real local PostgreSQL validation: **PASS — `validate_prompt8.py` exited 0**.
Migration 0006 and the Prompt 8 PostgreSQL security tests were exercised successfully.
There has been no live Supabase, Meta, n8n or Railway access.

## Implemented HTTP contract

Exactly three asset endpoints are registered:

| Endpoint | Permission | Result |
| --- | --- | --- |
| `POST /api/v1/assets/upload-url` | `asset:write` | 201 new intent, 200 compatible ready reuse |
| `POST /api/v1/assets/{asset_id}/complete` | `asset:write` | 200 ready or terminal rejected result |
| `GET /api/v1/assets/{asset_id}/download-url` | `asset:read` | 200 ready asset capability |

Read is allowed to owner, manager, editor, reviewer, operator and viewer.
Write is allowed only to owner, manager and editor. Content attachment uses `content:write`.
Authentication and the single UUID `X-Organization-Id` use the existing protected identity flow.
Both POST operations require exactly one UUID `Idempotency-Key`.
Complete accepts no body, including no empty JSON object.

Upload fields are `filename`, `mimeType`, `byteSize`, and a 64-character lowercase hexadecimal `sha256`;
unknown/authority fields are rejected. Positive integer byte size is strict, with the configured maximum enforced.
Filename validation rejects paths, controls, bidi, hidden names and dangerous/double extensions.
Unicode normalization and a bounded ASCII slug produce a server-chosen extension.
The raw name is metadata only and is never an object path or audit field.

Responses retain `data` and `meta.correlationId`; all protected responses use `Cache-Control: no-store`.
Upload `data.asset` is durable; `data.upload` is ephemeral and may be null.
Download returns `assetId`, `downloadUrl` and `expiresAt`.
Public asset projections omit storage bucket/key and the internal transaction marker.
Deleted assets are hidden as 404 on download. Non-ready assets produce 409.
New-key completion of any terminal state produces 409; same completed key replays.

## Explicit commit-before-signing

`AssetCoordinator` enters an explicit async transaction context. JWT validation precedes it.
Inside `protected_session`, runtime role verification and transaction-local user context precede membership resolution,
organization context, permission checking, idempotency, the pending asset, structural audit and durable result.
Only after the context exits and the root commit succeeds does `create_signed_upload()` execute.
No repository or domain service calls `commit()`.

A simulated root commit failure is tested through the actual FastAPI dependency composition and produces no signing call.
A signing failure after successful commit returns sanitized 503 with `Retry-After: 30` while leaving the pending intent recoverable.
There is no distributed rollback. An authorized pending replay reuses the same asset and may receive a different signed URL.
The durable idempotency body contains no URL, signed token, ephemeral headers, provider JSON or correlation ID.

Same-tenant ready hash reuse requires compatible MIME and size and creates no upload capability or additional asset audit.
Pending with another key, rejected and deleted duplicate hashes return 409.
Cross-tenant hash existence is never exposed. PostgreSQL uniqueness arbitrates concurrent hash intents;
the shared idempotency store remains unchanged and retains actor/tenant isolation and its 24-hour minimum replay horizon.

## Storage and verification

`StorageProvider` has only `create_signed_upload`, `stat_object`, `stream_object`, and `create_signed_download`.
The httpx Supabase adapter uses a configured HTTPS origin, server-only credential, verified TLS,
`trust_env=False`, explicit timeouts and disabled redirects. Signed URLs are validated against the expected origin and exact object path.
Provider responses/errors are normalized; no raw error body is returned.
Tests use MockTransport or a provider fake, never Supabase.

Canonical private bucket: `nightclub-assets`.
Canonical key: `org/{organizationId}/assets/{assetId}/{sanitizedFilename}`.
Upload signing always requests `upsert=false`. Its effective lifetime is provider-defined at 7200 seconds;
there is no configurable upload TTL. Download TTL defaults to 60 seconds and may be configured more conservatively.
Future bucket provisioning must enforce private access, the MIME allowlist and a 10,485,760-byte size restriction;
no bucket was provisioned or verified live in this stage.

Accepted bytes must decode as one static JPEG, PNG or WebP image with:

- At most 10,485,760 bytes, enforced while streaming, independent of Content-Length.
- At most 8192 pixels on each axis and 20,000,000 decoded pixels.
- Matching expected byte count, actual format/MIME and SHA-256 calculated over original received bytes.
- Successful Pillow container verification and full decode; no animation, malformed/truncated image or known GPS/geolocation metadata.

Benign EXIF is allowed. Pillow decompression protection remains enabled. There is no re-encoding, stripping or transformation.
ETag, provider metadata and the client digest are not cryptographic verification; the client digest is an expectation only.
The verification decision is bounded by a 15-second timeout. A decode runs off the event loop against bounded data;
Python cannot forcibly stop an already-running decoding thread, but a timed-out/cancelled result cannot write or commit.

Terminal validation rejection commits `status=rejected`, null dimensions and a controlled rejection code with HTTP 200.
Missing object is 409 and leaves pending. Temporary provider failure is 503/`Retry-After: 30` with rollback.
Malformed provider protocol is a sanitized 502 with rollback. Parser text is never exposed.

## Immutable content attachment snapshots

Content POST/PATCH accept ordered `assetIds` (zero through ten distinct UUIDs).
POST omitted means empty; PATCH omitted inherits the previous order; PATCH `[]` creates a no-asset version.
Null, duplicates and over-limit lists are 422. Reordering alone creates a new version; an otherwise unchanged list is a 422 no-op.
Foreign/missing assets are 404; same-tenant non-ready assets are 409.
All new versions get their own ordered ContentAsset snapshot in the same transaction as the item/version/audit/idempotency effects.
Approved edits still return to draft and clear approval. Historical version/attachment rows are never updated or deleted.

Fresh reads include `assetIds`. Historical Prompt 7 idempotency response bodies are replayed unchanged, including responses without `assetIds`.
The create fingerprint preserves historical behavior for an omitted/empty attachment list.
Content audit adds only a structural `assetIds` changed-field marker and `assetCount`.

## Migration and least privilege

Added migration: `20260922_0006_asset_business_access.py`.
Revision: `20260922_0006`; parent: `20260910_0005`.
It checks the exact 0005 version, safe runtime role, no memberships/ownership/broad grants,
20 ENABLE/FORCE tables and the exact 18 historical policies before changing anything.
Drift is rejected rather than repaired.

`content_versions.attachment_creation_xid xid8` is added nullable without a default first;
then its default becomes `pg_current_xact_id()`. Historical rows stay NULL; there is no backfill.
The ORM maps it as a deferred internal column. Application inserts omit it, public schemas omit it,
and runtime INSERT/UPDATE privileges do not include it.

Six new policies bring the expected total to 24:

- `assets_business_select`
- `assets_business_insert`
- `assets_business_update`
- `content_assets_business_select`
- `content_assets_business_insert`
- `audit_asset_insert`

Asset SELECT is tenant scoped. INSERT requires current actor, pending image, canonical destination,
bounded size, allowed MIME/lowercase digest and empty verification fields.
UPDATE is limited to pending -> ready/rejected, with valid dimensions for ready and null dimensions for rejected.
Only status, width, height and updated_at are writable.
SELECT FOR UPDATE may hide terminal rows under that policy; the repository safely distinguishes terminal state from absence
without treating an unlocked new pending row as locked.

ContentAsset INSERT requires valid tenant/content/version/ready-asset relationships, current version/current draft,
version creator equal to the current user, position 0..9, current full transaction ID and exact transaction timestamp.
Both marker predicates are required. Historical NULL rows and committed versions are closed.
ContentAssets receive SELECT and column-limited INSERT only. Assets receive SELECT, column-limited INSERT and the narrow UPDATE above.
No runtime DELETE, broad ContentVersion UPDATE, privileged helper, role mutation or database service-role bypass is introduced.
Unsafe security downgrade is blocked.

Assets ORM now uses the existing PostgreSQL asset_kind enum, CHAR(64), a timezone-aware deleted_at and the database pending default.
There is no rewrite of existing physical asset columns.
Frozen SQL, migrations 0001–0005, auth_stub and historical harness revisions remain unchanged.

## Local validation evidence

Executed focused command:

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/test_assets_api.py backend/tests/test_assets_service.py backend/tests/test_assets_storage.py backend/tests/test_assets_security_migration.py backend/tests/test_content_assets.py -q --tb=short
```

Result: **196 passed**.

Executed full suite without PostgreSQL opt-in:

```text
653 passed, 233 skipped, 1 warning in 12.55s
```

The skips are the opt-in PostgreSQL suites, including 63 new Prompt 8 cases.
The warning is the existing Starlette TestClient/httpx deprecation; dependency versions were not changed to suppress it.
Final closure checks: `python -m compileall backend scripts` exited 0;
`python -m pip check` reported `No broken requirements found.`;
`git diff --check` exited 0. The changed-file secret scan covered 36 files and found zero high-confidence credential patterns;
the reviewed credential-like literals are explicitly synthetic test fixtures or environment variable names/placeholders.
The frozen-file comparison against HEAD exited 0. Staging is empty and HEAD remains the original base.
The earlier 167-pass regression checkpoint is intermediate evidence, not the final total.

## Real local PostgreSQL validation

`scripts/local-dev/validate_prompt8.py` validates only `nightclub_ai_prompt8_test` on loopback:5432,
using retained `alembic_test_user` and `nightclub_api`. It refuses an existing database or unsafe credentials/roles,
never provisions roles, masks provider/runtime dotenv values in child processes, and never prints DSNs/passwords.
The owner executed the harness successfully with `PROMPT8_EXIT=0`. The complete migration route
0001 -> 0002 -> 0003 -> 0004 -> 0005 -> 0006 passed. Final Alembic head was `20260922_0006`.

The real PostgreSQL suite completed with **716 passed, 170 skipped, 2 warnings in 22.73s**, exit status 0.
The 170 skips are earlier opt-in PostgreSQL harness suites not enabled by `--prompt8-postgres`, not Prompt 8 failures.
The warnings were the existing Starlette TestClient/httpx deprecation and a Windows access-denied warning while writing
`.pytest_cache`; neither blocked execution.

The catalog contained exactly **24 policies** and preserved the 18 historical Prompt 5/6/7 policies unchanged.
Real RLS, grants, context, concurrency and attachment immutability cases passed. Runtime connectivity passed.
The expected 0006 -> 0005 downgrade attempt was blocked with
`Unsafe security downgrade blocked; a reviewed compensating migration is required`, proving the downgrade guard.
The subsequent catalog check completed with **1 passed, 62 deselected**, exit status 0.

The restore-guard tests temporarily changed a column default in this disposable DB using its owner,
then restore it in `finally`. This supplies bad-XID and old-timestamp fixtures through runtime inserts
without giving runtime control of either field and without weakening any RLS policy, grant, FORCE flag or role.

Cleanup completed with `TEST_DATABASE_REMAINING: []`: `nightclub_ai_prompt8_test` was removed.
The retained roles `nightclub_api` and `alembic_test_user` remained unchanged.
Historical harnesses remain pinned to their own migration revisions and should be reproduced with their matching validated source baseline;
the current Prompt 8 application expects migration 0006 for attachment access.

## Exclusions and remaining limits

No asset list/detail/delete or standalone attach/detach endpoint, video/document API, transformation,
retention worker, malware certification, publishing, AI, scheduling, WhatsApp, frontend, deployment or later prompt work.
No live provider integration claim. The bucket setup and environment-specific Storage contract remain deployment checks for a future authorized stage.
An upload capability is a bearer capability for up to two hours; pending/rejected object retention is deferred.
This local validation does not imply production security certification or live validation of Supabase Storage,
deployment, Meta, n8n or Railway.
