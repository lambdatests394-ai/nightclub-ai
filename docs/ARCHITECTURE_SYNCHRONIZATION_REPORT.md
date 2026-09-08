# NIGHT CLUB AI v1.0 - Architecture Synchronization Report

**Mode:** Architecture synchronization only.  
**Source of truth compared:** Canonical NIGHT CLUB AI v1.0 requirements supplied by the project owner.  
**Compared artifact:** `docs/ARCHITECTURE_BLUEPRINT.md`.  
**Decision status:** No architectural decision in this report changes the blueprint. Every recommendation requires explicit approval before it is incorporated or implemented.

## Synchronization result

The blueprint is substantially aligned with the canonical requirements. There are **no direct conflicts**: no requirement is contradicted, omitted as a deliberate exclusion, or delegated to an incorrect system of record. The remaining items are missing product/operational decisions or design improvements that should be resolved before implementation approval.

## 1. MATCH

| Canonical requirement | Blueprint alignment | Status |
|---|---|---|
| Independent Night Club AI project | Separate GitHub repo, Railway services, Supabase project, Meta app, n8n instance, domains, keys, and CI scope; explicit prohibition on La Boutique dependencies | Match |
| FastAPI owns business logic | Modular FastAPI monolith owns state transitions, authorization, integration adapters, publication decisions, audit writes, and database access | Match |
| n8n owns orchestration and automation | n8n owns cron, dispatch, retries/orchestration, and notifications; it cannot mutate domain tables or hold social credentials | Match |
| Supabase PostgreSQL owns persistent data | Complete application schema, audit/outbox records, and transactional state reside in Supabase PostgreSQL | Match |
| Supabase Storage owns assets | Private `nightclub-assets` bucket, server-generated keys, signed upload/read URLs, asset metadata and validation flow | Match |
| Python 3.12 + FastAPI backend | Python 3.12 is pinned and FastAPI is the sole backend boundary | Match |
| Facebook Page generation and publishing | Facebook connection, approved-version workflow, scheduled publication jobs, attempts, reconciliation, and external post ID are specified | Match |
| WhatsApp Business integration | Separate WhatsApp adapter, verified webhook intake, delivery-status storage, conversations/messages, controlled outbound template/operator messages | Match |
| AI-assisted generation | Provider-neutral port, OpenAI/Gemini adapters, structured output, usage/cost records, and mandatory human approval | Match |
| Campaign management | Campaign lifecycle, briefs, date range, and campaign-to-content relationship are defined | Match |
| Content approval workflow | Versioned drafts, submit-for-review, approval/changes-requested decisions, separation of duties, and audit trail | Match |
| Scheduled publishing | `publication_jobs`, leases, due-job workflow, retries, cancellation, and current content status are defined | Match |
| Meta Graph API | FastAPI-owned Meta OAuth, encrypted credentials, Facebook/WhatsApp adapters, HMAC webhook verification, and provider reconciliation | Match |
| Future Instagram extensibility, not v1 | Reserved `instagram` platform value and adapter boundary; no endpoint, scope, webhook subscription, publishing behavior, or UI in v1 | Match |
| Secrets excluded from Git | Secret-store-only policy, `.env.example` names only, client prohibition, encryption and rotation strategy | Match |
| Auditable external events | Webhook inbox, publication attempts, AI generation records, automation runs, outbox, and immutable audit logs with correlation IDs | Match |
| Idempotent webhooks | Raw-body signature validation, durable webhook inbox, unique event/payload keys, quick acknowledgement, outbox processing | Match |
| Testability | Unit, integration, contract, E2E, security, and operational test layers are specified | Match |
| Independent Railway deployment | Dedicated `nightclub-api` and `nightclub-n8n` services, staging/production isolation, health checks, forward-only migrations, GitHub-driven release flow | Match |

## 2. CONFLICT

### No direct conflicts found

The blueprint does not contradict the canonical technology baseline, MVP scope, ownership principles, isolation rule, or Instagram exclusion.

The two apparent tensions below are already resolved within the proposal and are **not** canonical conflicts:

| Tension | Current proposal | Canonical requirement | Resolution already proposed | Reason |
|---|---|---|---|---|
| FastAPI ownership vs. n8n automation | n8n triggers and coordinates workflows; FastAPI validates state, leases jobs, publishes, and persists outcomes | FastAPI owns business logic; n8n owns orchestration | Preserve this separation | It prevents n8n from becoming an unauditable second backend while satisfying automation ownership |
| Exactly-once publish expectation | The system uses at-least-once delivery plus idempotency and reconciliation | Webhooks must be idempotent and external events auditable | Preserve at-least-once semantics; never claim distributed exactly-once publishing | Network timeouts and third-party Meta APIs can leave publication outcome ambiguous |

