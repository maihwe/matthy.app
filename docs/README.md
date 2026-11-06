# Phase 0 — Discovery & Design Documents

Per §51, these documents are produced **before production code is written**, and are
reviewed before implementation begins. §46 is explicit: *"No major coding until these
are reviewed."*

## Documents

| # | Document | Status | Depends on |
|---|---|---|---|
| 01 | [Product Requirements](01_PRODUCT_REQUIREMENTS.md) | Complete | — |
| 02 | [System Architecture](02_SYSTEM_ARCHITECTURE.md) | Complete | 01 |
| 03 | [Database Design](03_DATABASE_DESIGN.md) | Complete (ERD in §13) | 01, 02 |
| 04 | [API Specification](04_API_SPECIFICATION.md) | Complete (incl. API contract) | 03, 05 |
| 05 | [Role & Permission Matrix](05_ROLE_PERMISSION_MATRIX.md) | Complete | 01 |
| 06 | [Financial Rules](06_FINANCIAL_RULES.md) | Complete | 01, 03 |
| 07 | [Loan Rules](07_LOAN_RULES.md) | Complete | 06 |
| 08 | [Disbursement Rules](08_DISBURSEMENT_RULES.md) | Complete | 06 |
| 09 | [Notification Design](09_NOTIFICATION_DESIGN.md) | Complete | 04 |
| 10 | [Security Requirements](10_SECURITY_REQUIREMENTS.md) | Complete | 01, 02 |
| 11 | [Team Workflow](11_TEAM_WORKFLOW.md) | Complete | — |
| 12 | [Deployment Plan](12_DEPLOYMENT_PLAN.md) | Complete | 02 |
| 13 | [Testing Strategy](13_TESTING_STRATEGY.md) | Complete | 06, 07, 08 |
| 14 | [Regulatory Checklist](14_REGULATORY_CHECKLIST.md) | Complete | 01 |
| — | Database ERD (§51) | 03 §13; physical `ERD.dbml` deferred to Phase 1 so it derives from migrations | 03 |
| — | API Contract (§51) | 04 §7, versioned and diffed in CI (04 §15) | 04 |

All fourteen documents are drafted and **reviewed and accepted by the repository owner**
(§46's gate). Implementation has begun: [PHASE_1_PLAN.md](PHASE_1_PLAN.md) records how the
four gate conditions below were closed, the Phase 1 scope, and the slice order.

## Phase 1 documents

These are working documents, not part of the Phase 0 set. They are amended as
implementation teaches something the design did not anticipate.

| Document | Status |
|---|---|
| [Project Handover](PROJECT_HANDOVER.md) | Start here — completed milestone, saved GitHub/CI state, remaining work and next-session checklist (2026-11-06) |
| [Phase 1 Plan](PHASE_1_PLAN.md) | Active — slices S0–S5 (S1 split into S1a/S1b), decisions P1-1…P1-18, blocking items §6, **specification conflicts C1–C7 in §7** |
| [Branch Protection](BRANCH_PROTECTION.md) | Configured and verified on `main` and `develop` — active GitHub rulesets, required CI/PRs and administrator update restrictions |

## Decisions resolved

| Ref | Question | Answer |
|---|---|---|
| Q7 | Money as integer minor units? | **Yes** — `BIGINT`, never floating point |
| Q9 | Double-entry ledger? | **Yes** — deferred constraint trigger enforces the balance invariant |
| Q1 | Fund custody? | **Design for both** via `platform_settings.custody_model`; default `SYSTEM_OF_RECORD`; legal position settled before Phase 3 |
| Q2 | Interest model? | **No default** — the group must choose flat, reducing balance, or none explicitly (07 §3) |
| Q3 | Who bears a settlement shortfall? | **Recorded, not apportioned** — posts to suspense `2100` with a mandatory explanation; a group officer resolves each case (06 §5.3) |
| Q4 | Where do late fees accrue? | **The group**, on contributions only; loan penalties out of MVP scope (06 §7) |

## Open questions

Unresolved items are recorded rather than invented — §52 requires that ambiguity be
surfaced and confirmed, never silently resolved into a financial rule. The full list
with reasoning lives in [01_PRODUCT_REQUIREMENTS §7](01_PRODUCT_REQUIREMENTS.md#7-open-questions).

| Ref | Question | Blocks | Status |
|---|---|---|---|
| Q5 | KYC tiers and thresholds? | 10, 14 | Open — design accommodates tiers; thresholds are counsel's answer |
| Q6 | Are groups registered entities? | 14 | Open — schema supports both; §45 review |
| Q8 | Single-currency MVP? | 03, 06 | Open — schema is multi-currency, and currencies are a table of their own; MVP launches one currency |
| Q10 | Are non-fiat units (crypto-assets) in scope? | 03, 06 | Open — **not supported**, by arithmetic: `BIGINT` minor units fit exponents 0–4, not 18 (06 §3.4.5) |
| Q1 | Platform custody of funds? | 06, 08, 12, 14 | Design-for-both locked; **the legal position must be settled before Phase 3** |

None of the five blocks Phase 1. Each has a stated working assumption in the document
that depends on it, labelled as an assumption rather than presented as a decision.

## Document conventions

- **Requirement IDs are stable.** `FR-XXX-###` and `NFR-XXX-###` are referenced by the
  API spec, permission matrix, and test strategy. Retire an ID; never renumber one.
- **Money is written as integer minor units** where a value is being stored, and as
  decimal currency where it is being displayed (`BIGINT` kobo; API speaks `"1000.00"`).
- **Unresolved decisions are marked**, not guessed. A document that invents a financial
  rule to look complete is worse than one that admits a gap.

## Phase 0 completion

The document set is complete. What it commits the project to, in one place:

| Artifact | Where | Nature |
|---|---|---|
| **Traceability spine** | 01's `FR-XXX-###` / `NFR-XXX-###` IDs | Cited by 04, 05, 06, 07, 08, 09, 10, 13 |
| **API contract** | 04 §7 | Versioned, breaking changes diffed in CI (04 §15) |
| **Posting rules** | 06 §4.13 | The reference implementation target — 16 event types |
| **Chart of accounts** | 06 §2, seeded by 03 | Balances derived, never stored as a running total |
| **Permission catalogue** | 05 §4–5 | ~100 codes; `has_perm()` is the single enforcement path |
| **ERD** | 03 §13 | Physical `ERD.dbml` deferred so it derives from migrations |
| **Test obligations** | 13 §8 | **102 required tests**, indexed across six documents |
| **CI gates** | 11 §6, 13 §10 | **Ten**, all blocking; the test gate expands into five suites |

### Before Phase 1 begins

1. ~~**Review these documents** (§46 — this is the gate, not a formality).~~
   **Closed** — reviewed and accepted by the repository owner.
2. ~~Confirm the Phase 1 scope boundary stated in 01's out-of-scope list.~~
   **Closed** — restated as an explicit exclusion list in
   [PHASE_1_PLAN §2](PHASE_1_PLAN.md).
3. ~~Confirm the four open questions have working assumptions the user accepts for now.~~
   **Closed** — each carries a stated working assumption in the document that depends on
   it, and none blocks Phase 1.
4. Confirm the §45 legal review is engaged — it gates launch, not Phase 1, but its
   answers (Q1 custody, Q5 KYC, Q8 currency) reach back into the schema.
   **Still open, and not a Phase 1 blocker.** Reaches back into the schema at Phase 3.

Phase 1 is under way. See [PHASE_1_PLAN.md](PHASE_1_PLAN.md) for what S0 delivered and
what remains unverified.
