# ADR-001: FastAPI PostgreSQL Role and Row-Level Security Model

**Date:** 2026-09-02  
**Status:** Accepted for Phase 1 architecture; implementation remains subject to the approved scaffolding scope.

## Context

The approved core architecture assigns FastAPI all database writes, authorization decisions, and domain state transitions. Supabase PostgreSQL is the persistent store, and the blueprint names Row-Level Security (RLS) as defense in depth.

This required an explicit decision because the Supabase service-role key bypasses RLS by design. Using that key as FastAPI's application-data access path would make RLS ineffective against backend authorization or tenant-isolation defects. Leaving the PostgreSQL connection role unspecified would make the security model ambiguous.

The system also has trusted background requests from n8n. Those requests are authenticated at FastAPI; n8n never connects to PostgreSQL.

## Decision taken

Adopt **Option A**: FastAPI will connect to Supabase PostgreSQL through a dedicated, minimum-privilege PostgreSQL login role named `nightclub_api` (final credential value supplied only through the Night Club AI deployment secret store).

The role must be created without superuser, role-administration, database-creation, replication, or `BYPASSRLS` privileges. It receives only the schema, table, sequence, and function privileges required by FastAPI. It is the only application-data database role used by the API.

RLS is enabled on every Night Club AI application table. Policies use transaction-local, server-set request context -- at minimum authenticated user ID, organization ID, actor type, and correlation ID -- plus membership data to enforce tenant and role constraints. FastAPI establishes that context at the beginning of every database transaction and never accepts it directly from a client as trusted SQL context. Background/system operations establish a separately validated system actor context and remain organization-scoped.

`SUPABASE_SERVICE_ROLE_KEY` is **not** an application-data PostgreSQL or PostgREST access credential. If it is required for Supabase Auth administration or server-side Storage operations such as issuing signed URLs, it remains server-only, is used through a narrowly scoped Supabase client, and is never used to query or mutate business tables.

Anonymous and ordinary `authenticated` Supabase roles have no direct grants to Night Club AI business tables. RLS therefore protects both the dedicated FastAPI data path and a future accidental direct Supabase client path.

## Consequences

- RLS is a real secondary control for backend tenant/role isolation instead of a cosmetic control that a service role bypasses.
- FastAPI remains the sole public business-data API and the only component that writes business state; n8n remains without database access.
- Phase 1 migrations must include explicit role grants, RLS enablement, deny-by-default policies, and safe request-context helper functions before business-table access is exposed.
- The connection pool must reset transaction-local context correctly. All application queries must run within an explicit transaction; connection reuse must never leak actor or organization context.
- Database migrations themselves require a separate controlled migration/admin credential. That credential is not available to the running API and is not an application runtime dependency.
- The design adds initial SQL/policy test effort. Required tests include cross-organization reads/writes, role-escalation attempts, background/system context, and pooled-connection context leakage.
- A FastAPI bug can still make incorrect business decisions, so application-layer RBAC remains mandatory. RLS constrains the blast radius and provides independent tenant/role checks; it does not replace application authorization.
- All role names, connection strings, Supabase project references, and secret stores remain exclusive to Night Club AI and must never reference La Boutique.
