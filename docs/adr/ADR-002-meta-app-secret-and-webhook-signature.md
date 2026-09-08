# ADR-002: Meta App Secret and Webhook Signature Source of Truth

**Date:** 2026-09-02  
**Status:** Accepted for Phase 1 architecture; implementation remains subject to the approved scaffolding scope.

## Context

The blueprint listed `META_APP_SECRET` and `META_WEBHOOK_APP_SECRET` as separate environment variables. That is ambiguous: Meta's app secret is normally used for application authentication and to compute/verify the `X-Hub-Signature-256` HMAC of the raw webhook body. Two variables could drift or be populated with unrelated values, causing webhook verification failures that are difficult to diagnose.

The Phase 1 Meta app registration, WABA, Page IDs, review, and production credentials do not yet exist. The project owner will create and inject those Night Club AI-specific values later. No real credential is needed to record this decision.

## Decision taken

Use **one** canonical server-only environment variable: `META_APP_SECRET`.

The same Meta app-secret value is used for both:

1. server-side Meta OAuth/application authentication; and
2. HMAC SHA-256 verification of the raw request body for Meta webhooks.

`META_WEBHOOK_APP_SECRET` is not a supported runtime variable and must not be read. It is removed from the target environment-variable specification to eliminate split configuration.

`META_WEBHOOK_VERIFY_TOKEN` remains a distinct, randomly generated Night Club AI secret. It is used only for Meta's initial webhook challenge verification and is not used to compute the webhook HMAC. `META_PAGE_ID`, `META_WABA_ID`, and other Meta identifiers may be represented as empty placeholders in `.env.example` during Phase 1; real values remain outside Git.

## Consequences

- There is one auditable source of truth for Meta application authentication and webhook signature verification.
- Webhook handling must preserve the exact raw request bytes, calculate the expected HMAC SHA-256 with `META_APP_SECRET`, and compare it in constant time before parsing or persisting the payload.
- Rotation of the Meta app secret changes both OAuth/application authentication and webhook verification. The operational runbook must coordinate the Meta console update, Railway secret update, rollout, and verification test.
- `META_APP_SECRET` and the raw signature value must never appear in logs, errors, audits, n8n credentials, browser code, or repository files.
- Tests must include valid signature, invalid signature, missing signature, altered-body, and challenge-verification cases using fixture secrets only.
- Because no environment is deployed yet, no compatibility alias or fallback to `META_WEBHOOK_APP_SECRET` is needed. Introducing one later would require a new ADR and an explicit rotation plan.
- All Meta IDs, app credentials, and webhook configuration remain unique to Night Club AI and must never be copied from, connected to, or shared with La Boutique.
