# 02 — System Architecture

**Status:** Phase 0 draft, awaiting review
**Derives from:** 01_PRODUCT_REQUIREMENTS, Master Development Prompt §1, §2, §19, §28, §29, §36, §39, §40, §47, §49
**Depends on:** 01_PRODUCT_REQUIREMENTS

---

## 1. Architectural principles

Five principles govern every structural decision in this document. Where a later
choice looks odd, it is because one of these outranks convenience.

**P1 — Financial correctness outranks everything.** §52: "Never sacrifice financial
correctness for development speed." A design that is slower to build but provably
correct is chosen over a faster one that is merely probably correct.

**P2 — The ledger is the single source of truth.** Balances, totals, and reports are
derived. Nothing about a balance is authoritative except the entries that produced it.

**P3 — A modular monolith, not microservices.** §2: the platform "should initially use
a modular monolith rather than unnecessary microservices," while being "designed so
individual services can later be extracted when scale requires it." The module
boundaries below are therefore load-bearing: they are what makes extraction cheap later.

**P4 — Business logic lives in services.** §48 forbids business logic inside templates
or scattered across API views. Views parse and authorise; services decide and act.

**P5 — Boundaries are enforced by dependency direction, not by discipline.** If the
import graph is not policed, a modular monolith degrades into a ball of mud. §5.4 makes
the rule mechanical and testable.

---

## 2. System context

```
        ┌──────────────────┐        ┌──────────────────┐
        │  Mobile app      │        │  Web admin       │
        │  (Flutter/RN)    │        │  dashboard       │
        └────────┬─────────┘        └────────┬─────────┘
                 │  HTTPS / JSON             │  HTTPS / HTML
                 │  JWT + refresh            │  session cookie
                 └───────────┬───────────────┘
                             ▼
                 ┌───────────────────────┐
                 │   REST API  /api/v1/  │
                 │   OpenAPI-documented  │
                 └───────────┬───────────┘
                             ▼
                 ┌───────────────────────┐
                 │   Django application  │
                 │   DRF · services      │
                 │   modular monolith    │
                 └──┬─────────┬────────┬─┘
                    │         │        │
        ┌───────────┘         │        └──────────────┐
        ▼                     ▼                       ▼
 ┌─────────────┐      ┌─────────────┐        ┌──────────────┐
 │ PostgreSQL  │      │   Redis     │        │   Celery     │
 │ source of   │      │ cache ·     │        │   workers    │
 │ truth,      │      │ broker ·    │◄──────►│   + beat     │
 │ ledger      │      │ rate limits │        └──────┬───────┘
 └─────────────┘      └─────────────┘               │
                                                    ▼
                                          ┌──────────────────┐
                                          │ Background jobs  │
                                          │ email · SMS ·    │
                                          │ push · reports · │
                                          │ reconciliation   │
                                          └──────────────────┘
                                                    │
                                                    ▼
                                          ┌──────────────────┐
                                          │ Payment providers│
                                          │ (behind the      │
                                          │  abstraction)    │
                                          └──────────────────┘
```

Two things in that diagram carry most of the architectural weight: **PostgreSQL is the
only source of truth for money** (Redis holds caches and queues, never balances), and
**payment providers sit behind an abstraction** that the domain talks to in its own
vocabulary.

---

## 3. Deployment topology

### 3.1 Initial (§39)

```
Load balancer
     │
     ▼
Django application  ──►  PostgreSQL
     │                    +
     ▼                    Redis
   Celery                 Celery
```

A single application process, a primary database, and Redis. This is the correct
starting point and it will carry a long way — the MVP's job is to be simple enough to
"develop, test, deploy, and operate reliably" (§52), not to pre-empt scale it has not
yet earned.

### 3.2 As traffic increases (§39)

```
                     Load balancer
                          │
              ┌───────────┼───────────┐
              ▼           ▼           ▼
            API         API         API
              └───────────┼───────────┘
                          ▼
                       Redis
                          │
                    PostgreSQL
                          │
                   Read replicas
```

### 3.3 Deferred until justified

Read replicas, connection pooling, application caching, additional queue workers,
object storage, CDN, horizontal scaling, dedicated reporting infrastructure, and
service extraction. Each is introduced when a measurement demands it, and §38 is
explicit that capacity claims are not made before load testing supports them.

---

## 4. Application modules

§49's structure, with the dependency rules made explicit:

