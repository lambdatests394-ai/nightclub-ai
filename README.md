# NIGHT CLUB AI v1.0

Prompt 3 provides the validated database/domain baseline. Prompt 4 Stage B adds the approved identity read endpoints, ES256 authentication, organization context and centralized RBAC; it awaits human review and has not been committed. Meta, AI and n8n integrations are not implemented.

See [Prompt 4 security contract](docs/PROMPT_4_SECURITY.md) and the [local PostgreSQL validation runbook](docs/runbooks/local-postgres-validation.md). Authentication settings remain blank placeholders until the owner supplies deployment configuration. Missing trusted authentication configuration never bypasses verification.

Run locally after installing dependencies:

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --reload
```

Run offline/unit and HTTP tests (local PostgreSQL integration is opt-in through the runbook):

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests -q
```
