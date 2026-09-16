
# 01 — Product Requirements

**Project:** matthy.app — Community Contribution, Savings, Borrowing & Disbursement Platform
**Status:** Phase 0 draft, awaiting review
**Derives from:** Master Development Prompt §1–§52
**Depends on:** — (this is the root document)

---

## 1. Purpose

matthy.app is a platform for organised community finance. It serves the savings and
lending structures that already exist across Nigeria and similar markets — contribution
groups, daily collectors ("esusu"/"ajo" style), cooperatives, associations, and
street-level savings groups — and replaces the paper ledger and the WhatsApp
reconciliation thread with a system that is financially correct, auditable, and usable
on a phone.

The organising insight is that **these groups already work**. The problem is not that
they lack a process; it is that their process lives in exercise books, memory, and
trust. The platform's job is to record that process faithfully and make it verifiable,
not to impose a different one.

### 1.1 What "financially correct" means here

This is the standard the whole system is held to, and it is stricter than a typical
CRUD application:

- Every movement of money produces an immutable ledger entry.
- Balances are **derived from the ledger**, never stored as editable fields.
- A completed financial transaction is never deleted or silently edited. Errors are
  corrected by posting a reversing entry that references the original.
- Every financial operation is idempotent — a retried request, a duplicated webhook,
  or a replayed offline sync must not move money twice.
- No financial figure shown to a user is computed on the client.

### 1.2 Success criteria

The platform succeeds when a group can run its entire financial life on it — collect,
record, reconcile, lend, repay, disburse, report — with a complete audit trail that
survives scrutiny by an auditor, a regulator, or a disputed member.

---

## 2. Domain hierarchy

The platform's structural spine (§3):

```
Country → State → LGA → City/Town → Area → Street → Group → Contribution Scheme → Members
```

Two rules that the domain model must enforce structurally, not by convention:

- **A street is not a group.** A street is a location — a container. Groups are created
  *at* a street.
- **One street can contain many groups.** A single street may host a daily contribution
  group, a monthly savings group, and a traders' association, each with its own
  membership, schemes, and officers.

```
Nigeria
└── Lagos
    └── Ikeja
        └── Opebi
            └── Opebi Street
                ├── Opebi Daily Contribution
                ├── Opebi Monthly Savings
                └── Opebi Traders Group
```

Naming of the middle tiers is jurisdiction-specific in the real world (Nigeria uses
State/LGA; other countries do not). The schema models these tiers **generically** so
the platform is not welded to one country's administrative vocabulary — but the
user-facing labels are seeded per country, and Nigeria ships with Nigeria's.

---

## 3. Actors

| Actor | Description |
|---|---|
| **Member** | Belongs to one or more groups. Contributes, may borrow, may guarantee. |
| **Collector** | Field agent who collects cash from members and records it. May or may not be a member of the group. |
| **Group officer** | Owner / Chairman / Secretary / Treasurer / Auditor / Loan Officer — see §5. The set is **configurable per group**. |
| **Group administrator** | A member with administrative permission over one or more groups. Not a separate account type. |
| **Platform support** | Handles user-facing issues. Does not move money. |
| **Platform admin** | Operates the platform. Does not move money. |
| **Super admin** | Highest privilege. Protected from ordinary deletion. |

A single person holds different roles in different groups. The role is scoped to the
group, never global (§5):

```
Mathias
├── Group A → Member
└── Group B → Treasurer
```

---

## 4. Functional requirements

Requirement IDs are stable and are referenced by the API specification (04), the
permission matrix (05), and the test strategy (13). **FR IDs are never renumbered** —
retire them instead.

### 4.1 Authentication & user management (§4)