## 3. MISSING

These are absent from the canonical requirements or insufficiently specified for an implementation decision. They are not silent additions to scope.

| Area | Missing decision / detail | Why it matters | Required owner decision |
|---|---|---|---|
| Meta / Facebook | Exact Page IDs, Meta business ownership, required Graph permissions, sandbox availability, and app-review status | Determines whether publishing can be built/tested and which OAuth flow/scopes are valid | Provide/confirm separate Night Club AI Meta assets and approval timeline |
| WhatsApp | v1 must choose its minimum supported behavior: inbound inbox, delivery tracking, template sends, operator free-form replies, or all | Meta policy, templates, retention, UX, testing, and effort differ materially | Approve exact WhatsApp v1 scope and authorized phone/WABA assets |
| WhatsApp compliance | Data-retention period, consent/opt-out handling, operator access policy, and applicable local privacy/legal review | WhatsApp payloads can include personal data | Define policy and accountable owner before storing production messages |
| Supabase | Project region, tier, backup/PITR expectation, allowed direct API exposure, and production/staging project IDs | Latency, cost, recovery, and security controls depend on these choices | Select independent Night Club AI Supabase environments |
| Supabase database access | Exact database-role/RLS operating model for FastAPI is not fixed | A privileged PostgreSQL connection can bypass RLS; a user-scoped connection needs safe claim propagation | Approve either API-enforced authorization plus deny-by-default RLS, or a fully user-context-aware RLS design |
| Railway / n8n | n8n persistence topology, encryption key ownership, execution retention, admin ownership, and Railway region/plan | n8n needs durable configuration/execution state and safe credential management | Confirm independent n8n service storage/database and operational owner |
| Authentication | Initial login method, invited users, MFA policy, session duration, and recovery process | RBAC cannot be operationally complete without an identity policy | Approve Supabase Auth configuration and initial role assignments |
| AI | Default provider/model, language/brand guide, moderation policy, request budget, fallback policy, and data-retention preference | Quality, cost, privacy, and output behavior cannot be inferred safely | Approve AI operating policy and initial provider configuration |
| Storage | Permitted media formats/sizes, video support at launch, malware scanner decision, and asset retention/deletion window | Controls workload, cost, safety, and Facebook compatibility | Approve media policy |
| Notifications / operations | Alert destination, on-call owner, service-level target, and dead-letter replay authority | Failed publication and connection expiry need a human operational path | Assign support owner and channel |
| Domains | Public API, OAuth callback, webhook, and future UI domain names | Meta OAuth/webhook configuration requires exact HTTPS URLs | Reserve Night Club AI-only domains |
| Canonical API consumers | The current requirement names the backend but not a v1 UI/client or integration consumer | Determines initial UX acceptance testing and API authentication flow | Confirm whether v1 delivers API-first only or includes an operator client in a subsequent approved scope |

## 4. IMPROVEMENT

The following refinements improve correctness or reduce delivery risk. They do not alter the system ownership model or MVP scope unless explicitly approved.

