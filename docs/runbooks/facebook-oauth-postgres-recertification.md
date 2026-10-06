# Local OAuth PostgreSQL recertification checkpoint

Date: 2026-10-05. Scope: review only; no production certification or deployment.

## Source and historical evidence

Keep branch `review/facebook-oauth-local`, HEAD
`1a1684af14c42b81b23feb67d0d5f413a5c33ad1`, existing changes and empty staging.
The approved external revision-2 plan has SHA256
`9b8b6ebb7202caaa6f628aee87b29e2d222a1fdb1dc798f897a95fd498c992c2`.
The 16 reviewed OAuth files are pinned by
`docs/facebook-oauth-reviewed-source-manifest.json`, separately from the
original transfer manifest. Neither manifest replaces historical B7.

Run historical B7 from a fresh detached worktree at the HEAD above. Its
unchanged comparator remains `301fe0f0c8bb2e0bff1c7f8a042e1aa2bb794529`.
Use the repository virtualenv with the historical worktree as working directory;
run its B7 CLI and `backend/tests/test_prompt11_b7_deployment_readiness.py`.
Use a fresh explicit pytest `--basetemp` whose parent exists if Windows Temp
permissions block pytest. Remove only the detached worktree created for that run,
without force. Do not edit B7, baseline, historical reports, skips or xfails.

## Isolated resource contract

Use only Docker, never native PostgreSQL. The harness rejects repository `.env`
presence without opening that file and filters inherited environment variables.
Passwords and signing/encryption keys are synthetic, memory-only; do not supply
personal credentials or write DSNs to files.

- Official image: `docker.io/library/postgres@sha256:a85daf0dbd5e79586e850e3fe4b21b796799828ad015ce2166aeb98cc24da61c`.
- Platform: `linux/amd64`; metadata describes PostgreSQL 16.15 Trixie.
  Actual server version must be observed at execution, not inferred from the tag.
- Reserved container: `nightclub-ai-oauth-pg16-cert`.
- Purpose label: `com.nightclub-ai.purpose=oauth-recertification`.
- Unique run label: `com.nightclub-ai.run-id=<fresh UUID>`.
- Database: `nightclub_ai_oauth_recert_test`.
- Binding: `127.0.0.1:55432:5432` only.
- Data: tmpfs `/var/lib/postgresql/data`; no persistent volume/personal mount.

Preflight requires Docker daemon, at least 3 GiB free on the user-profile drive,
free reserved name/purpose/port, and exact approved manifest/platform. A block
must not trigger automatic cache deletion, an alternate database, or lower guards.
The fixed image may be downloaded only after preflight passes. Its immutable
image cache may remain; do not run Docker prune.

`docker create` returns a full ID. Inspect ID, name, both labels, digest-derived
image ID, platform, binding and mounts before start. Cleanup rechecks captured ID,
name and both labels, removes exclusively that ID, and verifies ID/name/run-label
absence. Abort on preexisting resources; never reuse or remove them.

## Execution, once preflight is viable

From `C:\Users\Rogelio\nightclub-ai`, with a NEW evidence directory outside
the repository and Temp under USERPROFILE:

```powershell
.\.venv\Scripts\python.exe scripts/local-dev/validate_facebook_oauth_postgres.py --evidence-dir "$env:USERPROFILE\NightclubAI-Evidence\oauth-postgresql-checkpoint-20261005\postgres-run-next"
$LASTEXITCODE
```

No manual credential environment variables are needed. The harness provisions
three synthetic roles ONLY inside its new container, applies the local auth
fixture, and migrates sequentially through `20260928_0010`. Runtime and scheduler
are LOGIN/NOINHERIT/NOSUPERUSER/NOCREATEDB/NOCREATEROLE/NOREPLICATION/NOBYPASSRLS;
the isolated migration role is LOGIN/INHERIT/CREATEDB with other privileges false.
Existing native roles are never connected to or changed.

Only this invocation supplies `--facebook-oauth-postgres`. Historical opt-ins
remain disabled. FastAPI uses real JWT/JWKS verification, coordinator, repositories,
cipher and root transactions, with only external HTTP transports intercepted.

## Inspector, fault injection and catalog

FastAPI receives only `nightclub_api`. A separate container-administrator inspector
opens a fresh `REPEATABLE READ READ ONLY` transaction per allow-listed observation
and rolls it back. It can see committed state/audit across FORCE RLS because it is
the isolated administrator, but is never passed to application settings/repositories.
Provisioning, synthetic fixtures and the exact reviewed trigger use a separate
control connection; read-only observation never performs writes or DDL.

Validate migrations against their original verifier functions before accepting
the initial snapshot. Compare complete before/after policy rows, recipient roles,
commands, USING/WITH CHECK, permissiveness, grants/grantors/grantability, ownership,
table RLS/FORCE and sequence dependencies. Also require 58 policies, 22 application
tables, migration head 0010, strict runtime/scheduler posture and no memberships.
Capture sorted sanitized catalogs plus SHA256 in external evidence; count alone
cannot pass. Per-case runtime identity and unchanged catalog assertions are required.

The approved fault injector resides only in the new local harness. It matches the
fixed synthetic organization/actor/page and connection-created/updated audit,
not state-consumption audit. Remove it in `finally`; confirm both function and
trigger absence. No FORCE RLS toggle or policy/grant change is allowed.

## Acceptance cases

- Owner/manager 200; editor/reviewer/operator/viewer 403 without exchange.
- Wrong authorized organization/actor 400; foreign membership 403; no consumption.
- Membership revoked before consumption or before persistence: 403, no connection.
- Sequential replay/concurrent distinct sessions: statuses {200,409}, exactly one
  consumed event, connection, exchange and subsequent page query.
- Creation/reconnection: same connection ID, encrypted credential, structural audit.
- Observe consumption COMMITTED before the provider exchange in another transaction.
- Provider failure: 502, consumed state retained, no connection; replay 409.
- Persistence trigger failure: 503 IDENTITY_UNAVAILABLE, consumed state retained,
  connection and connection audit rolled back, no successful response.
- Before/after runtime identity nightclub_api, RLS/FORCE unchanged and no residues.

## Separate current-source gate

```powershell
.\.venv\Scripts\python.exe scripts/local-dev/validate_facebook_oauth_recertification.py
.\.venv\Scripts\python.exe scripts/local-dev/validate_facebook_oauth_recertification.py --run-local-checks "$env:USERPROFILE\NightclubAI-Evidence\oauth-postgresql-checkpoint-20261005\offline-run-next"
.\.venv\Scripts\python.exe scripts/local-dev/validate_facebook_oauth_recertification.py --postgres-evidence "$env:USERPROFILE\NightclubAI-Evidence\oauth-postgresql-checkpoint-20261005\postgres-run-next\postgresql-result.json"
```

Exit 2 means PostgreSQL evidence pending; exit 1 means failure/block. A historical
B7 rejection on current OAuth source remains a real full-suite failure. Separate
gates do not suppress it or certify the entire suite. Even complete local evidence
requires human review before any delivery/certification. Do not stage, commit,
push, deploy, publish or connect to Meta/Supabase/Railway/n8n in this checkpoint.
