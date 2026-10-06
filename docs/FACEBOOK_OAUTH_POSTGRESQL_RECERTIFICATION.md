# OAuth PostgreSQL checkpoint — implementation and available evidence

Date: 2026-10-05.

Status: LOCAL OAUTH POSTGRESQL CHECKPOINT PASS / DELIVERY HUMAN REVIEW PENDING.
The local Chromium/PostgreSQL evidence has been approved by human review.
Production certification has NOT been performed; the complete backend suite is
NOT approved. OAuth remains disabled by default (`FACEBOOK_OAUTH_ADMIN_ENABLED=false`).
This documentation synchronization does not rerun tests or infrastructure.

## Scope and source preservation

Branch `review/facebook-oauth-local`; HEAD unchanged:
`1a1684af14c42b81b23feb67d0d5f413a5c33ad1`. No staging or commit.
Approved external plan revision-2 SHA256:
`9b8b6ebb7202caaa6f628aee87b29e2d222a1fdb1dc798f897a95fd498c992c2`.

The initial preparation created seven authorized files: two test/support modules,
two local validators, this report, the runbook and a separate 16-source manifest.
The later authorized observability checkpoint added one offline test module.
Conftest modification
only registers the explicit `--facebook-oauth-postgres` option; old opt-ins remain
unchanged. The 15 non-runbook entries in the 16-source manifest retain their exact
hashes. The local-preparation runbook entry is updated only for this authorized
documentation synchronization, with raw and CRLF-to-LF SHA256 values.
The separate source gate also compared 46 preserved automation/security/migration/
SQL/n8n/dependency files to the historical checkpoint. Original transfer manifest,
external plans and historical B7 sources/reports remain unchanged.

## Observed execution results

| Check | Real result | Exit |
| --- | --- | --- |
| Offline observability and safety/source/fault-injector tests | 19 passed, 16 expected opt-in skips | 0 |
| Historical B7 CLI on detached checkpoint | B7_OFFLINE_READINESS: PASS; production NOT_VERIFIED | 0 |
| Historical B7 tests on detached checkpoint | 31 passed | 0 |
| Last complete backend run, NO PostgreSQL opt-in | 1261 passed, 379 skipped, 4 failed, 1 warning | 1 |
| Current automation regression | 253 passed | 0 |
| Separate OAuth gate | PASS: 16 OAuth sources, 46 protected inputs and PostgreSQL evidence | 0 |
| Real isolated OAuth PostgreSQL suite | 25 passed, 1 warning | 0 |
| Isolated PostgreSQL harness | PASS; captured-ID cleanup completed | 0 |
| compileall for new Python files and conftest | PASS | 0 |
| pip check | No broken requirements found | 0 |
| git diff --check | PASS, no output | 0 |
| High-confidence secret-pattern scan, 16 sources + checkpoint files | 0 findings; only synthetic inputs/placeholders | 0 |

The four full-suite failures remain in
`backend/tests/test_prompt11_b7_deployment_readiness.py`:
`test_current_repository_contract`, `test_direct_cli_is_offline_and_reports_scope`,
`test_known_crlf_materialization_is_not_content_drift`,
`test_hash_guarded_migration_still_requires_exact_bytes`.
All stop on current-source `CERTIFIED_SOURCE_DRIFT`. Historical execution passes;
the OAuth config/main changes explain the rejection. Checks downstream of that
guard are not claimed to pass on current source. No B7 edits/skips/xfails were made.
Historical B7 is accredited separately at executable checkpoint
`1a1684af14c42b81b23feb67d0d5f413a5c33ad1`, retaining comparator
`301fe0f0c8bb2e0bff1c7f8a042e1aa2bb794529`. The local composite-gate proposal
does not turn general pytest into PASS or authorize/certify production.

Two preparation issues were recorded without concealing their results:
the first historical pytest attempt had 22 passed/9 Temp-permission setup errors,
resolved using a fresh explicit basetemp; the first new offline runner attempt
had 2 failed/1247 passed/379 skipped/16 setup errors and automation 248 passed/
5 setup errors because its basetemp parent was absent. Only the NEW runner was
corrected to create that parent. The second complete offline attempt produced
the results above; both external raw outputs are preserved. A sandbox preflight
PermissionError was retried with approval and then exposed the disk guard.

Current candidate scope is 25 files: the 16-source OAuth delivery plus
seven checkpoint files, conftest and the observability test. `git diff --name-status` identifies six
tracked content changes; `git ls-files --others --exclude-standard` identifies
19 new candidate files. Known Windows stat-cache/EOL entries are not discarded,
normalized or staged.

## Successful real PostgreSQL checkpoint — current evidence

The single authorized instrumented retry completed with harness exit 0 and
`25 passed, 1 warning` (9 structural and 16 real PostgreSQL cases). The warning
was a pytest-cache write-permission warning, not a PostgreSQL validation failure.
Observed server version: `16.15`. Actual Alembic head: `20260928_0010`.

Initial and final catalogs are identical, with SHA256 for both:

`53ec088541befb01b180e2057e9627ff4c757090bdcd4160a51bfaa56d23be23`.

