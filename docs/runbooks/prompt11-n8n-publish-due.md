# Prompt 11 B5 — Publish Due workflow import and live-check runbook

## Status and boundary

B5 delivers a portable JSON artifact and offline contract checks only. It does
not provision n8n, deploy, activate a schedule or claim successful live import,
runtime compatibility, production URL verification or live HMAC certification.
The following live steps require a separate owner-approved checkpoint.

The artifact is `n8n/workflows/prompt11-publish-due.json`, named
**Night Club AI — Publish Due**, with `active=false`. B1–B4 remain unchanged;
Alembic head remains `20260928_0010`, with the previously certified 58 policies
(13 system-automation policies). No database certification is repeated for B5.

## Built-in node contract

| Node type | Artifact typeVersion | Purpose |
| --- | --- | --- |
| `n8n-nodes-base.scheduleTrigger` | 1.2 | One trigger per minute, UTC |
| `n8n-nodes-base.set` | 3.4 | Timestamp, canonical material, minimal success item |
| `n8n-nodes-base.crypto` | 2 | Credential-backed HMAC-SHA256, hexadecimal |
| `n8n-nodes-base.httpRequest` | 4.2 | Single empty POST, full response |
| `n8n-nodes-base.switch` | 3.2 | Explicit status handling and fallback |
| `n8n-nodes-base.stopAndError` | 1 | Fixed, non-sensitive error codes |

