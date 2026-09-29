# Night Club AI automation artifacts

[Publish Due](workflows/prompt11-publish-due.json) is the versioned, **inactive**
Prompt 11 B5 workflow. It triggers once per minute in UTC and sends one HMAC-signed,
empty-body POST to the certified FastAPI publish-due endpoint. FastAPI owns all
eligibility, tenant isolation, state transitions, leases, reconciliation and retries.

Before any live execution, a human must select a Crypto v2 credential named
**Night Club AI Internal HMAC**, with its **Hmac Secret** configured securely to
match the backend's current `N8N_INTERNAL_SECRET`. No credential payload or
instance-specific credential reference is included in the artifact.

The HTTP origin `https://nightclub-api.example.invalid` must be replaced in the
imported instance with the approved actual HTTPS API origin. The versioned JSON
keeps the placeholder. The workflow has no database access, Meta credentials,
tenant/job identifiers or business logic. It needs no Code node, custom node or
Node.js module allowlist.

See the [import and certification runbook](../docs/runbooks/prompt11-n8n-publish-due.md)
for node versions, transport restrictions, credential rotation and execution-data
hygiene. Import/runtime compatibility, live HMAC and activation require a separate
human checkpoint. **Do not publish or activate this workflow as part of B5.**

Offline validation from the repository root:

```powershell
.\.venv\Scripts\python.exe scripts/local-dev/validate_prompt11_n8n_workflow.py
.\.venv\Scripts\python.exe -m pytest backend/tests/test_prompt11_n8n_workflow_contract.py -q
```

These commands parse the artifact and test its frozen contract. They do not call
n8n, FastAPI, Meta or PostgreSQL and do not certify an installed n8n runtime.