The catalog checks include full policy expressions/recipient roles/commands,
grants, ownership, role posture, memberships and RLS/FORCE, not only counts.
They confirm 58 policies, 22 application tables with RLS/FORCE and no unexpected
memberships. Runtime `nightclub_api` identity and least-privilege posture were
checked before/after cases; the administrative READ ONLY inspector was separate.

Authorization, tenant/actor isolation, owner/manager versus other roles,
membership revocation, replay and concurrent distinct sessions `{200,409}`,
single committed consumption/provider exchange, encrypted creation/reconnection,
structural audit, consumption before provider contact, provider failure and
transactional persistence rollback all passed. The exact approved temporary
trigger rejected connection persistence without undoing prior state consumption.
The trigger and its function were verified absent at completion.

Container removal targeted only captured ID
`e9e4389f0011a6e65db9a63284a197604d73d49bdc8a2b17da9da2ebb202fa7d`,
after rechecking identity and purpose/run-id labels. Absence was confirmed by ID,
name, purpose and run-id. Native retained roles were not contacted or altered.

## Superseded preparation and failed-attempt history

The following describes previous attempts only, NOT the current checkpoint.

At the initial preparation, Docker daemon was reachable (29.8.1); approved immutable manifest metadata matched
linux/amd64. The image reference is:
`docker.io/library/postgres@sha256:a85daf0dbd5e79586e850e3fe4b21b796799828ad015ce2166aeb98cc24da61c`.
At that stage, registry metadata identified 16.15 Trixie; no server version had
yet been observed. The successful later run observed 16.15 directly.
The initial space check observed 162557952 free bytes, below the retained 3 GiB
guard. No image pull, container create/start, database, role, Alembic execution or
PostgreSQL opt-in occurred. Container resources were absent before execution.

At that initial checkpoint the 16 real cases were written but unexecuted and
the source/PostgreSQL gate returned exit 2. The disk preflight returned exit 1.
Those pending/blocked states are superseded by the successful evidence above,
including actual concurrency, persistence, audit, catalog and trigger cleanup.

During final closure a later check observed 0 free bytes. Only this checkpoint's
clean detached historical worktree and four explicitly identified pytest temporary
directories were removed; previous browser environments, personal files and all
external evidence were retained. A subsequent check observed 6397952 free bytes
(about 6 MiB), still below the unchanged guard. These temporary artifacts are
regenerable. No unapproved storage cleanup or image download was attempted.

After capacity became available, a subsequent harness attempt returned exit 1
with a generic `RuntimeError` and removed its own container. Its technical cause
was not preserved and remains UNKNOWN. Observability was then instrumented and
tested offline (`19 passed, 16 skipped`); the single controlled retry passed.
That later success does not identify the cause of the earlier failed attempt.

## Security and evidence

The new harness separates runtime nightclub_api from administrative control and
read-only inspector. The inspector never enters FastAPI; fresh read-only
transactions observe committed rows through an explicit SELECT allow-list.
No production privileges/policies or FORCE RLS behavior are modified. The
PostgreSQL best-practices review reinforced this existing approved boundary;
it introduced no alternative role or architecture.

The exact reviewed transaction-fault trigger is confined to the new harness and
one synthetic case. Cleanup verifies captured full container ID and both labels,
then removes only that ID. Catalog evidence compares full expressions/ACL/owners/
roles/RLS, with policy count supplemental. Credentials and synthetic private keys
exist only in memory; no environment files or diagnostic credential dumps are
copied into external evidence.

Raw offline outputs are stored outside Temp/repository under
`C:\Users\Rogelio\NightclubAI-Evidence\oauth-postgresql-checkpoint-20261005`.
The successful evidence is in `observability-retry-1`, including sanitized output,
result, exact catalogs, historical B7 outputs and the separate OAuth gate result.
The delivery candidate export records the Git-canonical 25-file diff and raw/LF
SHA256 hashes. This documentation-only revision produces a new revision-2 package,
without overwriting the original package or evidence. Original
20261004 plans/manifests are preserved separately.

## Remaining review and operational boundaries

Local PostgreSQL evidence is PASS; human review of the delivery package remains
pending. Production certification and activation have not been performed.
Preserve historical B7 and keep its four current-tree rejections visible.
The proposed local composite gate uses separate historical/current evidence;
it does not approve the indiscriminate pytest suite or grant production authority.

Non-blocking technical debt remains recorded and NOT corrected:
if the primary operation and `cleanup_owned()` fail simultaneously, preserve
the primary cause and also record a structured cleanup failure. No harness,
application, migration, SQL or test change is authorized by this documentation pass.

No real connections to Meta, Supabase, Railway, n8n or providers, real accounts,
publications or native PostgreSQL operations occurred. No
staging, commits, push, PR, merge, tag or deployment were performed.

Final status: LOCAL OAUTH POSTGRESQL CHECKPOINT PASS — DELIVERY HUMAN REVIEW PENDING
— PRODUCTION NOT CERTIFIED OR AUTHORIZED — OAUTH DISABLED BY DEFAULT.