| ID | Requirement |
|---|---|
| FR-AUTH-001 | A user registers with full name, phone number, and password. Email is optional at registration but required for email-based recovery. |
| FR-AUTH-002 | A user logs in with either phone number or email, plus password. |
| FR-AUTH-003 | A user sets a transaction PIN, distinct from the login password, used to authorise financial actions. |
| FR-AUTH-004 | Passwords are hashed with Django's password hasher. **Plain-text passwords are never stored, logged, or transmitted into storage.** |
| FR-AUTH-005 | The PIN is stored hashed with the same standard as passwords. |
| FR-AUTH-006 | Email verification via a single-use, expiring token. |
| FR-AUTH-007 | Phone verification via OTP. |
| FR-AUTH-008 | OTPs are single-use, rate-limited, and expire. |
| FR-AUTH-009 | Password reset via email or phone, using a single-use expiring token. |
| FR-AUTH-010 | Two-factor authentication is available and can be enabled per user. |
| FR-AUTH-011 | A user can view and revoke active sessions. |
| FR-AUTH-012 | A user can view registered devices and revoke a device's access. |
| FR-AUTH-013 | Account status (active / suspended / closed) is tracked and enforced on every authenticated request. |
| FR-AUTH-014 | Verification status is tracked separately from account status. |
| FR-AUTH-015 | Last login and registration date are recorded. |
| FR-AUTH-016 | A user profile carries: profile picture, address, country, state, LGA, city, area, street. |
| FR-AUTH-017 | A user's sensitive profile changes (phone number, PIN, email) are audit-logged with previous and new values. |

### 4.2 Roles & permissions (§5)

| ID | Requirement |
|---|---|
| FR-RBAC-001 | Roles are scoped to a group. The same user may hold different roles in different groups. |
| FR-RBAC-002 | The set of roles a group uses is **configurable**. A daily collection group needs only Owner, Collector, Members; a formal association may use Chairman, Secretary, Treasurer, Collector, Auditor, Members. |
| FR-RBAC-003 | Platform-level roles (Super Admin, Platform Admin, Support Admin) are separate from group-level roles. |
| FR-RBAC-004 | Permissions are configurable per role, within platform safety limits. |
| FR-RBAC-005 | No user may approve their own restricted financial transaction (separation of duties). |
| FR-RBAC-006 | Role changes are audit-logged. |
| FR-RBAC-007 | A group must always retain at least one owner. |

### 4.3 Locations (§3)

| ID | Requirement |
|---|---|
| FR-LOC-001 | Location tiers are modelled generically, not hard-coded to Nigerian administrative names. |
| FR-LOC-002 | The hierarchy is Country → State → LGA → City/Town → Area → Street. |
| FR-LOC-003 | Locations are searchable by name at every tier. |
| FR-LOC-004 | A user can find a street and see the groups hosted on it. |
| FR-LOC-005 | Location records are administratively managed; ordinary users cannot create top-tier locations. |
| FR-LOC-006 | Location changes to a group or user are audit-logged. |

### 4.4 Groups (§6)

| ID | Requirement |
|---|---|
| FR-GRP-001 | A user can create a group, specifying name, description, type, and street location. |
| FR-GRP-002 | A group has an optional logo, a group type, and a status. |
| FR-GRP-003 | A group has an owner. Ownership is transferable to another member. |
| FR-GRP-004 | A group can have multiple contribution schemes. |
| FR-GRP-005 | Membership is by invitation or by application, per group configuration. |
| FR-GRP-006 | Membership may require approval before it becomes active. |
| FR-GRP-007 | A membership can be suspended and reinstated. Suspension does not erase history. |
| FR-GRP-008 | Full membership history is retained — join, approve, suspend, reinstate, leave, remove. |
| FR-GRP-009 | A group can define written group rules, visible to members. |
| FR-GRP-010 | A group has a location: country, state, LGA, city, area, street. |
| FR-GRP-011 | Group settings changes are audit-logged. |

### 4.5 Contribution schemes (§7)

This is a core module. It must not be modelled as one application per frequency.

