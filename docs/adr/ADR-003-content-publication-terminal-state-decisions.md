# ADR-003: Content Scheduling Cancellation and Permanent Publication Failure

**Date:** 2026-09-02  
**Status:** Accepted for Phase 3 domain model.

## Context

The `content_status` state machine specifies scheduling and publication states, but did not explicitly define the semantic result of cancelling a schedule or receiving a permanent publication failure.

## Decision taken

1. Cancelling a schedule changes `publication_jobs.status` to `cancelled` and returns the associated `content_items.status` from `scheduled` to `approved`. The approved content version remains available for a new explicit schedule.
2. A permanent publication failure changes `content_items.status` from `publishing` to `failed`. It requires an explicit human recovery action; it never silently returns to `approved` or retries.

## Consequences

- Cancelling a time slot is not treated as cancelling the content itself.
- Permanent Meta failures remain visible and auditable rather than entering an automatic retry loop.
- Future recovery endpoints/workflows must model a deliberate operator action and audit it; they are outside this phase.
