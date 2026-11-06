# matthy.app

A community contribution, savings, borrowing & disbursement platform — for
contribution groups, daily collectors, cooperatives, associations, and savings groups.

**Backend:** Python 3.12+ · Django · Django REST Framework · PostgreSQL · Redis ·
Celery · Docker

## Status

**Resume work here:** [Project handover — completed work, remaining tasks and next
session checklist](docs/PROJECT_HANDOVER.md) (updated 2026-11-06).

**Phase 0 — Discovery: complete and accepted.** All fourteen design documents are
drafted and reviewed; [`docs/`](docs/README.md) tracks their status, the decisions
resolved, and the open questions still awaiting answers. Also settled: 102 required tests
([13 §8](docs/13_TESTING_STRATEGY.md)), ten blocking CI gates
([11 §6](docs/11_TEAM_WORKFLOW.md)), and the exact double-entry posting rules for every
financial event ([06 §4.13](docs/06_FINANCIAL_RULES.md)).

**Phase 1 — Foundation: in progress.** The plan is
[`docs/PHASE_1_PLAN.md`](docs/PHASE_1_PLAN.md), which closes the four §46 gate conditions
and states the slice order (S0 scaffold → S1 `accounts` → S2 `audit` → S3 `locations` →
S4 auth API → S5 `groups`).

The foundation includes accounts, audit, locations, groups and authentication:
registration, phone and email verification, login, token refresh, logout, password recovery
and changes, transaction PIN setup/changes, MFA enrollment/challenges and recovery
codes, session management, device management and browser admin MFA with staff enrollment.
[`docs/AUTHENTICATION.md`](docs/AUTHENTICATION.md) documents setup, token rotation,
and the remaining authentication work. [Groups and permissions](docs/GROUPS.md)
covers group setup, invitations, membership, bounded role configuration, platform
administration and role-triggered MFA/session revocation. Local verification uses
real PostgreSQL 16 and Redis. All ten gates also passed on GitHub after the S4/S5 merge;
`main` and `develop` now have active branch protection. Staging deployment, production
delivery and the remaining product work are recorded in the handover.

[Staging preparation](docs/STAGING.md) records the container configuration,
required hosting inputs and deployment/walkthrough procedure.

The product brand is **IHWEZA** — *Together We Move.* The repository, Python package and
Django project name remain `matthy.app` / `config`.

The project brief is a 41-page specification held outside this repository.

## Development phases (§46)

| Phase | Scope |
|---|---|
| 0 — Discovery | Requirements, architecture, ERD, API spec, security model, permission matrix |
| 1 — Foundation | Django project, PostgreSQL, custom User, authentication, roles, locations, groups, Docker, testing framework, API docs |
| 2 — Contributions | Schemes (daily → 12-month), members, collectors, schedules, contributions, receipts, basic reports |
| 3 — Financial Core | Payments, ledger, reconciliation, reversals, audit logs, balance calculation |
| 4 — Borrowing | Eligibility, applications, guarantors, approval, disbursement, repayments, history, overdue |
| 5 — Communication | Messages, bulk email, SMS, push, templates, notification engine, history |
| 6 — Mobile | Member app, collector app, group administration, notifications, offline handling |
| 7 — Production Hardening | Security testing, load testing, backups, disaster recovery, monitoring, logging, performance, fraud controls |

## Non-negotiable rules (§44)

1. Never trust the frontend with financial calculations.
2. Never store plain-text passwords.
3. Never delete completed financial transactions.
4. Never process a payment solely because the client says it succeeded.
5. Never allow unauthorized role escalation.
6. Never allow a user to approve their own restricted financial transaction.
7. Prevent duplicate payment processing.
8. Record every important financial action.
9. Use database transactions for financial operations.
10. Preserve a complete audit trail.
11. Verify payment webhooks.
12. Use idempotency keys.
13. Protect sensitive personal information.
14. Back up financial data.
15. Test restoration of backups.