These are node versions, not a claim about a minimum n8n product release. Before
import, verify the chosen instance supports each version. In particular, do not
downgrade Crypto to legacy v1 or substitute a secret in node parameters. Crypto v2
uses the `crypto` credential's `hmacSecret` field, displayed as **Hmac Secret**.
See the official [Crypto credential documentation](https://docs.n8n.io/integrations/builtin/credentials/crypto/)
and [Crypto v2 source](https://github.com/n8n-io/n8n/blob/master/packages/nodes-base/nodes/Crypto/v2/CryptoV2.node.ts).

No Code, shell, SSH, database, Webhook, agent, community or custom node is used.
Neither `NODE_FUNCTION_ALLOW_BUILTIN` nor `NODE_FUNCTION_ALLOW_EXTERNAL` is needed.
n8n receives no DB credentials, Meta tokens, organization IDs, job IDs or content.
The only external request in this workflow is to FastAPI.

## Frozen request and signature

The timestamp Set node evaluates `Math.floor(Date.now() / 1000).toString()` once.
The following Set node constructs the canonical text from that stored value;
Crypto then signs it immediately, with no waiting or network node in between.
The same timestamp item field is used in `X-N8N-Timestamp`.

Canonical UTF-8 text, with exactly four LF separators and no trailing newline:

```text
v1
POST
/internal/automation/publish-due
<stored decimal timestamp>
e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
```

The digest is SHA-256 of zero bytes. Crypto action `hmac`, algorithm `SHA256`,
encoding `hex` signs the canonical text, not its JSON representation. The request
adds `v1=` to the lowercase hexadecimal result. The secret exists only in the
selected n8n credential, never in item data or workflow JSON.

Transport contract:

- `POST https://nightclub-api.example.invalid/internal/automation/publish-due` in Git.
- Replace only the origin in the imported instance with an owner-approved HTTPS origin.
- Exactly two custom headers: `X-N8N-Timestamp` and `X-N8N-Signature`.
- HTTP authentication is `none`; no credential is attached to HTTP Request.
- No Authorization/JWT, tenant, idempotency or Cookie header; no query parameters.
- Send Body disabled: zero raw bytes, not `{}`, `null`, an empty JSON string or LF.
- Follow Redirects explicitly disabled, including POST redirects; TLS verification enabled.
- HTTP timeout 90 seconds, workflow timeout 120 seconds; automatic retries disabled.
- Full response with status and body, non-2xx not automatically thrown. Autodetect
  permits text error/redirect responses to reach status routing; B4 success is JSON.

The [official HTTP Request implementation](https://github.com/n8n-io/n8n/blob/master/packages/nodes-base/nodes/HttpRequest/V3/HttpRequestV3.node.ts)
is the source for the nested redirect/response settings. Actual wire behavior,
including zero-byte POST and TLS/redirect handling, still requires live certification.
Ordinary transport-managed headers are not additional application-auth headers.

## Response handling

| HTTP status | Terminal behavior |
| --- | --- |
| 200 | One item with eight counters and `correlationId` only |
| 401 | `NIGHTCLUB_INTERNAL_AUTH_FAILED` |
| 422 | `NIGHTCLUB_AUTOMATION_CONTRACT_INVALID` |
| 503 | `NIGHTCLUB_AUTOMATION_UNAVAILABLE` |
| Any other status, including 3xx | `NIGHTCLUB_AUTOMATION_UNEXPECTED_RESPONSE` |

The success fields are `selected`, `processed`, `published`,
`reconciledPublished`, `retryScheduled`, `permanentFailure`, `notDue`,
`leaseUnavailable` and `correlationId`. FastAPI is authoritative; n8n copies rather
than recalculates these values. The Set projection excludes all other fields.
The four error codes are constants, not interpolated response bodies or headers.

DNS, TLS, connection and timeout failures stop at HTTP Request: they are not HTTP
status responses and do not get misrepresented as a 503. Do not retry the node or
manually replay failed executions as a recovery strategy. A later scheduled run
is the next orchestration opportunity. Durable eligibility, retry timing, leases
and reconciliation remain in FastAPI/PostgreSQL; n8n never decides to republish.
The one-minute cadence can overlap a 90-second call. Backend concurrency guards
remain authoritative; do not add n8n business locks or loops.

## Execution-data hygiene and compatibility gate

The artifact specifies `executionOrder=v1`, `timezone=UTC`, 120-second bounded
execution, progress/manual/success saving disabled, error saving `all`, and
`redactionPolicy=all`. The final success projection does not erase earlier
in-memory node outputs. Failed executions can contain transient signatures,
timestamps and canonical material; restrict operator access and retention.

Redaction is **not guaranteed simply by importing this JSON**. It depends on the
installed version, license and instance policy. The official
[workflow settings schema](https://github.com/n8n-io/n8n/blob/master/packages/cli/src/public-api/v1/handlers/workflows/spec/schemas/workflowSettings.yml)
defines `redactionPolicy`; the official [release notes](https://github.com/n8n-io/n8n-docs/blob/main/docs/changelog/release-notes.md)
describe license gating and possible stripping of this setting. Redaction is not
a promise of deletion: authorized reveal paths may still exist.

Before live execution, verify effective settings and instance-level logging.
Never weaken a stronger instance-wide policy. If effective redaction cannot be
confirmed, keep the workflow inactive and require an explicit owner decision;
prefer disabling error-data storage in the live instance rather than retaining
signing material unredacted. Review that stronger live configuration separately;
the offline checker intentionally validates only the versioned artifact.
Do not attach raw execution exports, request headers or credentials to tickets.

## Future owner-controlled import checklist

1. Provision or open the separately approved n8n instance. B5 does not do this.
2. Verify the node versions and execution-data safeguards above; import the JSON.
3. Confirm the workflow remains inactive/unpublished and no schedule is running.
4. Create/select a Crypto credential named **Night Club AI Internal HMAC**.
5. Set **Hmac Secret** directly in the credential UI to the backend's current
   `N8N_INTERNAL_SECRET`, using the approved secret-management channel. Do not
   paste it into expressions, workflow item fields, Git, screenshots or logs.
6. Select that credential on **Sign with Crypto credential**. No portable
   credential ID is embedded in the repository artifact.
7. Replace the `.example.invalid` origin in **Publish due** with the approved
   actual HTTPS origin. Keep the canonical path unchanged and add no query.
8. Verify Send Body/Send Query disabled, exactly the two HMAC headers, redirects
   disabled, TLS verification enabled, 90-second timeout and no automatic retry.
9. Verify UTC timezone, one-minute interval, 120-second workflow timeout and
   effective execution-data policy. Keep the schedule inactive during testing.
10. With owner approval, perform **one** controlled manual execution. This may
    publish real due jobs through FastAPI; agree on the target/data beforehand.
    Confirm the outbound POST body is empty without logging signing material.
11. Verify HTTP 200 and the nine-field success item with `correlationId`; correlate
    using backend audit/log evidence. Do not infer success merely from import.
12. Inspect any sanitized workflow export for accidental secret payloads before
    sharing. Never export credentials/encryption keys or commit the live config.
13. Only after separate human approval consider publishing/activating the workflow.

If a node version is missing, import changes settings, redaction is stripped,
authentication fails or a redirect occurs: stop and review. Do not add Code nodes,
credential fallbacks, insecure TLS or redirect forwarding to make it work.

## Current-secret rotation

1. Configure the backend with a new `N8N_INTERNAL_SECRET` and retain its old
   current value as `N8N_INTERNAL_SECRET_PREVIOUS` during the approved overlap.
2. Update the n8n Crypto credential to the new current value through the secure UI.
3. Verify an authorized trigger succeeds without disclosing either value.
4. Remove the backend previous value after the approved overlap window.

n8n signs with one current secret only. B1 verifies current/previous; no second
credential, fallback signer or signature retry belongs in this workflow.

## Offline certification

From the repository root:

```powershell
.\.venv\Scripts\python.exe scripts/local-dev/validate_prompt11_n8n_workflow.py
.\.venv\Scripts\python.exe -m pytest backend/tests/test_prompt11_n8n_workflow_contract.py backend/tests/test_automation_internal_auth.py backend/tests/test_publish_due_api.py backend/tests/test_publish_due_coordinator.py backend/tests/test_publish_due_discovery.py -q
```

The validator accepts only the portable placeholder artifact, exits 0 with
`OFFLINE_WORKFLOW_CONTRACT: PASS` or exits 1 with a sanitized structural failure.
It rejects extra node fields, credentials, destinations, expression changes,
unsafe transport settings, hidden graph branches/cycles and duplicated JSON keys.
It never evaluates arbitrary JavaScript and never contacts any service.

Tests decode the restricted canonical expression and compare its bytes against
the existing B1 `canonical_payload` function. The public synthetic fixture yields:

```text
v1=ccf29f320317b9d0a6ce79b201b3294a448128a859777517b882145ae8390de4
```

This establishes offline contract compatibility, not execution by n8n's expression
engine. Live import, credential binding, request bytes, response projection,
redaction, error paths and deployed FastAPI HMAC remain the next human checkpoint.