| ID | Requirement |
|---|---|
| FR-SCH-001 | A group can create multiple contribution schemes. |
| FR-SCH-002 | Supported contribution frequencies: daily, weekly, monthly, every 3 months, every 6 months, every 12 months — modelled as a reusable frequency, not separate code paths. |
| FR-SCH-003 | A scheme carries: name, description, frequency, amount, minimum amount, maximum amount, start date, end date, grace period, late fee, collection method(s), collector, and disbursement rules. |
| FR-SCH-004 | **Contribution frequency and disbursement frequency are independent.** Daily contributions with monthly disbursement is valid; monthly contributions with 6-monthly disbursement is valid. |
| FR-SCH-005 | A scheme supports one or more collection methods. |
| FR-SCH-006 | A scheme's members are tracked with their participation status and their own schedule. |
| FR-SCH-007 | A scheme can be archived; archiving preserves all history and prevents new contributions. |
| FR-SCH-008 | A scheme's financial parameters cannot be mutated once contributions exist against it. Changes are made by closing the scheme and creating a successor, so that historical schedules remain reproducible. |

### 4.6 Collection methods (§8)

| ID | Requirement |
|---|---|
| FR-COL-001 | Supported methods: cash collector, bank transfer, payment gateway, card, USSD where supported. |
| FR-COL-002 | A single scheme may accept multiple methods. |
| FR-COL-003 | The method actually used is recorded on every contribution record. |
| FR-COL-004 | Methods are represented in the domain, not hard-coded in views. |

### 4.7 Collector system (§9, §10)

The platform must support traditional daily cash collection — this is a first-class
workflow, not a special case of online payment.

| ID | Requirement |
|---|---|
| FR-CLT-001 | A collector is assigned to a scheme. A collector may serve multiple schemes. |
| FR-CLT-002 | The collector dashboard shows: today's expected total, amount collected, amount outstanding, and member count. |
| FR-CLT-003 | For each member the collector sees: member name, expected amount, payment status, amount collected, time collected, payment method, receipt/reference, outstanding amount. |
| FR-CLT-004 | A collector can record a collection in a small number of taps, optimised for moving member to member. |
| FR-CLT-005 | A collector can record a partial payment; the outstanding amount is retained and visible. |
| FR-CLT-006 | **Collector settlement** is a distinct workflow: collector collects → records → system computes expected → collector submits settlement → treasurer/group admin verifies → settlement confirmed. |
| FR-CLT-007 | The system computes the expected settlement total and compares it to the submitted amount. |
| FR-CLT-008 | **Any difference is flagged and requires a written explanation.** A variance is never silently absorbed. |
| FR-CLT-009 | Settlement history is retained per collector and per scheme. |
| FR-CLT-010 | Settlement records are audit-logged. |
| FR-CLT-011 | A recorded collection is **never silently altered** — corrections are made by reversal and re-entry. |
| FR-CLT-012 | Collections recorded offline reconcile via idempotency keys on sync, and duplicate submissions are detected and rejected. |

### 4.8 Contribution records (§11)

| ID | Requirement |
|---|---|
| FR-CON-001 | Every contribution records: unique ID, member, group, scheme, amount, expected amount, actual amount, payment method, status, reference, collector (if applicable), date, time, created by, and verified status. |
| FR-CON-002 | Statuses are: PENDING, PROCESSING, COMPLETED, FAILED, REVERSED, CANCELLED. |
| FR-CON-003 | Duplicate contribution records are prevented via unique transaction references and idempotency keys. |
| FR-CON-004 | A contribution cannot be hard-deleted once it has reached a completed status. |
| FR-CON-005 | Every contribution produces a receipt reference the member can retrieve. |
| FR-CON-006 | Contribution history is queryable by member, scheme, group, collector, date range, and status. |

### 4.9 Ledger (§12, §13)

The ledger is the financial heart of the platform.