```
matthy.app/
├── manage.py
├── requirements/
├── config/
│   ├── settings/          base.py · dev.py · test.py · staging.py · production.py
│   ├── urls.py
│   ├── asgi.py
│   └── wsgi.py
├── apps/
│   ├── accounts/          users, auth, devices, sessions, KYC, roles
│   ├── locations/         country → state → lga → city → area → street
│   ├── groups/            groups, membership, group roles & permissions
│   ├── contributions/     schemes, schedules, contributions, receipts
│   ├── collectors/        assignment, field collection, settlement, variance
│   ├── payments/          abstraction layer, providers, attempts, webhooks
│   ├── ledger/            accounts, entries, transactions, reversals, balances
│   ├── loans/             eligibility, applications, guarantors, repayments
│   ├── disbursements/     requests, approvals, payment, verification
│   ├── notifications/     dispatch, templates, delivery records
│   ├── communications/    campaigns, bulk email, SMS, messaging
│   ├── reports/           read-only aggregation and export
│   ├── audit/             immutable audit trail
│   └── administration/    platform admin views and operations
├── tests/
├── docker/
├── scripts/
└── docs/
```

Every app follows the internal shape §47 prescribes:

```
apps/<module>/
├── models.py          or models/ package when it grows
├── serializers.py     request/response shaping only
├── services.py        or services/ package — all business logic
├── selectors.py       read-side query composition
├── permissions.py     DRF permission classes
├── validators.py      reusable validation
├── tasks.py           Celery tasks (thin — they call services)
├── urls.py
├── views.py           thin: parse, authorise, delegate, serialise
├── admin.py
├── migrations/
└── tests/
```

### 4.1 Layer assignment

Modules are assigned to layers. **A module may import from its own layer or any layer
below it. Never from a layer above.**

| Layer | Modules | Role |
|---|---|---|
| **L0 — Foundation** | `accounts`, `locations`, `audit` | Identity, place, and the audit trail. Depend on nothing. |
| **L1 — Structure** | `groups`, `contributions`, `collectors` | Who contributes what, where, and who collects it. |
| **L2 — Financial** | `ledger`, `payments`, `loans`, `disbursements` | Movement of money. |
| **L3 — Engagement** | `notifications`, `communications`, `reports` | Reaching people and explaining what happened. |
| **L4 — Presentation** | `administration` | Platform admin surfaces. |

### 4.2 The ledger inversion

The most important rule in this architecture:

> **`ledger` is at L2 but depends on nothing above L0. `payments`, `loans`, and
> `disbursements` import `ledger`. `ledger` never imports them.**

The ledger does not know what a loan is. It knows how to post a balanced set of
entries against accounts, within a transaction, idempotently. Loans, payments, and
disbursements translate their domain events into ledger postings.

This is what keeps the ledger trustworthy. A ledger that understands loans can develop
loan-shaped bugs. A ledger that only understands debits and credits can be verified
against itself, which is what FR-LED-010 requires.

```
loans ─────┐
payments ──┼──► ledger.service.post(entries, idempotency_key)  ──► PostgreSQL
disburse ──┘
```

### 4.3 `audit` is special

`audit` is at L0 because everything must be able to write to it, but it is
**write-mostly and append-only**. There is no application code path that updates or
deletes an audit record (FR-AUD-003). Enforcement is twofold: no service exposes such
an operation, and the database enforces it (see 03_DATABASE_DESIGN) so that a future
developer with a shell cannot quietly violate it.

---

## 5. Cross-cutting architecture

### 5.1 Money representation

**Ratified (PRD Q7):** all monetary values are stored and computed as **integer minor
units** — kobo for NGN, so ₦1,000.00 is `100000`.

Rationale: binary floating point cannot represent decimal fractions exactly, so
`0.1 + 0.2 != 0.3` in IEEE 754. In a system whose entire purpose is to have the right
number of kobo in the right place, that is a defect rather than a tradeoff. Integers
make addition and subtraction exact. Where division is unavoidable — splitting
interest, allocating a percentage fee — the rounding rule and the treatment of the
remainder are specified explicitly in 06_FINANCIAL_RULES rather than left to whatever
the language does.

**"Minor units" means the currency's own exponent, not two decimal places.** NGN is 2,
JPY is 0, KWD is 3 — the exponent and the rounding mode are both attributes of the
currency in the `currencies` table (03 §4.5.2), never of the country and never a
constant in code. A platform that assumes 2 cannot represent yen, and the assumption is
invisible until it reaches a market that uses them.

A single `Money` value type wraps the integer and its currency so that arithmetic
across currencies, and accidental mixing of a minor-unit integer with a major-unit
decimal, are caught rather than silently permitted.

The API speaks in decimal strings (`"1000.00"`), matching §43's contract example.
Conversion happens at the serialiser boundary, in one place, and nowhere else, and it
reads the exponent from the currency rather than assuming two digits.