| ID | Area | Current proposal | Recommended improvement | Reason |
|---|---|---|---|---|
| IMP-01 | Database relationships | `content_versions.ai_generation_id` is described but not declared as an FK; the ERD omits this relationship | Add FK `content_versions.ai_generation_id -> ai_generation_requests.id` and show it in the ERD | Preserves provenance of AI-derived content and prevents dangling generation references |
| IMP-02 | Database integrity | `review_decisions` has independent FKs to content and version, but no constraint that the version belongs to that content | Enforce with a composite unique key on `(content_item_id,id)` in versions and a composite FK, or validate in a transaction plus DB constraint | Avoids approval records that reference a version of a different item |
| IMP-03 | Database integrity | `content_items.connection_id` does not itself enforce same-organization and platform compatibility | Add a composite organization/connection constraint or an explicit trigger/transactional invariant; require `platform='facebook'` for publishable content jobs in v1 | Prevents cross-tenant or wrong-platform publication configuration |
| IMP-04 | Database integrity | Assets are linked to content version with no DB-level organization alignment rule | Enforce organization consistency when attaching assets and reject non-`ready` assets transactionally | Prevents cross-organization asset exposure |
| IMP-05 | Database integrity | The narrative says all tables have timestamps, but the listed complete schema does not show them uniformly | Specify the complete common columns, timestamps, soft-delete policy, and database update mechanism per table | Removes ambiguity before migrations are designed |
| IMP-06 | Supabase / FastAPI security | RLS is defense in depth, but the FastAPI database connection's role and RLS behavior are undecided | Document a single explicit access model: FastAPI is the exclusive data API, uses a least-privileged database role, enforces RBAC, and RLS denies browser/anonymous access; do not expose Supabase service keys to clients | Avoids a false assumption that RLS protects calls made through a privileged backend connection |
| IMP-07 | Storage security | Client completion checks object metadata but large-file integrity verification is only optional/asynchronous | Decide whether v1 accepts images only or adds an explicit verification/scanning queue before `ready` | Makes the asset security boundary testable and cost-bounded |
| IMP-08 | Webhook idempotency | Meta webhooks have durable deduplication; internal n8n commands are HMAC protected but their replay/idempotency record is not explicit | Require a signed event ID/idempotency key for every n8n-to-FastAPI mutation and store/replay its outcome through `idempotency_keys` or `automation_runs` | HMAC proves origin but does not by itself prevent an authorized request replay |
| IMP-09 | Meta webhook mapping | `webhook_events.organization_id` is nullable and the mapping from a received Meta object to a known connection is not specified | Define connection lookup precedence and quarantine unknown/ambiguous events with audit/alert status | Preserves tenant isolation and makes misconfiguration visible |
| IMP-10 | Meta credentials | `META_APP_SECRET` and `META_WEBHOOK_APP_SECRET` can imply two different secrets, although webhook HMAC normally uses app-secret material | Define one source of truth for the Meta app secret or explicitly document why distinct secret values are required | Prevents deployment misconfiguration and unverifiable signatures |
| IMP-11 | Facebook publication | The blueprint supports assets generically but does not lock the first Facebook formats/media combinations | Establish a v1 publishing capability matrix (text/link/photo first; video only if approved) and validate before schedule | Avoids late discovery of platform-specific restrictions |
| IMP-12 | WhatsApp architecture | Facebook and WABA selection are mentioned in one OAuth callback | Separate Facebook Page connection and WhatsApp Embedded Signup/WABA connection flows at the API boundary, even if they share a Meta business account | The platform assets, permissions, lifecycle, and policy checks are distinct |
| IMP-13 | n8n isolation | n8n credentials are independent, but its exact inbound/outbound trust boundary is broad | Restrict FastAPI-to-n8n delivery to a dedicated endpoint and n8n-to-FastAPI to allowlisted internal endpoints; sign both directions with timestamp, nonce, body hash, and key rotation ID | Reduces replay and credential-leak blast radius |
| IMP-14 | Railway deployment | Two Railway services are proposed; n8n's backing state is left open | Specify Railway service variables, persistent storage/database choice, deploy health check, and backup/recovery runbook for n8n separately from business data | Makes automation recoverable without violating Supabase ownership of business data |
| IMP-15 | Environment variables | App variables are detailed, but n8n runtime encryption/persistence variables and secret rotation cadence are not | Add an environment-variable classification table: public, secret, generated-at-deploy, owner, rotation frequency, and consumer service | Makes secret hygiene auditable and prevents accidental cross-project reuse |
| IMP-16 | Audit | Auditing is broad, but raw before/after snapshots can contain sensitive WhatsApp or credential-adjacent data | Define field-level audit redaction and immutable retention policy; never record credential material or full customer message body in generic audit snapshots | Keeps audit useful without duplicating sensitive data |
| IMP-17 | Project isolation | Isolation is strongly stated but not represented as a release gate | Add a pre-production isolation checklist: project IDs, domains, secret prefixes, repository remotes, Railway service IDs, Supabase refs, Meta app ID, and n8n instance must all match the Night Club AI inventory | Turns a policy into a verifiable control |
| IMP-18 | Complexity control | Multi-organization schema is future-friendly but slightly expands an MVP for one nightclub | Keep it only as a bounded isolation primitive: create one organization at bootstrap, do not expose tenant administration beyond required owner controls | Retains safe boundaries without prematurely delivering SaaS multi-tenancy |

## Focused synchronization review

### Supabase

**Match:** PostgreSQL is the durable source of truth; Storage is private and the blueprint avoids public direct access. Auth is available through Supabase JWTs.