| ID | Requirement |
|---|---|
| FR-LED-001 | Every movement of money produces a ledger entry. Entry types include: CONTRIBUTION, LOAN_DISBURSEMENT, LOAN_REPAYMENT, GROUP_DISBURSEMENT, REFUND, FEE, REVERSAL, ADJUSTMENT. |
| FR-LED-002 | Every ledger entry is traceable to: user, group, scheme, source, reference, date, amount, status. |
| FR-LED-003 | A completed financial transaction is never deleted. Corrections are posted as reversals or correcting transactions that reference the original entry. |
| FR-LED-004 | Financial history is preserved permanently. |
| FR-LED-005 | Balances — member, scheme, group, and platform totals — are **derived from the ledger**, which is the sole source of truth. |
| FR-LED-006 | Balance fields, where they exist for performance, are derived caches that can be recomputed from the ledger at any time, and are protected against direct unauthorised modification. |
| FR-LED-007 | The system can produce: opening balance, contributions, other credits, disbursements, refunds, other debits, and current balance, and these must reconcile. |
| FR-LED-008 | Financial operations run inside database transactions, with row-level locking where concurrent operations could otherwise produce an inconsistent balance. |
| FR-LED-009 | The system prevents double payment, double disbursement, double loan repayment, duplicate webhook processing, and negative balances where prohibited. |
| FR-LED-010 | The system can prove its own integrity: for any account, the sum of ledger entries equals the derived balance. |

### 4.10 Borrowing (§14–§17)

| ID | Requirement |
|---|---|
| FR-LOAN-001 | A member has a borrowing area: eligibility, apply, my loans, guarantors, repayments, loan history. |
| FR-LOAN-002 | A loan application records: amount requested, purpose, duration, repayment frequency, proposed repayment amount, guarantors, supporting documents where required, and application date. |
| FR-LOAN-003 | Loan statuses: DRAFT, SUBMITTED, UNDER_REVIEW, GUARANTOR_PENDING, APPROVED, REJECTED, DISBURSEMENT_PENDING, DISBURSED, ACTIVE, OVERDUE, COMPLETED, DEFAULTED, CANCELLED. |
| FR-LOAN-004 | **Loan eligibility is determined by a configurable engine**, not a hard-coded universal rule. |
| FR-LOAN-005 | Eligibility factors available to the engine include: membership duration, contribution history, contribution consistency, total contributions, existing loans, outstanding debt, repayment history, group rules, maximum loan amount, and guarantor requirements. |
| FR-LOAN-006 | **Each group configures its own lending rules within platform-imposed limits.** |
| FR-LOAN-007 | A loan may require one or more guarantors, per group configuration. |
| FR-LOAN-008 | Guarantor workflow: member applies → guarantor selected → guarantor notified → guarantor reviews → approve/reject → loan proceeds to review. |
| FR-LOAN-009 | The guarantor decision is recorded with guarantor, loan, decision, date, time, IP/device where appropriate, and comments. |
| FR-LOAN-010 | **A guarantor cannot approve a request outside their permission scope.** |
| FR-LOAN-011 | Repayment supports daily, weekly, monthly, and other configured schedules. |
| FR-LOAN-012 | The system tracks: principal, interest/service charge, fees, amount repaid, outstanding balance, next payment, due date, late payment, and penalty where permitted. |
| FR-LOAN-013 | The financial and legal treatment of interest, fees, and penalties is **configurable** and must comply with applicable Nigerian regulations. |
| FR-LOAN-014 | Overdue loans are identified and managed, with the transition to OVERDUE and DEFAULTED following group-configured rules. |
| FR-LOAN-015 | Full loan history is retained per member and per group. |

### 4.11 Disbursements (§18)

| ID | Requirement |
|---|---|
| FR-DIS-001 | Disbursement is modelled separately from contribution collection. |
| FR-DIS-002 | Disbursement types include: monthly group payout, loan disbursement, welfare payment, emergency assistance, refund, approved group expense, and other authorised disbursement. |
| FR-DIS-003 | Workflow: REQUEST → REVIEW → APPROVAL → PAYMENT PROCESSING → VERIFICATION → COMPLETED. |
| FR-DIS-004 | **Separation of duties is enforced: the person requesting a disbursement cannot approve their own request.** |
| FR-DIS-005 | Approval authority is permission-scoped and configurable. |
| FR-DIS-006 | Every disbursement step is audit-logged with actor and timestamp. |
| FR-DIS-007 | A disbursement that fails at payment processing is recoverable without creating a duplicate. |

### 4.12 Payments (§19)