**The boundary:** this representation fits fiat currencies with exponents 0–4. It does
not fit 18-decimal digital assets, where a `BIGINT` would hold about nine whole units
(06 §3.4.5, PRD Q10). That is a scope boundary rather than a limitation to engineer
around.

### 5.2 Idempotency

Required by FR-CON-003, FR-PAY-005, FR-LED-009, and FR-APP-015. Every financial
mutation accepts a client-supplied `idempotency_key`.

```
request ──► key seen before?
              ├── no  ──► execute, store (key → result), return result
              └── yes ──► return the stored result, execute nothing
```

The key is unique per scope (typically user + operation + key) and the store is a
database table, not a cache — a cache that evicts an idempotency key converts
"safe to retry" into "charges twice." The write of the key and the execution of the
operation share one transaction, so a crash mid-operation cannot leave a key that
claims success without the work having happened.

### 5.3 Transaction and locking discipline

| Concern | Approach |
|---|---|
| Multi-step financial writes | `transaction.atomic()` — all or nothing |
| Concurrent balance mutation | `select_for_update()` on the balance-bearing rows |
| Lock ordering | Consistent, documented order across all call sites, to avoid deadlock |
| External calls (payment providers) | **Never inside a transaction.** The transaction is short and purely local; provider calls happen outside it, with their own idempotency. |
| Nested atomic blocks | Permitted; the outermost defines the real boundary |

The exclusion in row three matters: holding a database transaction open across a
network call to a payment provider is how a slow provider becomes a platform-wide
outage, and how a rolled-back transaction ends up disagreeing with money the provider
actually moved.

### 5.4 Enforcing module boundaries

Principle P5 — boundaries must be mechanical, not aspirational. An import-linter
contract in CI asserts the layer rules from §4.1 and the ledger inversion from §4.2. A
developer who imports `loans` from `ledger` gets a failing build, not a code-review
comment that may or may not be read.

This is the single cheapest thing in the whole architecture. A modular monolith
degrades through imports, and the only durable defence is a test that fails.

### 5.5 Asynchronous processing (§36)

Celery + Redis for: email, SMS, push notifications, payment reconciliation, scheduled
contribution reminders, loan reminders, report generation, bulk operations, and other
long-running work. **No expensive background work runs inside a request/response cycle**
(FR-COM-003).

Celery beat drives scheduled work — contribution due reminders, loan repayment
reminders, overdue detection, and reconciliation sweeps.

Tasks are thin wrappers. A task's body calls a service function; it does not contain
business logic. This keeps every rule testable without a running worker, and keeps
retry semantics (which belong to the task) separate from correctness semantics (which
belong to the service).

**Retry safety.** Every financial task must be idempotent, because Celery will run a
task more than once after a worker dies mid-execution. Tasks that move money take
their idempotency key from the domain event (e.g. the payment attempt ID), never from
a freshly generated UUID.

### 5.6 Caching

Redis caches read-heavy, non-authoritative data: location lookups, permission sets,
scheme configuration, dashboard aggregates. Caching is a performance concern, and two
things are never cached:

- **Balances.** They are derived from the ledger (P2). A cached balance that drifts
  from its entries is precisely the failure the whole design exists to prevent.
- **Authorisation decisions** that could outlive a permission change — a revoked
  permission that persists in cache is a security hole.

Cache keys that depend on permissions include the permission version in the key, so
revocation invalidates rather than lingers.

### 5.7 Authentication

Two paths, deliberately (Phase 0 decision):

| Client | Mechanism | Notes |
|---|---|---|
| Mobile app | JWT access + refresh | Stateless, access token short-lived, refresh rotation with reuse detection |
| Web admin | Django session cookie | Retains Django's session and CSRF protections for the browser admin |

Both resolve to the same user and permission layer beneath, so authorisation logic
exists once. §26's requirement for "JWT **or** secure session authentication as
appropriate" is met by using each where it is appropriate rather than forcing one onto
both clients.

Details — token lifetimes, rotation, revocation, lockout thresholds, session
invalidation — belong to 10_SECURITY_REQUIREMENTS.

### 5.8 Payment abstraction (§19)

```
              Application
                  │
                  ▼
          PaymentProvider  (interface)
          ┌───────┴────────┐
          ▼                ▼
   MockProvider      PaystackProvider        ← reference implementation
          │                │
          └───────┬────────┘
                  ▼
              Webhook
                  ▼
            Verification
                  ▼
               Ledger
                  ▼
              Receipt
```

The interface is defined in the domain's vocabulary — initiate a charge, verify a
transaction, parse a webhook, refund — not in any provider's. The domain never sees a
Paystack-shaped object; it sees a verified payment outcome and posts it.

