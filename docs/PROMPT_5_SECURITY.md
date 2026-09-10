# Prompt 5 security boundary

Date: 2026-09-09

## Delivery state

Stage B implementation and real local PostgreSQL RLS validation have passed human
review. The owner confirmed migration 20260907_0002 -> 20260909_0003, 301 passed /
12 expected historical Prompt 4 PostgreSQL skips, three bootstrap policies,
20/20 canonical tables with ENABLE and FORCE RLS, blocked unsafe downgrade,
context/pool/cancellation isolation and disposable database cleanup. The subsequent
message-only correction passed the non-PostgreSQL suite: 260 passed / 55 expected
opt-in skips. Unit/application execution does not replace that real validation.
Commit authorization remains separate. The local nightclub_api role is retained
unless a future explicit lifecycle decision changes that; it is not cleanup work.

## Request and transaction lifecycle

Verified JWT -> CurrentUser -> explicit AsyncSession root transaction -> effective
runtime role check -> transaction-local user GUC -> identity bootstrap -> validated
OrganizationContext when applicable -> transaction-local organization GUC -> response
-> commit/rollback and close. Personal endpoints never need an organization selector.

The context component accepts identity objects, not raw HTTP input. Values are SQL
parameters. It refuses missing/inactive/nested roots, malformed object UUIDs, stale
transaction identities and a different context user. Preexisting nonempty context
causes connection invalidation, not reuse as a fallback. There are no intermediate
commits in the protected request. Starting another transaction does not restore
authorization from session.info. Profile/organization HTTP contracts are unchanged.

The transaction boundary handles BaseException through finally. Asyncio cancellation
invalidates the session; rollback errors also invalidate it. Cleanup runs in a
shielded task and completes before cancellation propagates. Same-connection reuse
after commit/rollback and replacement after cancellation are distinct test cases.
Long-lived session settings are forbidden; transaction pooler compatibility is not
claimed. SQLAlchemy pool_pre_ping is only a connectivity check.

## Migration 20260909_0003

Requires online catalog checks and an existing safe `nightclub_api`; creates no
roles/passwords. Rejects unsafe role attributes, memberships, table ownership,
missing canonical tables, effective schema CREATE and any preexisting policies.
Conflicting policies are not silently deleted. Reversal requires a reviewed
compensating security migration; automatic downgrade raises before changing DDL.

Only these SELECT policies exist:

| Table | Policy |
| --- | --- |
| profiles | Current user's active profile |
| organization_members | Current user's memberships with an active visible profile; no organizations subquery |
| organizations | Active organization with a visible own membership |

The dependency graph is acyclic. There are no helper functions or SECURITY DEFINER.
No INSERT/UPDATE/DELETE grants exist on these tables. All 20 tables have ENABLE and
FORCE RLS; the other 17 have no runtime CRUD grants or policies. Nullable tenant
columns on system tables do not create exceptions. Workers are deferred.

Revocations are scoped to these 20 tables and the audit sequence. Optional existing
anon/authenticated/service_role principals are handled without inventing local
Supabase roles. Schema USAGE is granted to runtime and its direct CREATE revoked;
no global public-schema privilege is changed. Effective table/column privileges
are checked after grants. If other grant paths prevent the required result, fail.
PostgreSQL's [REVOKE documentation](https://www.postgresql.org/docs/16/sql-revoke.html)
describes additive privileges and revocation behavior; policies follow
[CREATE POLICY](https://www.postgresql.org/docs/16/sql-createpolicy.html).

`DATABASE_URL` is runtime-only. `DATABASE_RUNTIME_EXPECTED_ROLE` defaults to
`nightclub_api`. Each protected dependency verifies effective/session identity,
role attributes, memberships, public CREATE and nonownership of all 20 tables.
Dangerous identity is a sanitized IDENTITY_UNAVAILABLE 503 with Retry-After: 30.
Alembic requires DATABASE_MIGRATION_URL; no runtime fallback remains.

## Limits and deferred scope

- The three bootstrap policies intentionally ignore organization selection. They
  allow a user's multiple active organizations; malformed/wrong organization GUCs
  cannot expand that set. Missing/malformed user context denies rows or errors.
- PostgreSQL trusts the verified context supplied by FastAPI. GUCs do not defend
  against a fully compromised backend or arbitrary SQL with its credential.
- Existing schema/FKs and domain interfaces are unchanged. Business-table RLS,
  worker access, grant expansion and deployment to Supabase require later approval.
- Local fixtures are seeded before FORCE RLS. The removed-membership fixture is
  deleted before migration; live concurrent revocation is not claimed as tested.
- The Prompt 4 harness is pinned to 0002, where its owner-based persistence tests
  belong. Prompt 5 uses actual root transactions and a separate nonowner login.
- `sql/001`, `002`, `003` and migrations 0001/0002 remain unchanged. Despite the
  historical 0001 docstring, sql/003 is design-only and MUST NOT be executed.

See [local validation runbook](runbooks/local-postgres-validation.md).
