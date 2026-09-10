# ADR-006: Transaction-local identity RLS and minimum runtime privileges

Date: 2026-09-09

Status: Architectural decision, implementation and real local PostgreSQL validation
approved by human review for Prompt 5 Stage B. Commit authorization remains separate.

## Context

Prompt 4 validates JWTs in FastAPI and scopes repository queries. Historical
`sql/003_rls.sql` permits every row to the runtime role; it is not tenant RLS
and MUST NOT be executed. Direct asyncpg connections do not inherit JWT claims.

## Decision

- Set `app.user_id` only from verified `CurrentUser` and `app.organization_id`
  only from validated `OrganizationContext`, with parameterized
  `set_config(..., true)` inside an explicit root transaction.
- Keep RBAC in FastAPI. No `request.jwt.claims`, SET ROLE, SECURITY DEFINER,
  worker bypass or changes to identity value objects.
- Use acyclic SELECT policies: active own profile -> own memberships with an
  active profile -> active organizations with visible membership. Organization
  context is not required for these bootstrap reads, including organization listing.
- Grant runtime SELECT only on those three identity tables. Enable and FORCE
  RLS on all 20 canonical tables. The other 17 have no runtime CRUD or policy.
- Require externally provisioned `nightclub_api` LOGIN/NOINHERIT with no admin,
  creation, replication, bypass, membership or application-table ownership powers.
- Check the effective runtime identity before identity reads. Reject effective
  schema CREATE and unexpected role memberships rather than silently continuing.
- Require explicit migration credentials. This supersedes the fallback described
  in ADR-004; runtime `DATABASE_URL` is no longer a migration source.
- New Alembic 20260909_0003 applies the reviewed security boundary, not the frozen
  historical SQL. Its automatic downgrade is intentionally blocked.

## Consequences

Self-membership rows can include inactive organizations, but organizations and
their data remain filtered. Invalid organization GUCs do not broaden bootstrap
visibility: these three policies intentionally do not use that selector. The
application setter rejects malformed identity objects. Invalid user GUCs fail
UUID conversion; absent user GUCs expose no identity rows.

GUCs trust FastAPI: they are not protection against compromised database
credentials or arbitrary SQL. User/organization selection and business RBAC
remain application responsibilities. No future tenant CRUD is implicitly enabled.

Cancellation triggers invalidation and shielded close; normal completion/errors
finish the root transaction. Reuse and cancellation passed the opt-in real
PostgreSQL tests, not only mocks, as confirmed by human review. No transaction-mode
pooler is certified.

Local testing uses the already externally provisioned role `nightclub_api`
(a local validation identity, never production credentials), because the migration
targets that fixed role. It is retained locally unless a future explicit lifecycle
decision changes that; do not delete it as validation cleanup or repeat provisioning.
Never extend `alembic_test_user`; keep its intentional CREATEDB.