`MockProvider` is a first-class citizen, not a test fixture afterthought: it can
simulate success, failure, timeout, duplicate webhook delivery, and webhook arriving
before verification. Phase 3's tests are built against it, which is what makes the
abstraction genuinely provider-neutral rather than nominally so.

Three rules from §19 that shape the code:

- **The client is never believed.** Payment success is established by server-side
  verification (FR-PAY-003). A client-reported success is an unverified claim.
- **Webhooks are untrusted input.** Authenticated where supported, validated,
  idempotent, logged, safely retried (FR-PAY-004).
- **Webhook before redirect is normal.** Verification must be able to conclude
  correctly regardless of the order in which webhook and client return arrive.

### 5.9 Offline and poor-network support (§35)

The collector's app is the demanding case: a person moving from member to member, in a
market, on a poor connection, recording cash they are physically holding.

```
device records collection
        │
        ▼
local queue (durable, survives app restart)
        │
        ▼
   connectivity?
     ├── yes ──► POST with idempotency_key ──► server dedupes, records
     └── no  ──► stays queued, visible in sync status
```

Design rules:

- **A queued record is never silently discarded** (FR-APP-014). If the app cannot
  confirm the server accepted it, it says so. Losing a record of cash already in hand
  is the worst outcome in this system.
- **Every queued record carries a client-generated idempotency key**, so a retry after
  an ambiguous failure cannot double-post.
- **Conflicting data is surfaced, not resolved silently.** If the server has a
  different value for the same reference, that is a human's problem to settle, and the
  system's job is to make it visible.
- **Read-only data caches; financial writes queue.** Caching reads is safe; pretending
  a write succeeded is not.

---

## 6. API design (§29, §43)

Versioned under `/api/v1/`. Resource groups per §29: `auth`, `users`, `locations`,
`groups`, `schemes`, `contributions`, `payments`, `loans`, `guarantors`,
`disbursements`, `ledger`, `notifications`, `communications`, `reports`.

- Documented with OpenAPI/Swagger, generated from the code so the spec cannot drift
  from the implementation.
- Request and response schemas defined explicitly — no untyped pass-through.
- Consistent error responses across every endpoint, so client error handling is
  written once.
- **Contracts are frozen before implementation** (§43) and changes to a frozen
  contract are versioned or explicitly negotiated, never slipped in.

Full endpoint specification, including the contribution contract from §43, belongs to
04_API_SPECIFICATION.

---

## 7. Path to service extraction

§2 lists possible future services: authentication, payment, notification, loan,
reporting, fraud/risk, communication. The layer structure in §4.1 is what makes them
extractable.

| Future service | Extract from | Already isolated because |
|---|---|---|
| Payment service | `payments` | Already behind the `PaymentProvider` interface |
| Notification service | `notifications` | Already asynchronous; callers emit events, not sends |
| Communication service | `communications` | Already Celery-driven with no synchronous callers |
| Loan service | `loans` | Owns its data; talks to the rest only via ledger postings |
| Reporting service | `reports` | Read-only; can move to a replica with no domain change |
| Auth service | `accounts` | Already the only issuer of identity |

**No extraction happens before §38's load testing justifies it.** The table exists to
prove the boundaries are in the right places, not as a roadmap to be followed on
schedule.

---

## 8. Environments (§40)

| Environment | Purpose | Data |
|---|---|---|
| Development | Local work, Docker Compose | Synthetic |
| Testing | Automated test runs | Synthetic, created and destroyed per run |
| Staging | Pre-production verification | Production-shaped, anonymised |
| Production | Live | Real |

**Production is never a development target** (§40). Staging exists so that the
temptation to do so never arises. Secrets are supplied by environment, never committed;
`.env` files are git-ignored and a committed `.env.example` documents the shape.

---

## 9. Open questions inherited from 01

This document is written on these assumptions. If any is overturned, the affected
section is revised before implementation:

| Ref | Assumption | Affects |
|---|---|---|
| PRD Q1 | The platform does not take custody of funds | §5.8, §7, and the whole regulatory posture |
| PRD Q7 | Money is integer minor units | §5.1, and every model in 03 |
| PRD Q8 | MVP is single-currency (NGN) | §5.1, §6 |

**New in this document:**

**Q9 — Double-entry ledger.** This architecture assumes the ledger is **double-entry**:
every transaction posts two or more entries that sum to zero, so the books balance by
construction and FR-LED-010 becomes a queryable invariant rather than a hope. The
spec requires that "every movement of money must be recorded" (§12) and that balances
be derived (§13), which double-entry satisfies directly; it does not mandate the
mechanism. **Confirming this before 03_DATABASE_DESIGN is written matters**, because
the ledger schema differs substantially between single-entry and double-entry, and
retrofitting double-entry onto a live ledger is not a migration anyone wants to run.