| ID | Requirement |
|---|---|
| FR-PAY-001 | Payments integrate established providers through a **dedicated payment abstraction layer**. No payment gateway is built from scratch. |
| FR-PAY-002 | Flow: application → payment service → provider → webhook → verification → ledger → receipt. |
| FR-PAY-003 | **A payment is never treated as successful because the client says so.** Success is established by server-side verification with the provider. |
| FR-PAY-004 | Webhooks are authenticated where the provider supports it, idempotent, logged, validated, and safely retried. |
| FR-PAY-005 | Duplicate webhook delivery does not produce duplicate ledger entries. |
| FR-PAY-006 | Payment attempts are recorded separately from confirmed payments, so failure modes are diagnosable. |
| FR-PAY-007 | Every successful payment produces a receipt reference. |

### 4.13 Communication (§20–§23)

| ID | Requirement |
|---|---|
| FR-COM-001 | A dedicated communication system supports: messages, bulk email, SMS, push notifications, in-app notifications, and templates. |
| FR-COM-002 | Bulk email recipients can be selected by: all platform users, specific group, specific scheme, collectors, treasurers, group administrators, members with outstanding contributions, members with active loans, members with overdue loans, or custom selection. |
| FR-COM-003 | **Bulk email is processed asynchronously** and never inside a normal HTTP request. |
| FR-COM-004 | Campaigns track total recipients, sent, delivered (where supported), failed, bounced (where supported), and timestamps. |
| FR-COM-005 | Consent, privacy, and unsubscribe requirements are respected. |
| FR-COM-006 | Messages support platform announcements, group announcements, scheme announcements, collector-to-member communication, administrative messages, loan notifications, and contribution reminders. |
| FR-COM-007 | **A group administrator cannot message users outside their authorised groups.** |
| FR-COM-008 | Automatic notifications are triggered for: contribution due, contribution received, payment failed, loan application submitted, guarantor request, loan approved, loan rejected, loan repayment due, loan overdue, disbursement approved, group invitation, and new announcement. |
| FR-COM-009 | Notification delivery is a background concern — notification triggers never block the request that caused them. |

### 4.14 Reporting (§24)

| ID | Requirement |
|---|---|
| FR-RPT-001 | Contribution reports: expected, collected, outstanding, missed contributions, collection performance. |
| FR-RPT-002 | Member reports: total contributions, contribution history, outstanding amounts, loan history, repayment history. |
| FR-RPT-003 | Group reports: members, total contributions, total disbursements, loans, repayments, balance, outstanding contributions. |
| FR-RPT-004 | Collector reports: expected collection, actual collection, outstanding, settlement history, variances. |
| FR-RPT-005 | Reports respect the requester's permissions — a report never exposes data from a group the requester has no standing in. |
| FR-RPT-006 | Expensive report generation runs in the background. |

### 4.15 Audit (§25)

| ID | Requirement |
|---|---|
| FR-AUD-001 | Every sensitive action is auditable, recording: who, what, when, which object, previous value, new value, IP address where appropriate, and device/session where appropriate. |
| FR-AUD-002 | Auditable actions include (non-exhaustive): admin approved loan, treasurer approved disbursement, collector recorded contribution, user changed phone number, admin changed group role. |
| FR-AUD-003 | **Audit records are not casually editable or deletable.** There is no application path that mutates or removes an audit record. |
| FR-AUD-004 | Audit records are retained for the period required by applicable regulation. |

### 4.16 Clients (§30–§35)