**Approval-sensitive improvement:** Decide the FastAPI database role/RLS model before implementation (IMP-06) and establish independent project/staging/production identifiers. The service-role key must be server-only and used narrowly for Auth admin/Storage operations.

### FastAPI

**Match:** It is the sole business-logic and authorization boundary. Content state, publication, webhook persistence, integration credentials, and auditing remain in FastAPI.

**Approval-sensitive improvement:** Preserve this boundary when n8n workflows are created. No n8n node should contain state-transition, authorization, or direct SQL logic.

### n8n

**Match:** It orchestrates schedules, delivery/retry workflow, and operational notifications, backed by FastAPI's outbox and leases.

**Improvement:** Make internal mutating calls idempotent (IMP-08), explicitly configure the persistence/backup model (IMP-14), and narrow/sign both call directions (IMP-13).

### Railway

**Match:** It hosts independently deployable API and n8n services with environment separation, health checks, and CI/CD promotion.

**Missing:** Region, plan, domains, n8n backing state, and operational ownership are not yet selected.

### Meta APIs, Facebook, and WhatsApp

**Match:** FastAPI owns OAuth/credentials, Graph calls, verified webhooks, Page publishing, WhatsApp normalization, and delivery tracking. Instagram stays inactive.

**Improvement:** Use connection flows and capability matrices that distinguish a Facebook Page from a WABA/phone number (IMP-11, IMP-12). Do not assume a valid Facebook authorization automatically authorizes WhatsApp operations.

### AI abstraction

**Match:** OpenAI and Gemini are behind a provider port; outputs are structured, recorded, and cannot auto-publish.

**Missing:** Selection of default provider/model, brand policy, budget, moderation, fallback, and retention expectations. These are policy choices, not implementation defaults.

### Security and isolation

**Match:** Server-only secrets, encrypted platform credentials, RBAC, private assets, signature verification, audit records, and explicit La Boutique prohibition are all included.

**Improvement:** Add a machine-verifiable isolation release checklist (IMP-17), define audit redaction (IMP-16), and establish an environment-secret ownership/rotation register (IMP-15).

### Webhook idempotency

**Match:** Meta events are verified, persisted before asynchronous work, deduplicated, and quickly acknowledged. Publication uses durable lease/idempotency/reconciliation behavior.

**Improvement:** Apply equivalent persistent idempotency to n8n command retries and define unknown Meta-object quarantine behavior (IMP-08, IMP-09).

### Database relationships

**Match:** The principal organization, campaign, content version, asset, review, publication, AI, WhatsApp, webhook, outbox, and audit entities are present.

**Improvement:** Close the four integrity gaps in provenance and tenant/platform linkage before migration design (IMP-01 through IMP-05).

## FINAL ARCHITECTURE PROPOSAL

Adopt the blueprint's **modular FastAPI monolith** as the v1.0 architecture, with Supabase PostgreSQL/Storage as the persistent platform, n8n as signed orchestration only, Railway as the independent deployment target, and separate FastAPI adapters for Facebook Page, WhatsApp Business, OpenAI, and Gemini.

The required safety boundaries are:

1. FastAPI remains the only component that enforces authorization, business state, database writes, token use, publication, and audit creation.
2. n8n triggers and coordinates work only through signed/idempotent FastAPI endpoints; it has no direct access to business tables or Meta/AI credentials.
3. Supabase is private to the backend for application data and assets; browser clients receive only Auth tokens and short-lived signed Storage URLs.
4. Meta webhooks are raw-body HMAC verified, durably recorded, deduplicated, and processed asynchronously; publishing is reconciled after ambiguity.
5. AI generates proposals only; a human approval gate is required before any Facebook publication and AI WhatsApp replies are excluded from v1.
6. Instagram remains only a reserved extension boundary and is neither configured nor implemented in v1.
7. Every account, project, service, secret, domain, and CI environment is Night Club AI-specific, with a release gate proving no La Boutique reference or connection exists.

Before implementation approval, explicitly resolve the items in **MISSING** and approve/reject each item in **IMPROVEMENT**, especially IMP-01 through IMP-06, IMP-08, IMP-12, IMP-14, IMP-15, and IMP-17.

## ARCHITECTURE APPROVAL REQUIRED

This synchronization report does not authorize implementation. The project remains in planning mode until the project owner explicitly approves the final architecture proposal and any accepted improvements.
