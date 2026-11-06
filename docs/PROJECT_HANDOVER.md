# Project handover — 2026-11-06

Read this first when resuming work. It records the completed milestone, verified
GitHub configuration, unfinished work, and the next starting point. Check live Git
and GitHub state before acting; the commit below is the implementation baseline,
not a promise that branch heads will never advance.

## Where we are

IHWEZA (`matthy.app`) has a tested backend foundation: accounts, audit, locations,
the implemented authentication flows, and groups/permissions. The S4/S5 change set
was committed, pushed, reviewed through a pull request, and merged into `main`.
CI passed on the merged commit. Branch protection is now active.

Phase 0 design is accepted. Phase 1 has delivered this backend milestone, while
remaining authentication, deployment and operational work is still open. The full
member product and financial modules remain to be built. No staging or production
deployment was completed or verified during this session.

## Saved implementation and evidence

| Item | Saved state |
|---|---|
| Repository | https://github.com/maihwe/matthy.app |
| Implementation PR | [PR #2 — authentication flows and scoped group permissions](https://github.com/maihwe/matthy.app/pull/2), merged |
| Implementation baseline | [`733691a730e61dce84aa64ff6640bfa83f373293`](https://github.com/maihwe/matthy.app/commit/733691a730e61dce84aa64ff6640bfa83f373293) on `main` |
| Original feature branch | `feature/s4-auth-s5-groups`; original work `b6b5b5a`, CI configuration fix `42e5c13` |
| Passing merged-commit CI | [Run 42273688659](https://github.com/maihwe/matthy.app/actions/runs/42273688659), all eight jobs covering all ten gates passed |
| Integration branch | `develop` created from the same passing baseline; subsequent features go through PRs into `develop`, then a release PR into `main` |

Local verification passed all ten gates using PostgreSQL 16 and Redis. The full
suite passed 1,034 tests; the final focused suite passed 120, including three new
regressions, for 1,037 distinct tests verified. Accounts/groups combined line/branch
coverage was 95.90%; other line coverage was 96.01%. Dependency audit found no known
vulnerabilities, and OpenAPI generation/validation had no warnings or errors.

Hosted gitleaks initially misidentified the literal Django predicate
`consumed_at__isnull=True` in two queries. `.gitleaks.toml` retains the default rules
and allows only that exact captured predicate. Matching gitleaks 8.24.3 scans passed
for the feature commit and all eight then-existing local history commits; a
synthetic API key remained detectable. The corrected hosted scan passed too.

## What is implemented

| Slice | Completed behavior | Detailed record |
|---|---|---|
| S0 infrastructure | Django/DRF project, Docker/Compose setup, configuration, CI, test framework and OpenAPI contract checks | [Phase 1 plan](PHASE_1_PLAN.md) |
| S1 accounts | User/profile/device/session/OTP/token models and services, account-state checks and migrations | [Authentication](AUTHENTICATION.md), [database design](03_DATABASE_DESIGN.md) |
| S2 audit | Business-audit write path and immutable audit records/database guards | [Phase 1 plan](PHASE_1_PLAN.md), [database design](03_DATABASE_DESIGN.md) |
| S3 locations | Currency/country/location catalogue, hierarchical locations, seeds and permission-guarded administration | [API specification](04_API_SPECIFICATION.md) |
| S4 implemented authentication slices | Registration, phone/email verification, login, refresh rotation/reuse detection, logout, password recovery/change, PIN setup/change, TOTP/SMS MFA interfaces, recovery codes, bearer/browser session and device management, browser admin MFA and CSRF | [Authentication](AUTHENTICATION.md) |
| S5 groups/permissions | Group types and frozen role catalogue, group creation, invitations, membership/history, bounded custom roles/grants, ownership transfer, explicit platform assignments, `has_perm`, scoped read-only admin, role-triggered MFA/session revocation and cache invalidation | [Groups](GROUPS.md), [role matrix](05_ROLE_PERMISSION_MATRIX.md) |

Group permission checks and writes are tested for cross-group denial, auditor write
denial, invitation consent/replay, role delegation bounds, owner continuity,
concurrent revocation/transfer/bootstrap, audit rollback and immutable history.
Browser/mobile proof isolation, MFA replay, populated migration compatibility,
CSRF, pagination and rate limits are also covered.

Development/test code delivery uses Mailhog/in-memory capture. SMS MFA and
verification interfaces exist; actual SMS/email provider integration is unfinished.
Group invitation responses return one-time tokens to the caller; this slice does
not deliver invitation messages. Existing templates cover admin authentication,
not a member-facing application.

## GitHub safeguards now active

| Ruleset | Active configuration |
|---|---|
| [Main branch protection — 26717900](https://github.com/maihwe/matthy.app/rules/26717900) | PR required; all eight GitHub Actions checks required; branch must be up to date; review discussions resolved; stale reviews dismissed; deletion/force pushes blocked; linear history; squash/rebase merges only; no bypass actors |
| [Develop branch protection — 26717994](https://github.com/maihwe/matthy.app/rules/26717994) | Same PR/check/update/discussion/deletion/force-push requirements; merge/squash/rebase allowed; no bypass actors |
| [Owner administration of protected branches — 26718024](https://github.com/maihwe/matthy.app/rules/26718024) | Separate update restriction for both branches, bypassable only by repository administrators; it does not bypass the primary PR/CI rules |

Required approving reviews are **0** for the documented solo workflow. Code-owner,
last-push and extra unattributed-change approvals are disabled. CI checks are bound
to GitHub Actions app ID 15368. `maihwe` was the only collaborator and repository
administrator when verified; additional administrators would share the update
permission and require a policy review. GitHub returned `protected: true` for both
branches. See [Branch Protection](BRANCH_PROTECTION.md) for verification commands.

## Remaining work

### Next milestone: staging and a foundation walkthrough

Staging preparation has now started. [STAGING.md](STAGING.md) records the container
fixes, environment checklist and release procedure. GitHub's `staging` environment
was created with protected-branch deployment restrictions. Local probe regressions
(16 cases) and build-only static collection passed; image/runtime validation is
provided by the new Container workflow. Hosting target, domain, deployment access,
real delivery adapters and deployed walkthrough results remain pending. No hosted
staging URL or live deployment is recorded yet.

AWS was selected as the hosting direction after the staging container work.
[AWS_STAGING.md](AWS_STAGING.md) records the CloudFormation templates, temporary
GitHub OIDC credentials, explicit migration/release workflow, internal ALB probe
and restart procedure. The workspace has no AWS credentials/profile or account
connection. Account ID/access, region, domain/certificate and budget remain needed;
no AWS resources or paid services have been created. The first AWS stack deploys
web only; worker/beat services remain to be added when background tasks exist.
PR #5 container preparation is merged into `develop` at
[`c018875`](https://github.com/maihwe/matthy.app/commit/c018875064b2b8d715027e2419ce3e97b73485bf);
its [CI](https://github.com/maihwe/matthy.app/actions/runs/42292627376) and
[actual Docker/runtime check](https://github.com/maihwe/matthy.app/actions/runs/42292627478)
both passed. AWS configuration is a separate preparation change, not a hosted deployment.
Local AWS validation passed templates/workflows, lint/types, 20 AWS cases and 16
existing probe cases. AWS API/hosting validation is pending account access. The
manual AWS workflow must also exist on default `main` before dispatching `develop`;
promote the prepared configuration through a release PR after its CI passes.

This is the agreed next starting point, not a deployment already performed.

- Select/confirm the staging hosting account, domain, PostgreSQL 16 and Redis
  services, and deployment/registry approach. Staging settings exist, but hosted
  infrastructure, domain, secrets and deployment pipeline were not verified.
- Provision distinct staging secrets through the host's secret store: Django
  configuration, database/Redis credentials, matching RSA `JWT_SIGNING_KEY` and
  `JWT_VERIFYING_KEY`, and `MFA_ENCRYPTION_KEYS`. Never commit or place secrets in
  this handover.
- Select and implement a real SMS/email delivery backend and verified sender
  configuration. `PHONE_VERIFICATION_BACKEND` must be available; local capture is
  for development/tests. Include email-link and invitation delivery/handling.
- Review migration timing/locks against realistic data, back up the staging
  database, and apply migrations explicitly before routing traffic to new code.
  Legacy family backfills group sessions by user/device; older rows cannot
  distinguish independent login families. Data backfill/seed reversals are no-ops;
  rollback is not a lossless data-recovery strategy.
- Deploy a version identified by a commit SHA, verify health/configuration, and
  walk through the flows below using synthetic accounts. Record the staging URL,
  deployed commit, results, and outstanding failures here.
- Verify environment protections, secret separation, logs/alerts and a restore
  rehearsal. The GitHub `production` environment/reviewer configuration was not
  verified or created during branch-protection setup.

Foundation walkthrough acceptance cases:

1. Register a synthetic account; verify phone/email; log in, refresh and log out.
2. Enroll MFA and exercise browser admin sign-in and bearer sign-in separately.
3. Create a group; sign in again after role bootstrap revokes old sessions, and
   complete privileged-role MFA enrollment/challenge when required.
4. Invite another synthetic account, accept, and approve membership when configured.
5. Check cross-group access denial, inactive membership denial and auditor write
   denial; perform permitted role changes and an ownership transfer.
6. Revoke a role/session/device and confirm existing bearer/browser access stops;
   confirm redeemed invitation tokens cannot be replayed and tokens cannot be used
   by another account.

### Unfinished authentication and operational controls

- Forgotten-PIN recovery.
- Sensitive profile changes, including adding/changing email after registration,
  and the required re-verification/session-invalidation behavior.
- Application encryption of other sensitive profile fields; TOTP seed encryption
  and key rotation are already implemented.
- Security-event shipping to storage the application cannot rewrite, centralized
  logging, monitoring and alerting. Events emitted locally do not establish
  production detection or alert delivery.
- Reconcile remaining security decisions C2–C5, C9 and C11 in
  [PHASE_1_PLAN.md](PHASE_1_PLAN.md) against current code: erasure/anonymization and
  immutable audit retention, source-IP lockout requirements, new-device/risk
  step-up notifications, trusted proxy topology and possible travel signals.
  Existing per-IP throttling and privileged-role MFA are implemented; those do not
  establish completion of every risk-based authentication requirement.
- Real delivery, a member frontend email-link handler, background-delivery design
  and invitation-message integration.

The older planning tables and toolchain/blocker findings in `PHASE_1_PLAN.md` are
historical snapshots. Use its dated implementation update, this handover, and the
current authentication/groups guides to distinguish resolved items from open ones.

### Product phases still ahead

| Phase | Remaining product work |
|---|---|
| 2 — Contributions | Schemes, member/collector flows, schedules, contributions, receipts and basic reports |
| 3 — Financial core | Payments, double-entry ledger, reconciliation, reversals, balances, financial audit and idempotent posting |
| 4 — Borrowing | Eligibility, applications, guarantors, approval, disbursement, repayments and overdue handling |
| 5 — Communication | Notification engine, messages, templates, bulk email/SMS/push and delivery history |
| 6 — Mobile/member interfaces | Member app, collector app, group administration, notifications and offline behavior |
| 7 — Production hardening | Security/load testing, backups/restores, disaster recovery, observability, performance and fraud controls |

Member-facing screens are not built. Financial/ledger/payment/loan/disbursement
modules are not built; no money moves in this milestone. API support is not a
finished member experience. Cross-cutting staging/delivery work may precede its
full product-phase implementation.

Legal review and outstanding product decisions remain recorded in
[docs/README.md](README.md): fund custody before Phase 3, KYC tiers/thresholds,
group legal status and MVP currency scope. Non-fiat units are unsupported under
the current arithmetic design. Do not infer these decisions from working tests.

## Start the next session here

1. Read this document, then [AUTHENTICATION.md](AUTHENTICATION.md),
   [GROUPS.md](GROUPS.md), [BRANCH_PROTECTION.md](BRANCH_PROTECTION.md) and
   [12_DEPLOYMENT_PLAN.md](12_DEPLOYMENT_PLAN.md) for the chosen next task.
2. Check `git status --short --branch`, GitHub authentication and the latest remote
   branches/PRs. Preserve any new local changes; fetch before creating a branch.
   Temporary CLI/authentication setup from this session may not survive a restart.
3. Create a focused branch from current `develop`. Save work through a PR to
   `develop` with the documented self-review and passing checks; promote to `main`
   through a release PR. Direct pushes, force pushes and branch deletion are blocked
   on the protected branches, including for the current administrator.
4. Continue from [AWS_STAGING.md](AWS_STAGING.md): obtain AWS account access, region,
   hostname and budget; estimate/review the proposed resources; then follow registry,
   secret, infrastructure, database-initialization and release ordering. If AWS
   access is still absent, finish independent preparation and report the missing input.
5. After meaningful progress, update this handover with deployed/saved commit links,
   verification results and remaining items. Do not rerun the long local suite just
   to recover context or push unchanged code; run checks appropriate to new work and
   satisfy the hosted CI gates required for merging.

Suggested restart request: **“Read docs/PROJECT_HANDOVER.md and continue with staging
readiness and the registration/login/group walkthrough.”**