| ID | Requirement |
|---|---|
| FR-APP-001 | The platform is mobile-first. |
| FR-APP-002 | The mobile client is cross-platform (Flutter or React Native). The backend remains Python/Django. |
| FR-APP-003 | The mobile application presents role-based interfaces: member, collector, group administrator. |
| FR-APP-004 | Member app navigation: Home, Groups, Contributions, Borrowing, Transactions, Notifications, Profile. |
| FR-APP-005 | Member dashboard shows: total contributions, current schemes, today's due amount, outstanding contribution, active loan, next loan repayment, recent transactions. |
| FR-APP-006 | Collector app dashboard shows: today's expected, collected, outstanding, members, collection history, settlement. |
| FR-APP-007 | The collector interface is optimised for speed, poor network conditions, mobile screens, and simple data entry. |
| FR-APP-008 | Group admin can manage: members, roles, schemes, contributions, collectors, loans, disbursements, reports, communications, and group settings. |
| FR-APP-009 | A platform admin web dashboard covers: dashboard, users, groups, locations, schemes, contributions, payments, loans, disbursements, communications, reports, audit logs, security, and system settings. |
| FR-APP-010 | **The platform Super Admin is protected from ordinary deletion.** |
| FR-APP-011 | The mobile app caches safe read-only data for offline viewing. |
| FR-APP-012 | The mobile app queues permitted collection actions while offline and synchronises on reconnect. |
| FR-APP-013 | The mobile app detects duplicate submissions and shows synchronisation status. |
| FR-APP-014 | **The mobile app never silently loses a financial record.** |
| FR-APP-015 | Offline financial operations use unique IDs and idempotency keys, with strong safeguards against duplicate records and conflicting data. |

---

## 5. Non-functional requirements

| ID | Requirement |
|---|---|
| NFR-SCALE-001 | The platform is designed so that millions of users can eventually use it, while the first MVP remains simple enough to develop, test, deploy, and operate reliably. |
| NFR-SCALE-002 | The initial deployment is a modular monolith, with boundaries drawn so that services can be extracted later without redesign. |
| NFR-SCALE-003 | Load testing precedes any claim about user capacity. In the spec's own words: capacity is not claimed until load testing demonstrates acceptable performance. |
| NFR-SEC-001 | Secure password hashing; JWT or secure session authentication as appropriate; refresh-token strategy where JWT is used; MFA/2FA. |
| NFR-SEC-002 | Rate limiting; account lockout and risk controls; CSRF protection where applicable. |
| NFR-SEC-003 | Input validation; output validation; SQL injection protection; XSS protection; secure headers. |
| NFR-SEC-004 | Encryption in transit; encryption for sensitive stored information where appropriate; secrets management. |
| NFR-SEC-005 | Permission checks on every access; audit logs; idempotency; transaction locking where necessary. |
| NFR-SEC-006 | **Sensitive information is never exposed through APIs.** |
| NFR-DATA-001 | PostgreSQL. Schema normalised and carefully indexed. |
| NFR-DATA-002 | Financial data is backed up, and backup restoration is tested. |
| NFR-OPS-001 | Git, GitHub/GitLab, Docker, Docker Compose for development, CI/CD, automated testing, environment variables, secrets management. |
| NFR-OPS-002 | Cloud deployment with monitoring, centralized logging, and automated backups. |
| NFR-OPS-003 | Environments: development, testing, staging, production. **Never develop directly against production.** |
| NFR-QUAL-001 | PEP 8, type hints, meaningful names, small focused modules, reusable services, clear separation of concerns, proper exception handling, logging, automated tests, documentation. |
| NFR-QUAL-002 | Avoid: giant files, giant functions, repeated code, hard-coded business rules, hard-coded secrets, business logic inside templates, business logic scattered across API views. |

---

## 6. Explicitly out of scope for the MVP

Recorded so that scope creep is visible and deliberate:

- Microservice extraction. The monolith is correct until scale demonstrably requires otherwise.
- Building a payment gateway. Licensed providers are used.
- Cross-border money movement.
- Secondary market for loan positions.
- Interest-bearing member savings accounts as a banking product.
- Automated credit scoring beyond the configured eligibility engine.
- Native (non-cross-platform) mobile clients.

---

## 7. Open questions

Per §52, these are **deliberately unresolved** rather than silently invented. Each
materially affects money, security, permissions, or legal compliance, and each is
resolved with the user before the dependent document is finalised.

Resolved questions stay in the list with their answer. **Retire an ID; never renumber
one** — the numbers are cited across eight documents, and a gap is cheaper than a
renumbered reference.

**Q1 — Custody of funds (highest priority; blocks 06, 08, 12, 14).**
Does the platform ever take custody of member funds, or does money always move
directly between member, collector, and a group-held account at a licensed
institution? This is the single largest determinant of the platform's regulatory
posture. If the platform holds funds, it is likely performing a regulated activity
and needs licensing or a licensed partner. **Working assumption pending your answer:
the platform is a system of record and does not take custody.** All of Phase 0 is
written on that assumption, and it is flagged here because reversing it later would
be expensive.

