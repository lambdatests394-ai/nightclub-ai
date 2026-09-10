# NIGHT CLUB AI v1.0

Prompts 3, 4 and 5 are closed, validated and promoted. Prompt 4 provides identity read endpoints, ES256 authentication, organization context and centralized RBAC. Prompt 5 adds identity-only PostgreSQL RLS and runtime-role enforcement. The local nightclub_api role is retained unless a future explicit lifecycle decision changes that. Meta, AI and n8n integrations are not implemented.

Prompt 6 Stage B implements the campaign vertical slice; its independent real local
PostgreSQL validation passed the human checkpoint. The implementation commit is
published on `feature/prompt-6-campaign-api` and PR #4 targets `develop`. The PR
remains unmerged pending explicit human authorization. This is not a Supabase
deployment. See [Prompt 6 security](docs/PROMPT_6_SECURITY.md) and the
[implementation report](docs/PROMPT_6_IMPLEMENTATION_REPORT.md).

See [Prompt 4 security contract](docs/PROMPT_4_SECURITY.md) and the [local PostgreSQL validation runbook](docs/runbooks/local-postgres-validation.md). Authentication settings remain blank placeholders until the owner supplies deployment configuration. Missing trusted authentication configuration never bypasses verification.

See [Prompt 5 security](docs/PROMPT_5_SECURITY.md). Runtime uses `DATABASE_URL` and checks `DATABASE_RUNTIME_EXPECTED_ROLE` (default `nightclub_api`). Alembic requires a separate `DATABASE_MIGRATION_URL`; it never falls back to the runtime URL. Historical `sql/003_rls.sql` is design-only and MUST NOT be executed.

Run locally after installing dependencies:

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --reload
```

Run offline/unit and HTTP tests (local PostgreSQL integration is opt-in through the runbook):

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests -q
```