**Q2 — Interest model (blocks 07).**
Flat interest, reducing balance, or configurable per scheme? The spec requires the
treatment to be configurable and compliant with Nigerian regulation, but does not
choose a default. A default must be chosen for the MVP, and Nigerian lending rules
and disclosure expectations need a legal read before it is fixed.

**Q3 — Collector liability (blocks 06, 08).**
When a collector's settlement falls short, who carries the loss — the collector, the
group, or the platform? The spec requires the variance be flagged and explained, but
is silent on who bears it. This is a business rule with real legal weight and it is
**not** being invented here.

**Q4 — Late fees (blocks 06).**
Does a late fee accrue to the group, the scheme pool, or the platform? And is it
charged on contributions, loan repayments, or both?

**Q5 — KYC tiering (blocks 10, 14).**
What identity verification is required at what balance or transaction threshold?
Nigeria's KYC tiers are tiered by transaction limits; the thresholds need to be
confirmed against current CBN requirements rather than assumed.

**Q6 — Group legal personality (blocks 14).**
Are groups registered entities (e.g. registered cooperatives) or informal
associations? Registered cooperatives carry reporting obligations that informal
groups do not, and the platform may need to distinguish them.

**Q7 — Money representation (blocks 03, 06).**
**Proposed, not yet ratified:** all monetary values are stored and computed as
**integer minor units** (kobo for NGN), never as floating point. Floats cannot
represent decimal money exactly and their use in a financial ledger is a defect, not
a preference. This is a technical correctness decision rather than a business rule,
but it is recorded here because it constrains every financial model in 03.

**Q8 — Multi-currency scope (blocks 03, 06).**
The schema is generic and multi-country. Is the MVP single-currency, with
multi-currency deferred until a second country onboards? **Working assumption: yes.**
Note this is a question about *phasing*, not about the model: currencies are a table of
their own (03 §4.5.2) and carry their own minor-unit exponent and rounding mode
(06 §3.4), so onboarding a second country is rows rather than a migration.

**Q9 — Double-entry ledger (blocks 03, ERD).**
**Resolved: yes.** Recorded here for traceability.

**Q10 — Scope of "not only Nigeria": fiat only, or digital assets too? (blocks 03, 06).**
The platform is designed multi-country and multi-currency. Whether that extends to
non-fiat units — crypto-assets, tokenised deposits — is **not decided**, and it is not a
configuration question: integer minor units fit fiat exponents 0–4 comfortably and an
18-decimal asset fits about nine whole units in a `BIGINT` (06 §3.4.5). **Working
assumption: fiat only.** Supporting digital assets later needs a different
representation, not a wider column, so the answer is worth having before Phase 3 rather
than after.

---

## 8. Glossary

| Term | Meaning |
|---|---|
| **Group** | A collection of members who contribute together under one or more schemes. Created at a street; not equivalent to a street. |
| **Scheme** | A contribution arrangement within a group: amount, frequency, and disbursement rules. |
| **Collector** | Field agent who collects contributions, typically cash, from members. |
| **Settlement** | The reconciliation event where a collector hands over collected funds and accounts for any variance. |
| **Variance** | The difference between the amount a collector was expected to collect and the amount they submit. Always requires explanation; never silently absorbed. |
| **Contribution** | A single payment by a member into a scheme. |
| **Disbursement** | Any outflow of money from a group or scheme — a payout, loan, welfare payment, or expense. Separate from collection. |
| **Ledger entry** | An immutable record of a movement of money. The source of truth for all balances. |
| **Reversal** | A correcting ledger entry that offsets a prior entry without deleting it. |
| **Idempotency key** | A client-supplied unique key that makes a retried financial operation safe to repeat. |
| **Guarantor** | A member who accepts liability for another member's loan. |
| **Eligibility engine** | The configurable ruleset determining whether and how much a member may borrow. |
