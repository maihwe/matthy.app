# Phase 1 — Foundation: Implementation Plan

**Status:** Approved for execution (Phase 0 reviewed and accepted, 2026-09-16)
**Derives from:** [01](01_PRODUCT_REQUIREMENTS.md), [02](02_SYSTEM_ARCHITECTURE.md),
[03](03_DATABASE_DESIGN.md), [04](04_API_SPECIFICATION.md), [05](05_ROLE_PERMISSION_MATRIX.md),
[10](10_SECURITY_REQUIREMENTS.md), [11](11_TEAM_WORKFLOW.md), [12](12_DEPLOYMENT_PLAN.md),
[13](13_TESTING_STRATEGY.md)

---

## 1. The §46 gate, closed

[docs/README](README.md) lists four conditions before Phase 1 begins. Their state:

| # | Condition | State |
|---|---|---|
| 1 | Review the Phase 0 documents | **Done** — reviewed and accepted 2026-09-16 |
| 2 | Confirm the Phase 1 scope boundary | **Done** — §2 below is that boundary |
| 3 | Confirm the open questions have acceptable working assumptions | **Done** — proceeding on the recorded assumptions (Q1 `SYSTEM_OF_RECORD`, Q5 unconstrained `kyc_level`, Q6 unmodelled, Q8 single-currency NGN, Q10 fiat only) |
| 4 | Confirm the §45 legal review is engaged | **Open, and does not block Phase 1.** It gates launch and Phase 3, and its answers reach back into the schema — Q1 before Phase 3 at the latest |

Condition 4 is carried forward rather than closed, because it is genuinely not ours to
close. Nothing in Phase 1 depends on its answer: the custody flexibility is already
confined to one `CHECK` (03 §2.6), currency is a table (03 §4.5.2), and KYC level is a
plain column (03 §4.2). That is what those decisions bought.

## 2. Scope

Phase 1 delivers the foundation every later module stands on. 01's out-of-scope list
applies in full; within scope:

| Area | Contents |
|---|---|
| Project | Repository layout per 02 §4, `config.settings` package, `manage.py`, CI gates per 11 §6 |
| Database | PostgreSQL 16, `citext`/`pg_trgm` extensions, the migration discipline of 03 §16 |
| `accounts` (L0) | Custom `User`, `user_profiles`, `devices`, `user_sessions`, `otp_codes`, `verification_tokens` |
| `locations` (L0) | `currencies`, `countries`, `location_types`, `locations`; Nigeria seeds; `states`/`lgas`/… views |
| `audit` (L0) | `audit_logs` + immutability trigger |
| `groups` (L1) | `group_types`, `groups`, `group_members`, `group_member_history`, `group_roles`, `permissions`, `group_permissions`, `group_member_roles`, `group_invitations` |
| Authorisation | `has_perm(user, group, code)` — the single enforcement path (05 §8) |
| Auth API | The 21 endpoints of 04 §2 |
| Infra | Docker Compose (`web`, `worker`, `beat`, `postgres`, `redis`, `mailhog`), Celery + beat, OpenAPI schema |

**Explicitly not in Phase 1:** schemes, contributions, collectors, payments, ledger,
loans, disbursements, notifications, communications, reports. `ledger` in particular is
Phase 3 — no money moves in Phase 1, and no table in this phase holds a balance.

## 3. Slices

§47's rule is that a module is finished before the next begins. The slices below are the
module order, with the one dependency that forces it stated.

| # | Slice | Why here | Requirement IDs |
|---|---|---|---|
| **S0** | Repository skeleton, `config.settings`, Docker Compose, CI gates, `scripts/` | 11 §1.2: infrastructure lands first, because testing deferred for want of a harness is testing abandoned | NFR-OPS-001, NFR-QUAL-001, 11 §6 |
| **S1** | `accounts` — custom User, profiles, devices, sessions, OTP, tokens | **First project module, and it must be.** See P1-5 below | FR-AUTH-001…017, SEC-AUTH-*, SEC-OTP-* |
| **S2** | `audit` — `audit_logs`, the write path, and the immutability trigger | L0; `accounts` writes to it from its first service, and `locations` seeding writes to it too | FR-AUD-001…003, SEC-AUD-001…003 |
| **S3** | `locations` — currencies, countries, location types, tree, seeds | L0, and `groups` needs a `location_id` to sit at. Adds `user_profiles.location_id` by additive migration | FR-LOC-001…004 |
| **S4** | Authentication API — JWT, refresh rotation with reuse detection, PIN, MFA, rate limits | Consumes S1's models; this is where 10 §3's token rules become real | SEC-JWT-001…009, SEC-MFA-* |
| **S5** | `groups` + permissions — roles, catalogue, matrix seed, `has_perm` | Last, because it depends on all three L0 modules | FR-RBAC-001…007, FR-GRP-*, 05 §5 |

### P1-5 — why `accounts` is slice 1, not slice 3

Django cannot swap `AUTH_USER_MODEL` after `auth` and `admin` have been migrated.
`admin.LogEntry` holds a `ForeignKey` to the user model, so the first `migrate` against
the *default* `auth.User` fixes that reference permanently: pointing
`AUTH_USER_MODEL` at a custom model afterwards raises
`InconsistentMigrationHistory`, and the only cure is dropping the database.

The original order here was `locations` → `audit` → `accounts`. It is unbuildable as
written, and it would have failed at S1's first `migrate`, not at review.

The cost of the correction is small and one-directional:

* `accounts` lands with `AUTH_USER_MODEL = "accounts.User"` set in `config.settings.base`
  **before** the first `migrate` exists.
* `user_profiles.location_id` is a nullable FK added when `locations` lands (S3). This is
  an additive migration on an existing table, so it is safe and needs no data backfill —
  a profile may legitimately have no address yet.
* `accounts` therefore has no dependency on `locations`, and its own tests do not wait for
  it. Nothing in S1 needs a location to exist.

### P1-5a — why S1 splits into S1a and S1b

P1-5 puts `accounts` before `audit`, but `accounts`' first *service* writes an audit
record (02 §4.3: everything writes to `audit`). Read as one slice that is a cycle:
`accounts` models before `audit`, `audit` models before `accounts` services.

It is not a cycle once the slice is split at the right seam:

| Sub-slice | Contents | Why here |
|---|---|---|
| **S1a** | `accounts` models, constants, admin, and the first migration | Forced first by `AUTH_USER_MODEL`. Touches no other project module |
| **S2** | `audit` models, the immutability trigger, and `record()` — the write path | L0. Depends on nothing |
| **S1b** | `accounts` services — registration, OTP issue/verify, session issue and rotation, lockout | Depends on S2 for `record()`, but see C5: SEC-AUD-001 requires the events these services emit to be logged *distinct from* business audit, and S2 provides no such sink. So the dependency is real only for a business-audit write, and S1b has none of those yet — FR-AUD-002's five named actions are all S5+ |

The seam is real rather than a convenience: S1a is data definition with no behaviour, and
S1b is behaviour. §47's "finish a module before the next begins" is respected in the sense
that matters — no *half-written* module is being left behind, because S1a is complete as
data definition and S1b is a later, separately-reviewed addition.

Branding (IHWEZA) rides along with S0, so that the admin title, OpenAPI title, and email
identity are set in one place before anything renders them.

### Current implementation update (2026-11-06)

Accounts, audit and locations now have migrations and executable tests. The first
S4 slice implements login, refresh, logout, session lists/revocation and device
lists/revocation. See [AUTHENTICATION.md](AUTHENTICATION.md) for setup and remaining
scope. C6 now has explicit `family_id` and `revoked_reason` columns with a tested
legacy backfill. C7 has a configurable 120/minute per-IP refresh limit; login also
has a 50/15-minute per-IP limit across accounts. Other S4 flows are tracked below.

The next S4 slice adds registration, phone-code requests and phone verification.
Registration responses now follow S10's generic response rule. C8 has a documented
50/hour per-IP registration/resend limit. C10 uses the channel booleans as facts and
updates the verification summary atomically when phone ownership is proven. Local
code delivery is captured in Mailhog; production SMS remains unconfigured.

Password recovery now supports verified phone and email channels, single-use
expiring proofs and recovery lockout reset. Authenticated password changes and PIN
setup/changes revoke bearer and browser sessions. PIN audit records contain only
change metadata, never credential values or hashes. Production delivery remains
outside these completed slices.

MFA now supports confirmed TOTP and local-captured verified-phone SMS enrollment,
single-use recovery codes and password/device-bound login challenges. C1 is resolved
by `0004_mfa`: encrypted seeds, pending enrollment, recovery-code and challenge tables,
plus persistent session verification/version metadata. C2's TOTP key decision uses
a separate Fernet keyring from the secret store with atomic re-encryption via
`rotate_mfa_keys`; encryption of other sensitive fields remains pending. Staff and
superusers must enroll through a restricted password proof, and cannot disable MFA.
S5 now enforces privileged group/platform role MFA and revokes existing sessions
when role authority changes. Browser admin MFA provides password-then-code
sign-in and staff enrollment. `0005_browser_mfa` binds cookies to revocable WEB-device
session rows, with 30-minute idle and 12-hour absolute limits; browser rows and
challenges cannot authorize mobile bearer/refresh flows. Privileged password-only
cookies remain denied. All browser mutations require POST and CSRF.

Email verification now accepts optional email during registration and provides an
authenticated request for the saved address plus public single-use confirmation.
It uses the existing `verification_tokens` table with purpose/address/credential
binding, 24-hour expiry, resend invalidation and configurable account/destination/IP
limits. Concurrent email and phone confirmation preserve the combined summary;
verified email enables the completed recovery flow. Email collisions at registration
receive a generic response instead of an error or account mutation. Adding/changing
email after registration and production delivery remain pending.

All ten local CI gates passed on 2026-11-06, including 977 tests against PostgreSQL
16 and Redis, the coverage floors, dependency audit and OpenAPI contract check.
This was the pre-S5 verification baseline. S4 still has the authentication work
listed in AUTHENTICATION.md. S5 implementation and verification are recorded in
[GROUPS.md](GROUPS.md).

S5 is now locally complete: group types/templates, groups, membership/history,
invitations, bounded role configuration, platform assignments, ownership transfer,
the single resolver, scoped admin and permission-guarded location writes. All ten
CI gates passed with 1,034 full-suite tests, followed by 120 focused regression
tests including three additional cases (1,037 distinct tests verified). Final
security scope coverage is 95.90%; other code has 96.01% line coverage. Production
delivery, member screens and the remaining S4 flows are separate work.

The status table and unexecuted-toolchain findings below are the original planning
snapshot; current verification results are recorded in AUTHENTICATION.md and GROUPS.md.

### Slice status (planning snapshot, 2026-09-16)

| Slice | Written | Verified | Note |
|---|---|---|---|
| **S0** | yes | **no** | Skeleton, settings, API conventions, health checks, Docker, CI. No toolchain has run against it |
| **S1a** | models, constants, admin, factories, tests | **no** | **No migration yet**, so gate 5 fails. Generated by `makemigrations`, not hand-written |
| **S2** | models, constants, admin, write path, trigger migration, factories, tests | **no** | The trigger migration is hand-written (P1-12) and depends on `audit/0001_initial`, which does not exist yet — so migrations are currently unresolvable |
| **S1b** | services written; **the audit writes are not** | **no** | Written: `config/ratelimit.py` + `RATE_LIMITS`, `services/errors.py`, `services/phones.py`, `services/otp.py`, `services/sessions.py`, `services/lockout.py`, `services/passwords.py`, `services/registration.py`, `services/credentials.py`, and their unit and integration tests. Two things are still missing, and neither is a detail: **no `accounts` service writes a business-audit record** — but see C5, because the obvious fix is the wrong one: the events these services produce are SEC-AUD-001's authentication events and must *not* go through S2's `record()` into the financial table, so what is owed is a security sink (C5) and, separately, one business-audit write at the point S1b first has one (a phone-number change, blocked on C4); and **no HTTP exists**, so rate limiting, `Retry-After`, and the mapping from these domain errors to status codes are still unexercised. No toolchain has run |
| **S3**–**S5** | not started | — | — |

"Written but not verified" is the honest state of S0, S1a, S2 and S1b and is not the same
as "done". 11 §4's definition of done ends at the Tests and Documentation stages, and no
slice has reached them.

## 4. Definition of done, per slice

11 §4's nine stages, applied without exception:

```
Understand → Design → Database → Models → Serializers → Services
          → API → Tests → Documentation
```

The gate for each slice is 13's coverage floor for its scope (90% branch for `accounts/`
and `groups/`; 80% line elsewhere), plus the CI gates of 11 §6.

Beyond the module's own tests, Phase 1 carries these obligations from the register
(13 §8):

| Source | IDs in Phase 1 scope |
|---|---|
| 05 §10 | T1, T2, T3, T4, T7, T8, T9, T11, T12 |
| 10 §11 | S1, S2, S4, S5, S6, S7, S8, S9, S10, S12 |

The remaining obligations in both registers belong to modules that do not exist yet and
are tracked, not claimed.

## 5. Decisions taken during planning

| # | Decision | Rationale |
|---|---|---|
| P1-1 | **The product brand is IHWEZA** (`Together We Move.`); the repository, Python package, and Django project stay `matthy.app` / `config` | Branding is presentation, and renaming the package would churn every Phase 0 document for no structural gain. The brand is set once, in a settings constant, and consumed by admin titles, the OpenAPI schema, and email |
| P1-2 | **`email` uniqueness is enforced by a functional unique index on `Lower(email)`**, not by a `CITextField` | `CITextField` is deprecated in Django 5.1 and removed in 6.0. A functional unique index gives exactly the case-insensitive guarantee 03 §4.1 asks for, and does not depend on a field that is going away |
| P1-3 | **`NULLS NOT DISTINCT` is used where 03 asks for it** (`ledger_accounts`, `locations`, `group_roles`) | Available since PostgreSQL 15 and supported by Django's `UniqueConstraint(nulls_distinct=False)` |
| P1-4 | **Logo assets are committed as optimized derivatives only** | 11 §5.1 forbids large binaries; a 1.3 MB raster poster is exactly that |
| P1-5 | **`accounts` is the first project module**, so `AUTH_USER_MODEL` is set before the first `migrate`; `user_profiles.location_id` arrives later as an additive nullable FK | Django cannot swap `AUTH_USER_MODEL` once `auth`/`admin` are migrated — `admin.LogEntry` fixes a FK to whatever user model existed at the first `migrate`. See §3 |
| P1-6 | **CI is GitHub Actions**; the ten gates of 11 §6 run as parallel jobs on every PR to `main` and `develop` | Decided by the repository owner. Branch protection is configured by hand per `docs/BRANCH_PROTECTION.md`, because it is a GitHub setting and not a file |
| P1-7 | **`User.is_active` is a property over `account_status`**, not a stored column | Two columns holding the same fact can disagree, and the disagreement is a suspended member who can still authenticate — `ModelBackend.user_can_authenticate` reads `is_active`. Note also that `AbstractBaseUser` declares `is_active = True` as a class attribute, so a model that does *not* override it reports every suspended account as active |
| P1-8 | **`date_joined` is a property aliasing `date_registered`**; only one column exists | 03 §4.1 lists both names for the same instant. Two columns holding one timestamp drift, and nothing would say which to trust |
| P1-9 | **`user_profiles.profile_picture` is a storage key (`CharField`), not an `ImageField`** | `ImageField` requires Pillow in the runtime image. The web container serves JSON; an image library there is attack surface with no caller. Pillow is a dev-only dependency used by `scripts/build_brand_assets.py` |
| P1-10 | **Coverage floors are enforced per scope by `scripts/check_coverage.py`**, not by coverage.py's global `fail_under` | 13 §9 specifies four floors (100% branch for `ledger/`, 95% financial, 90% `accounts`/`groups`, 80% line elsewhere). A single global 80 satisfies none of them — it would let the ledger sit at 81%, where an uncovered branch is an unposted transaction |
| P1-11 | **`audit_logs` has no ForeignKeys.** `actor_user_id`, `device_id` and `session_id` are plain `BIGINT` columns, despite 03 §12.1 naming them in Django's FK shape | Three reasons that compound. **A cascade must never be able to delete an audit row** — FR-AUD-003 requires immutability, and `ON DELETE CASCADE` is a deletion path no trigger on the table can intercept. **A record must outlive what it describes**, or "who approved this, and had they revoked that device?" stops being answerable at the moment the question matters. **It keeps `audit` at L0 with no dependencies**, which is what lets every module write to it. The cost is accepted and asserted in `test_audit_immutability.py`: `actor_user_id` has no referential integrity, so a bad id is written permanently |
| P1-12 | **Exactly one hand-written migration exists in the project**: `audit/0002_audit_immutability_trigger` | Its content is DDL no autodetector can express — a plpgsql function and a `BEFORE UPDATE OR DELETE` trigger. Everything else is generated, because a hand-written migration that disagrees with the model state fails gate 5 *after* review. The file documents the command that reproduces it (`makemigrations audit --empty -n audit_immutability_trigger`), so if its name or dependency ever stops resolving, the fix is mechanical |
| P1-13 | **`record()` refuses to run outside a transaction** (`transaction.get_autocommit()`) | The write must join the transaction of the action it describes. In autocommit mode the entry commits *alone*, so an action that then rolls back leaves a permanent record of something that never happened — indistinguishable, in an immutable table, from a record of something that did. The inverse failure (an action with no record) is the one 44 rule 8 is written about; this is the one it does not mention |
| P1-14 | **Audit actions are validated as `noun.verb_past`** (`user.suspended`, `disbursement.separation_of_duties.refused`), lowercase, dot-separated | `action` is the only handle on "who has suspended anyone this month?" — `10 §11`'s SEC-AUD-005 alerting and the admin's `action` filter both depend on the strings being uniform. A stray capital splits one history across two filters, and nothing else in the system would notice. Validated in `record()`, which is the single write path |
| P1-15 | **The rate limiter lives in `config/ratelimit.py`**, and the ceilings live in `settings.RATE_LIMITS` | 10 §6.4 is explicit that limits are "configuration, not constants, so they can be tightened in response to an incident without a deploy" — so the numbers are settings and the mechanism is cross-cutting infrastructure beside `config/middleware.py`. A missing or misspelled scope raises `ImproperlyConfigured` rather than defaulting to unlimited: the failure mode of a silent default is a security control that is simply absent, which nothing would report |
| P1-16 | **When the limiter cannot reach Redis it refuses**, with exactly two named exceptions: `financial_write` and `anonymous_read` fail *open* | A control that silently stops controlling is worse than one that stops the request, because nothing distinguishes it from a control that is working. The two exceptions are scopes whose limits protect against a *buggy client* and *scraping* rather than against an attacker — denying them converts a cache outage into a public outage and into the money path being unavailable. The cost is real and stated: a Redis outage stops logins. `RateLimiterUnavailable` is deliberately **not** a `RateLimited` (P1-16 and `errors.py`): an outage is a P1 for an operator and a 503 for the caller, while a refusal is routine and is a 429 |
| P1-17 | **The refresh token is an opaque 384-bit random string stored under a deterministic SHA-256** — not a JWT, and not Argon2 | SEC-JWT-002 requires it stored hashed, and 03 gives `refresh_token_hash` as the *only* column identifying a session, so the digest must be deterministic or a presented token could only be found by scanning the table — which is why a salted hash is impossible here rather than merely unnecessary. SHA-256 over Argon2 for the same reason: the value is 384 bits from `secrets`, so there is no dictionary for a work factor to slow, while an unauthenticated endpoint that spends ~100 ms of CPU per call is an easy exhaustion target. The three low-entropy secrets in this module's sibling files (password, 4-digit PIN, 6-digit OTP) still take the slow hash. Domain-separated (`ihweza.refresh.v1:`) so no other digest in the project can collide with it |
| P1-18 | **`revoke_session_family()` is the only definition of "family" in the project** | SEC-JWT-004 and SEC-JWT-009 both require revoking a "session family", and 03 has no lineage column to represent one (C6). It is inferred as `(user, device)` and justified by SEC-JWT-005's device binding. Localising it in one function means that if C6 is resolved by adding a column, exactly one function changes — rather than every call site that would otherwise have written its own version of the query and drifted |
| P1-19 | **The lockout schedule lives in `settings.LOCKOUT`**, read through `services/lockout.py`, and has **no enable/disable flag** | SEC-LOCK-001's "default 5" is configuration for the same reason §6.4's table is (P1-15) — an incident that needs a different threshold should not need a deploy. The absent flag is P1-16's reasoning applied unchanged: a switch that turns a control off is the switch that ends up set in production, and it would not solve the problem it was written for. A missing or non-integer key raises `ImproperlyConfigured` rather than defaulting, because a backoff that silently becomes "unlimited" is worse than one that fails loudly |
| P1-20 | **The escalating backoff is a pure function of `failed_login_attempts`**: 60 s doubling once the threshold is passed, capped at 15 minutes | 10 specifies the *shape* ("a backoff period that escalates with repetition") and no numbers, so the numbers are this project's decision and are stated. Deriving the window from the existing counter rather than adding a `lockout_count` column avoids a second source of truth for "how many failures has this account had" — the two would disagree the first time one path forgot to increment it. The cap is load-bearing rather than cosmetic: an uncapped doubling lets anyone who knows a phone number hold that account closed indefinitely, which converts a public identifier into a denial of service against a real person |
| P1-21 | **An attempt made while the lockout window is open is refused and changes nothing** — it neither increments the counter nor moves the deadline | The requirement does not say. The alternative — counting attempts as if the account were not locked — lets a stranger hold an account closed forever with one request per window, and a phone number is not a secret here: it is how a collector finds a member. The escalation still happens for an attacker who returns after each window closes, because each of those is a fresh failure, and the schedule in P1-20 is what bounds them |
| P1-22 | **A registration attempt against a number that is already registered**: not yet phone-verified → a fresh verification code to that number; already verified → nothing written and nothing sent. **The branch is on phone presence only, never on account status** | Nothing in 03 or 10 settles it. Refusing the unverified case would leave a half-registered account that password reset cannot help — there is no working account to reset into — and no route forward at all. Sending a code for a verified account would change nothing and would make registration a way to make a message arrive on any member's phone whose number someone knows. Branching on status would be a second oracle: "registered but suspended" identifies an account as plainly as "registered" (S10) |
| P1-23 | **The password similarity validator is configured with `user_attributes = ("username", "email", "full_name", "phone")`** | Django's default is `("username", "first_name", "last_name", "email")`, and three of those four name something this model does not have or does not use: there is no `first_name`/`last_name`, and `username` is nullable and is not an identity (`USERNAME_FIELD` is `phone`). On the default list the validator would compare against almost nothing for most members. The `phone` entry earns its place even though `NumericPasswordValidator` already refuses an all-numeric password: a *mixed* password that merely contains the number is not numeric and would otherwise pass, while the number is public and therefore the first thing to try |
| P1-24 | **Every login refusal that returns before verification spends one password hash** (`credentials._spend_a_hash`) | SEC-AUTH-005 requires identical failure responses, and identical response *bodies* are not sufficient: the paths that return early — an identifier no account matches, and an account inside its lockout window — would otherwise answer in the time a primary-key lookup takes while every other answer takes the time an Argon2id hash takes. Tens of milliseconds against under one is measurable over a network and is an account-existence oracle. Hashing against a throwaway instance is also how Django's own `ModelBackend` closes the same hole for an unknown username, so the mechanism is the framework's. The cost is one hash per refusal, bounded by the login rate limit, and it is paid deliberately — the lockout path is the worse of the two to leave visible, because a lockout is proof that the account exists |
| P1-25 | **The account-status branch in `authenticate()` is written `!= ACTIVE`**, with `CLOSED` named only to choose its message, so any status this build does not recognise fails **closed** | FR-AUTH-013 requires the status to be enforced; a row written by a newer deploy, or by hand during an incident, must not authenticate merely because this build cannot name the state it is in. Writing it as two positive tests (`== SUSPENDED`, `== CLOSED`) would let an unknown value fall through to success, which is the one direction this control must not fail in. Asserted in the tests against the whole `AccountStatus` enum rather than against the two members of it that exist today |

## 6. Blocking items

| Item | Blocks | Owner |
|---|---|---|
| **No git remote is configured** | Branch protection (11 §8), CI on PRs (11 §6), and the develop/main model (11 §2) | GitHub chosen (P1-6); the remote must be added and the `develop` branch created before the first PR. `docs/BRANCH_PROTECTION.md` is the checklist to run once it exists |
| **No dependency lock file** | Reproducible builds (12 §2.1), gate 9's `pip-audit` baseline | `requirements/*.txt` carry ranges only. `pip-compile --generate-hashes` must run to produce `*.lock` once a working toolchain exists. Blocked by the same missing environment as the row below, and unblocked by the same step: `pip-compile` lives in `requirements/dev.txt`, so it arrives with the virtualenv |
| **No toolchain is installed, and none has run** | Every gate of 11 §6 | Checked directly on 2026-09-16: `python3` is present at `/usr/bin/python3`, and **`ruff`, `mypy`, `pytest`, `django-admin`, `pip-compile` and `pre-commit` are all absent** — as is any `.venv` in the repository root. So this is not "the gates have not been run yet"; it is that the environment to run them in does not exist. The first step is therefore not a command but a provisioning one: create a virtualenv and install `requirements/dev.txt` into it, which is also what unblocks the lock file below. Nothing in S0, S1a, S2 or S1b has been executed: no `manage.py check`, no `ruff`, no `mypy`, no `pytest`, no `migrate`. Treat all four slices as unverified until `scripts/ci.sh` passes locally. The provisioning step was attempted on 2026-09-16 and **the attempt was refused by the sandbox's command classifier before `python3` ran** — so the absence is a property of this environment, not of the repository, and the step is *unexercised* rather than known-failing: whether `python3 -m venv` and a reachable PyPI index exist here is still unknown. The moment a shell with ordinary command execution is available, that is the first command, not a redesign Two lint rules were reasoned about from the configuration and cannot be confirmed without running it: `DJ001` (nullable string fields) is overridden with its rationale in `pyproject.toml`, and `RUF012` (mutable class attributes need `ClassVar`) may fire on the `indexes`/`constraints` lists in each model's `Meta` — that one is a one-line decision, not a defect, but it is unidentified rather than settled. A third finding of the same kind came from reading `select` and the per-file-ignores rather than from running the linter: `config/settings/production.py` imported `require_env` and never called it, which is `F401` — `config/settings/**` is ignored for `S105` only, and the `import *` from `base` is what supplies the env helpers, so the explicit import was both unused and redundant. Removed on 2026-09-16. It is worth recording as a *class* rather than as a fix: the three environment modules import the same helpers in three different styles (`staging.py` from `production`, `dev.py` from `base`, `production.py` relying on the star import and carrying a load-bearing `# noqa: F405`), so more of these are likely and only the linter will find them. The largest instance of the same class was found the same way on the same day, and unlike `F401` it is not a phantom: `ruff`'s `line-length` is **100** (`pyproject.toml:11`), and a `wc -L` sweep over the package finds **seven files past it** — `tests/security/test_static_guarantees.py` at 182, `apps/accounts/admin.py` 108, `tests/unit/test_settings_contract.py` 103, `config/api/pagination.py` 102, `tests/integration/test_lockout_service.py` 101, `tests/api/test_error_envelope.py` 101, `apps/accounts/services/otp.py` 101 — alongside aligned-continuation dicts and tuples in the test suite of a shape the formatter re-breaks (`test_ratelimit.py`'s `for scope in ("login", …)` and the `RATE_LIMITS` literal in `test_a_permanently_broken_counter_falls_into_the_policy`). So **gate 1 is red against the repository as committed**, and has been since those files were written: `ruff format` has never run. That makes the command the toolchain row above already owes the first CI run, not a step in anyone's editor. Nothing written on 2026-09-16 exceeds the limit — the longest line in any file edited that day is 101, at `test_lockout_service.py:362`, a signature no edit touched |
| **S1a and S2 have no `0001_initial`** | Gate 5 (`makemigrations --check`), and therefore every later slice | The models exist; the initial migrations do not. They must be **generated by `makemigrations accounts audit`**, not hand-written — a hand-written migration that differs from what Django generates fails gate 5 anyway, and one that is subtly wrong (constraint names, a functional index on `Lower(email)`) surfaces at `migrate` rather than at review |
| **`audit/0002` must be deleted before `makemigrations` can run at all** | The step above | `makemigrations` builds the migration graph before doing anything else, and `audit/0002_audit_immutability_trigger` declares a dependency on `audit/0001_initial`, which does not exist. So the first `makemigrations` fails with a `NodeNotFoundError` naming that file. The failure is correct and self-locating: delete `0002`, generate the initials, re-create `0002` with `makemigrations audit --empty -n audit_immutability_trigger`, and paste the operations back. The exact sequence is in that file's docstring. The file is committed in this state deliberately — its SQL is the one part that cannot be regenerated from the models, and it is worth more in version control than in a scratch buffer |
| **The client's IP address is undefined behind a reverse proxy** | The `ip_address` column of every audit entry, and therefore SEC-AUD-005's "repeated lockouts across accounts from one IP" | `apps.audit.services.client_ip()` reads `REMOTE_ADDR` and deliberately ignores `X-Forwarded-For`, because the header is client-supplied and trusting it without knowing the proxy topology writes an attacker-chosen string into an immutable record. 12 does not say how many proxies sit in front of the app. Behind Nginx the column will record the proxy's address until that is decided |
| **`TRUNCATE` bypasses the immutability trigger** | FR-AUD-003 under a compromised or careless application role | `TRUNCATE` does not fire row-level triggers, so immutability holds only if the application's database role does not own `audit_logs` (ownership includes the right to `TRUNCATE`). This is a deployment requirement for 12, not a code change — and it is the reason SEC-AUD-004 exists |
| **`.pre-commit-config.yaml` hook revisions are unresolved** | Gate 8's local half | Written without network access. Run `pre-commit autoupdate` before `pre-commit install` |
| **No access-token signing keypair exists** | SEC-JWT-001, and therefore S4's token issuance | "signed with a strong asymmetric algorithm so services can verify without holding the signing key" needs a keypair, a per-environment value (SEC-SEC-005) and a rotation story (SEC-SEC-006) — none of which can be committed (SEC-SEC-001). It is deployment configuration with a generation step, not a code change. S1b deliberately does not mint access tokens for this reason: the half of the lifecycle the database holds is in `services/sessions.py`, and the half that needs a key is in S4 |
| **The refresh endpoint is in no rate-limit scope** | S4's exposure, and C7 | 10 §6.4 lists eight scopes and none of them covers token refresh. `rotate()` is unauthenticated and does a locked lookup per call, so it is a cheap target — see C7. Options are a ninth scope or an accepted risk; either is a change to 10, not to the code |
| **Account creation is in no rate-limit scope either** | S4's exposure, and C8 | Same table, same omission, a different endpoint: registration is an unauthenticated write that creates rows and sends a message. The only bound on it today is SEC-OTP-004's per-destination issuance windows, which bound how often one number is *messaged*, not how often accounts are created. See C8 |
| **A required security message has no channel in the design** | SEC-LOCK-004, and therefore S4's step-up path | SEC-LOCK-004 requires a notification to the registered phone when a step-up challenge is raised; 09's trigger catalogue is declared complete and holds no security notifications, and its SMS rules reserve the channel for that catalogue and cap it below what SEC-OTP-004 already allows. See C9 |
| **Nothing decides which verification column wins** | S4's verification endpoints, and any onboarding report | 03 §4.1 specifies `verification_status`, `is_phone_verified` and `is_email_verified` with no stated precedence, so "what is this member's verification state" has more than one answer. `registration.py` reads the narrower column because its question is specifically about the phone; that is a choice at one call site, not a resolution. See C10 |
| **The risk signals of SEC-LOCK-004/005 have no source** | S4 and S5 | "Impossible travel" cannot be computed from anything in the schema — no login carries a location — and §6's `client_ip()` deliberately does not trust `X-Forwarded-For`, so even coarse geolocation from the address is unreliable behind a proxy. "Unusual withdrawal patterns" is Phase 3 by scope. The *new device* half is computable, from `devices`; it is the other two that are not. See C11 |
| **No `accounts` service writes a business-audit record** | 02 §4.3, 44 rules 8 and 10, FR-AUD-002 — and S1b's own definition of done | S2's `record()` is the single write path and no `accounts` service calls it. **This is not the defect it first looks like, and the fix is not to add the call.** FR-AUD-002's five named actions are all S5+, and the events S1b *does* produce are SEC-AUD-001's authentication events, which must be logged "distinct from business audit" — so filing them through `record()` would put them in the financial table and violate the requirement it was invoked to satisfy. The real gap is C5 (no security sink) and it is not closable from S1b. What remains genuinely owed here is a business-audit write at the point S1b first has one; on the present slice that is a phone-number change, which FR-AUD-002 does name, and which is blocked on C4's snapshot-redaction decision. P1-13 is the reason this is a transaction-boundary change rather than an added line: `record()` refuses to run outside a transaction, and `credentials.authenticate()` opens one only inside `lockout.record_failure()` |
| ~~**Eight misplaced `type: ignore` comments**~~ — **closed 2026-09-16** | Gate 3 (`mypy .`, with `warn_unused_ignores`) | Found by reading, because mypy cannot run. Seven sites in `tests/unit/test_ratelimit.py` and one in `tests/integration/test_lockout_service.py` carried `# type: ignore[no-untyped-def]` on a *continuation* line of a multi-line `def` rather than on the `def` line, which is where mypy reports the error — so each was one unignored error plus one "unused ignore" error, eight sites and sixteen errors in two files. One of the eight (`test_the_unavailable_scopes_are_the_shipped_ones`) took only annotated parameters and so needed no ignore at all. Fixed by removing the untyped `settings` fixture from those signatures and patching `django_settings` through `monkeypatch`, which needs no ignore in either position — moving the comment would have required being right about mypy's reporting line, and this way the question does not arise. **This is the row that shows what "no toolchain has run" costs**: eight definite type errors survived two sessions of careful reading and were found only by grepping for the pattern after one was noticed by accident. There is no reason to think they were the last |
| **The 46 remaining `settings`-fixture ignores rest on where mypy anchors `no-untyped-def`** | Gate 3 (`mypy .`), across six test files | Every function taking pytest-django's `settings` fixture carries `# type: ignore[no-untyped-def]`: 13 in `tests/unit/test_ratelimit.py`, 15 in `tests/unit/test_lockout_backoff.py`, 9 in `tests/integration/test_lockout_service.py`, 6 in `tests/integration/test_otp_service.py`, 2 in `tests/integration/test_registration_service.py`, 1 in `tests/integration/test_ratelimit_redis.py`. (A seventh kind sits at `tests/conftest.py:29`, on `api_request_factory()`, untyped for a different reason: neither its arguments nor its return are annotated.) These are believed correct, and the belief is **not** about pytest-django's typing — that was an earlier misreading of this row. `disallow_untyped_defs = true` (`pyproject.toml:79`, global, not confined to the financial-module override) is a **syntactic, per-definition** check: it flags any signature with an unannotated argument, whatever a fixture supplies, and mypy resolves no fixture types here — the only plugin configured is `mypy_django_plugin`, which knows about Django, not pytest. So an unannotated `settings` parameter needs the ignore wherever it appears, and all 46 are in the right place: on the `def` line. Whether that is the right place is the same question as where the eight defective sites went wrong — an ignore on a *continuation* line of a multi-line `def` missed the error and became an unused ignore itself, two errors per site. So **this row and the one above are one question with one command's answer**: `mypy .` either reports the 46 or it does not, and if it does not, the anchors are the reverse of what both rows assume. Recorded rather than resolved because the whole set is the accepted cost of the `settings` fixture, and the alternative the project already prefers — `monkeypatch.setattr(django_settings, ...)`, as in `test_credentials_service.py` — removes the question instead of answering it |
| **No marker-based selection is implemented anywhere** | 13 §10's suite split (6a–6e), and the `slow` marker's own meaning | Every test carries its suite marker and the markers are all registered in `pyproject.toml`, but **nothing filters on them**: `scripts/ci.sh` runs `pytest -p no:randomly` with no `-m`, `.github/workflows/ci.yml` runs a bare `pytest`, and `addopts` adds only `--strict-markers`/`--strict-config`/`-ra`. Two consequences. The minor one: 6a–6e cannot be run individually, so the "fast feedback loop" that 13 §10's split exists for is a marker convention rather than a capability — a developer has to know to type `pytest -m unit`. The one that matters: `slow` is documented in `pyproject.toml` as "excluded from the default local run" and is excluded from nothing, so the first long test to be marked `slow` will run on every local invocation and in CI while its author believes it is skipped. `scripts/ci.sh`'s comment claimed the suites were "selected by marker"; it now says what the command actually does. Fixing this is one `-m` expression per place, but which expression belongs in CI versus the default run is a decision about the developer loop, so it is recorded rather than guessed |
| **No password-reset flow exists in any slice** | FR-AUTH-009, SEC-AUTH-007, 10 §6.4's `password_reset` scope (3/hour per account), S10 | Repository owner: the pieces exist and nothing joins them, so this is a scoping decision (S1b or S4) rather than a design one. `RATE_LIMITS["password_reset"]` is configured and **has no caller in `apps/`** (verified by grep); `otp.issue()` already takes `user` as optional explicitly for this flow; `OTPCode` carries the nullable column it needs. The gap is a dead end in exactly one place, not two: `registration.py`'s verified-member branch sends a member who has forgotten they registered to password reset, and there is no password reset — whereas the abandoned *unverified* member has a working route, because re-registering takes the unverified branch and issues a fresh code (P1-22). So the exposure is the verified member who has forgotten their password and holds no other session. This row **replaces an earlier one** that read "`passwords.py` emits no record of any kind": that module is the strength *validator* and has no flow to record, and the row's two supporting claims (that `credentials.py` directs a closed account to reset, and that the abandoned unverified member has no route) were both wrong — `AccountClosed` is terminal and mentions no reset, and the unverified branch contradicts the second. Recorded because a wrong blocking item is worse than a missing one: it sends the next reader to the wrong file |
| **Nothing in S1b has an HTTP surface** | 04 §2's 21 endpoints, the `Retry-After` header (04 §1), and SEC-AUTH-005's "identical responses" | The domain errors are raised and never mapped; `RateLimited.retry_after` is carried and never emitted. Two consequences are worth naming: the collapse SEC-AUTH-005 requires is asserted against *exception classes*, not against response bodies, and the 429/503 distinction of P1-16 is unexercised. S4 is where both become real |
| ~~**`record_failure` has no concurrency test**~~ — **closed 2026-09-16** | Was: the property it is written for, which no sequential test can reach | Written as `tests/concurrency/test_lockout_contention.py`, three tests: two threads against a threshold of 2 asserting a counter of 2 *and* a locked account; five threads against a threshold of 5; and three threads against an already-locked account asserting the counter stays at 1, which is P1-21 holding under contention. Thresholds are installed via `monkeypatch.setattr` on `django_settings.LOCKOUT` because `lockout._configured()` reads the setting at call time, so the patched value is visible to worker threads. The barrier, not the join, is what bounds a stuck worker — `ThreadPoolExecutor.__exit__` waits without a timeout, so a join timeout cannot prevent a hang and the file says so rather than implying otherwise. The `tests/concurrency/` package also did not exist; `tests/__init__.py` says such directories are created by the slice that first needs them, so this is that slice. **Unverified like everything else — it has never been run**, and the likely first-run friction is in the harness (barrier and connection handling), not the service. `clear()` is deliberately not contention-tested: it performs no read-modify-write, so there is no update to lose, and the interleaving it does have is benign by design rather than by measurement |
| §45 legal review | Launch, Phase 3 (custody, KYC, currency) | Not a Phase 1 blocker; tracked in 14 |
| `DJANGO_SECRET_KEY` and provider credentials | Production only | Environment, never committed (10 §7) |

## 7. Specification conflicts surfaced during implementation

Found by building S1, S2 and S1b, and reading the code back against 10. Each is a place
where **03 and 10 disagree or 03 is silent**, so the code cannot satisfy both. §52 requires
ambiguity to be surfaced and confirmed rather than silently resolved into a rule, and
inventing schema to fill a gap is the failure mode that rule exists to prevent.

C1–C7 were found building the models and the first services. **C8–C11 were found by S1b's
last two services** — `registration.py` and `credentials.py` — and C8, C9 and C11 are places
where 10 asks for a control whose *inputs* the design does not provide, which is a different
kind of gap from a missing column and is easier to miss for exactly that reason.

None of these is a defect in the code that has been written: S1, S2 and S1b follow 03
literally. They are defects in 03 — and, for C7, C8, C9 and C11, in 10, with C9 reaching into
09 as well — found the moment something had to implement it.

| # | 10 requires | 03 provides | Blocks |
|---|---|---|---|
| **C1** | **SEC-MFA-002/003**: TOTP enrolment confirmed by a valid code before it takes effect, and recovery codes that are single-use and stored hashed. SEC-MFA-004 makes MFA **mandatory** for `SUPER_ADMIN`, `PLATFORM_ADMIN`, `TREASURER`, `GROUP_OWNER` and `CHAIRMAN` | Only `users.two_factor_enabled` and `users.two_factor_method`. There is **no column for a TOTP secret** and **no table for recovery codes** | S4 |
| **C2** | **SEC-DATA-004**: fields needing the plaintext, "e.g. next-of-kin phone", are encrypted at the application layer with a managed key | `user_profiles.next_of_kin_phone` is a plain `VARCHAR(20)`. 03 mentions encryption nowhere | S1's services, S4 |
| **C3** | **SEC-LOCK-002**: lockout is per-account **and** per-source-IP | `users.failed_login_attempts` and `users.locked_until` — per-account only | S4 |
| **C4** | **SEC-PII-003**: a data subject's erasure or anonymisation is supported, and 10 §5.3 reconciles this with §44 by severing the identity link — "it anonymises the *person*, not the *transaction*" | `audit_logs` is immutable by trigger (FR-AUD-003) and cannot be anonymised, but FR-AUD-002 names "user changed phone number" as an auditable action, so `previous_value`/`new_value` will hold phone numbers; `object_repr` can hold names; `ip_address` is personal data under the NDPA. The rows are permanent | S1b's first PII-bearing action, S4 |
| **C5** | **SEC-AUD-001**: authentication events — "success, failure, lockout, MFA challenge, token reuse detection" — "logged as security events **distinct from business audit**". **SEC-AUD-004**: audit logs "shipped to storage the application cannot rewrite, so a compromised application cannot erase its own tracks" | 03 defines exactly one destination, `audit_logs`, and 12 specifies no log sink. The trigger protects against a *careless* application; SEC-AUD-004 exists because it does not protect against a compromised one | **S1b, not S4** — corrected below |
| **C6** | **SEC-JWT-004**: reuse detection revokes "the entire session family". **SEC-JWT-009**: logout, password change, PIN change and role revocation invalidate the session family | `user_sessions` has no lineage: no `family_id`, no `replaced_by`, no `revoked_reason`. A family cannot be *represented*, only inferred, and `revoked_at` cannot distinguish "rotated away" from "revoked deliberately" | S1b's `sessions.py` (implemented with an inference — see P1-18) and S4 |
| **C7** | **10 §6.4** is the platform's complete rate-limit table, and SEC-JWT-003/004 make the refresh endpoint a security-critical, high-traffic one | §6.4 lists eight scopes and covers login, OTP, PIN, password reset, writes, bulk send and anonymous reads — **but not token refresh**. 10 is silent about a control on one of the two endpoints that hands out credentials | S4 |
| **C8** | **10 §6.4 is the platform's complete rate-limit table**, and it covers every user-facing credential action: login, OTP issue and verify, PIN entry, password reset, financial writes, bulk send, anonymous reads | §6.4 lists **no scope for account creation**. 03 provides nothing either. Registration is an unauthenticated write that creates two rows and sends a message, and the only bound on it is SEC-OTP-004's per-destination issuance windows — which bound how often one number is *messaged*, not how often accounts are created | S1b's `registration.py` (implemented with the gap recorded in its docstring) and S4 |
| **C9** | **SEC-LOCK-004**: a step-up challenge raises "a notification to the account's registered phone". SEC-AUD-005 wants security events alertable | 09 §4 declares its 12-trigger catalogue "The complete list" and holds **no security notifications**; §3.2 reserves SMS for that catalogue, caps it at 5 per member per day, and falls back to in-app beyond the cap — while **SEC-OTP-004 already allows 10 OTPs per day to one destination**. So the channel a security message would use is configured to reroute itself to a channel that cannot deliver a step-up challenge, below the volume an existing requirement already permits | S4 (where SEC-LOCK-004 first applies), and the notifications module |
| **C10** | Nothing in 10 says which column answers "is this member verified" — but FR-AUTH-013/014, SEC-AUTH-* and every onboarding report depend on there being one answer | 03 §4.1 specifies `verification_status`, `is_phone_verified` **and** `is_email_verified` with **no stated precedence**. Three columns, one question | S4's verification endpoints, and any reporting on onboarding |
| **C11** | **SEC-LOCK-004** (step-up on a new device or an impossible-travel signal, "plus a notification") and **SEC-LOCK-005** (unusual withdrawal patterns trigger review) | No login carries a location, so impossible travel is not computable; §6's `client_ip()` deliberately ignores `X-Forwarded-For`, so coarse geolocation from the address is unreliable behind a proxy (and §6 records that the proxy topology is unknown). The *new device* half **is** computable, from `devices` plus SEC-JWT-005's binding. SEC-LOCK-005's input is transaction history, which is Phase 3 by scope rather than by gap | S4 and S5 |

### Options

**C1** — either add `users.totp_secret` (encrypted, per C2's mechanism) plus a
`mfa_recovery_codes` table to 03, or record a decision that TOTP is out of scope for Phase
1 and MFA is SMS-only. The second is a real weakening of SEC-MFA-001 and SEC-MFA-005
together (SMS is explicitly "the weaker factor"), and SEC-MFA-004 makes MFA mandatory for
five roles — so it is a decision for the repository owner, not for implementation.

**C2** — needs a key-management decision first, because the encryption key is a secret
with a rotation story (12 §4, 10 §7). Two candidate shapes: application-layer encryption
of the individual columns, or not storing the plaintext at all. This one has a legal
dimension as well as a technical one and belongs with the §45 review.

**C3** — per-source-IP lockout is normally a Redis counter rather than a table, which
would make this a *service* concern and not a schema gap. Worth confirming, because if it
is meant to survive a Redis flush then it needs a table, and a security control that
evaporates on a cache restart is not one.

**C4** — found while writing the audit module, and the sharpest of the five, because the
two requirements are each satisfied and jointly unsatisfiable as the schema stands. 10 §5.3
resolves erasure against §44 by severing the *identity link*: the ledger entry keeps its
amounts and loses the person. That reconciliation works because ledger rows are records of
a transaction. An audit row is a record of *a person's action*, and FR-AUD-002 explicitly
names "user changed phone number" — so the snapshot columns are designed to contain the
very data erasure is about, in a table whose whole purpose is to be unalterable. Neither
requirement is wrong; the design has no room for both.

Three shapes, and the first is the one I would argue for:

* **Mask at write time.** `record()` stores a redacted value — `+23480•••••34` — rather
  than the raw one. The audit purpose is served ("who changed whose phone, and when"),
  because the entry never needed the full number to be *explanatory*; it needed to explain
  *what changed*. And a masked value is not personal data, so there is nothing to erase and
  the conflict dissolves instead of being managed. It has a real cost: an investigator
  cannot recover the old number from the audit trail, and would have to get it from
  somewhere else — which is the correct place anyway, since the audit trail's job is to say
  that a change happened and who made it.
* **Legal-obligation carve-out.** NDPA permits retention where processing is necessary to
  comply with a legal obligation, and §44 rule 10 requires the trail. Defensible, but it
  makes the answer to a data subject "no", and it needs counsel to say so in writing — this
  belongs with the §45 review.
* **Encrypt the snapshot columns** with the C2 key. Preserves the value, keeps the row
  immutable, and gives the erasure mechanism a key to destroy instead of a row to edit —
  crypto-shredding by subject. The strongest of the three technically, and it inherits
  C2's unsolved key-management question.

**C5** — 03 and 12 are silent rather than contradictory. `record()` is a single write path
to a single table, and the immutability trigger is exactly as strong as the database
permissions of the process holding the connection (see §6 — `TRUNCATE` is not covered by
the trigger at all). SEC-AUD-001 asking for security events "distinct from business audit"
and SEC-AUD-004 asking for a sink "the application cannot rewrite" both describe something
that is not `audit_logs`, and nothing in the design set defines what it is. Queueing
several hundred authentication events per hour into the same table and query path as the
financial trail is also a pure indexing decision with a real cost, independent of the
security question.

**This was attributed to S4 until S1b's login half was written, and that was wrong.** The
events SEC-AUD-001 names are already produced by S1b, on processes that exist today:
`lockout.record_failure()` applies a lockout, `sessions.rotate()` detects token reuse and
calls `revoke_session_family()`, and `credentials.authenticate()` emits both the success and
the failure lines. `otp` routes issuance the same way, though ordering a code is not
literally one of SEC-AUD-001's five — it is an authentication event by any reading, and its
module docstring treats it as one. All of them are `logging` calls. So the question C5 asks —
*where do security events go?* — is being answered today by default, via the root logger,
and the answer is "into whatever the logging configuration points at", which in Phase 1 is
stdout. That is not an immutable sink and not a queryable one. **All four call sites now say
so in their own docstrings** — `otp.py`, `lockout.py`, `sessions.py` and (as of 2026-09-16)
`credentials.py`, which was the one place still emitting a security event with no note
explaining where it went and why not to `audit_logs`. So the gap is not undocumented; it is
four services that have each independently recorded that there is nowhere to write. The fifth event, the MFA
challenge, does not exist at all — SEC-AUTH-006's PIN is unimplemented. `passwords.py` is the
one `accounts` service with no logger, but that is not a gap in the same sense: it is the
strength *validator* rather than a flow, and it has nothing to log that the functions calling
it do not. The genuine missing flow is password reset, which exists nowhere (§6), so a reset
is not a security event the project is failing to record — it is an event that cannot occur
yet. Calling `record()` from these services is **not** the fix and would be worse than
leaving them as logs: it would put the authentication trail into the financial, permanent,
un-anonymisable table that C4 is already stuck on, in direct contradiction of the phrase
"distinct from business audit". This also makes C5 a blocker on S1b's definition of done
rather than only on S4's.

Until these are answered, S4 (auth API) cannot be completed: C1, C3, C7, C8, C9, C10 and C11
all land there. C2 also reaches into S1, because the field already exists in the shape 03
specifies. C4 and C5 reach into S1b, and C5 was moved here from S4 once it became clear the
events it governs are already being emitted: C4 because the first `accounts` service that
records a phone-number change has to choose one of the three shapes above, and choosing by
default is the failure §52 exists to prevent; C5 because three services already produce
SEC-AUD-001's authentication events and are currently routing them to stdout by default. C10
has already reached S1b in a narrow way — `registration.py` had to pick one of the three
columns to answer "is this member's phone verified"; it picked the narrowest and said so at
the call site, which is a choice recorded rather than a resolution reached.

**C6** — S1b needed an answer to implement SEC-JWT-004 at all, so it took the narrowest one
available and **recorded it rather than hiding it**. Reuse detection is implemented as
"presenting a row whose `revoked_at` is set", and the family is inferred as
`(user, device)` — justified by SEC-JWT-005's device binding and confined to the single
function `revoke_session_family()` (P1-18). Two consequences follow, and both are real:

* **A member who signs out and then replays a cached token looks exactly like a thief.**
  `revoked_at` cannot distinguish "this was rotated away, so seeing it again is theft" from
  "this was deliberately revoked, so seeing it again is a stale app". The false positive
  revokes the family and raises a security event — noisy, and wrong.
* **Rotation cannot be told from an explicit revocation** in any report, which also means
  the count in the reuse alert is unverifiable after the fact.

The fix is a column: a `revoked_reason` (or a `replaced_by_id` lineage pointer, which is
stronger — it makes the chain walkable and answers "what did this token become"). Either is
a small additive migration, and because the inference is in one function, adopting it is
local. Not built here because it is schema, and adding schema to fill a gap is what §52
forbids doing silently.

**C7** — the omission is likely incidental rather than deliberate: §6.4's table reads as a
list of *user-facing* actions, and refresh is infrastructure. But the endpoint is
unauthenticated, is called by every client roughly twice an hour, and — per P1-17 — does a
locked row lookup per call. An attacker does not need to guess a token to make the server
work; they need only send plausible ones. Two shapes:

* **A ninth scope** in §6.4 (`refresh`: some multiple of the per-member rotation rate, keyed
  on the member once the token resolves and on the source IP before that). This is the
  consistent answer — every other credential endpoint has one.
* **Leave it unlimited**, on the grounds that the token is 384 bits and the lookup is
  indexed. That treats a CPU-exhaustion denial of service as a client-behaviour problem,
  which is the argument §6.4's own "Runaway clients" row exists to reject.

Note that the second option also means the "reuse detected" path — which revokes a family
and writes a security event — has no throttle in front of it, so an attacker who *does*
hold one valid token can trigger that write path at will.

**C8** — the omission has the same shape as C7's: §6.4 reads as a list of the actions a
*member* performs on their own account, and registration is performed by someone who is not
yet a member. But it is the only unauthenticated endpoint that writes rows **and** spends
money — every successful registration sends an SMS at the platform's expense — so it is a
more attractive target than refresh, not a less. Three shapes:

* **A ninth scope** (`registration`, keyed on the source IP): consistent with every other
  credential endpoint, and the natural home for it. The objection is real and is about this
  market specifically: a shared address — a cybercafé, a carrier's NAT, a collector
  registering twelve members from one handset — is the *ordinary* case, so a per-IP ceiling
  low enough to matter is also low enough to refuse legitimate signups. That makes the
  numbers harder than the shape, and it is the reason I would not pick this one alone.
* **Treat SEC-OTP-004's windows as sufficient.** Defensible, and cheap: an account cannot be
  created without a code, and codes are capped at 3 per 15 minutes and 10 per day per number.
  What it does not bound is rows created by someone who never verifies — ten unverified rows
  per number per day, and a fresh number costs nothing.
* **A switch rather than a limit.** `RegistrationNotAllowed` already exists and nothing raises
  it. Closing registration during an incident as a settings change rather than a deploy is the
  same argument P1-15 makes for the limits themselves. It is not a substitute for a ceiling;
  it is the answer for the hour when the ceiling is not enough.

The second and third together are what S1b leaves in place, and it says so in
`registration.py`'s docstring rather than leaving the reader to infer it.

**C9** — the hardest of these four to work around, because 10 and 09 are not merely silent but
arithmetically inconsistent with each other. SEC-LOCK-004 requires an SMS to the registered
phone as part of a step-up challenge. 09 §3.2 permits SMS only for §4's triggers; §4 declares
its catalogue "The complete list" and holds no security notification; and §3.2 caps SMS at 5
per member per day with a fallback to in-app — **below the 10 OTPs per day SEC-OTP-004 already
allows to one destination**. So a member can be sent ten verification codes and not the one
message that tells them their account is being attacked from somewhere they have never been.
Three shapes:

* **Add security notifications to 09's catalogue and exempt them from the §3.2 cap as a
  class.** The exemption is the important half: the cap's purpose is to bound a member's
  *volume of messaging*, and a cap with that purpose should not be able to suppress the
  message that says their account is under attack.
* **Raise the §3.2 cap above SEC-OTP-004's allowance.** Weaker, because it re-couples the two
  numbers: the next increase to the OTP allowance silently re-creates the conflict.
* **Carry the challenge in-app only.** Rejected on the requirement's own terms — SEC-LOCK-004
  names the registered phone, and an in-app notice reaches only a member already holding the
  app, which is precisely not the case when the suspicious login is the one being challenged.

**C10** — three columns, one question, no rule. This is the same defect the model's own
`is_active` docstring already names: two columns holding one fact can disagree, and nothing
says which to believe. `is_active` was *derived* from `account_status` for that reason (P1-7);
verification was not derived from anything. It matters beyond tidiness because the three can
disagree through a path the API will expose: a member who verifies an email reaches
`EMAIL_VERIFIED` while `is_phone_verified` is still False, and a report asking "how many
members are fully onboarded" then depends on which column its author happened to reach for.
Three shapes:

* **State a precedence in 03 and make `verification_status` derived** from the two booleans
  rather than stored — P1-7's treatment, applied to the second instance of the same defect.
* **Keep all three, declare `verification_status` the summary**, with the booleans as its
  inputs, and add a `CHECK` constraint refusing any combination the ladder cannot represent.
  Keeps all three queryable and makes the disagreement impossible rather than documented.
* **Drop `verification_status`** and answer from the two booleans, since that is what the
  ladder is computed from. Cheapest, and it gives up indexing "fully verified" as one value.

Not decided here, because it is schema: every shape either adds a constraint or removes a
column, and §52 forbids changing schema to fill a gap silently.

**C11** — the gap is not a missing column but a missing *input*, which is why it reads as
complete until something tries to compute it. "Impossible travel" needs to know where a login
came from; nothing in the schema records that, and §6's `client_ip()` deliberately refuses to
read `X-Forwarded-For`, so even the coarse substitute — geolocate the address — is unreliable
until the proxy topology is settled. One half of the requirement is genuinely computable and
should be stated as such rather than filed with the rest: the **new-device** signal needs only
`devices`, which already holds one row per member per install, plus SEC-JWT-005's binding. So
SEC-LOCK-004 is partly implementable in S4, and calling the whole of it blocked would overstate
the problem.

Two options for the travel half: record a coarse location on the session row (city or country,
which needs a geolocation database — a dependency, a size, and a privacy decision of its own),
or accept that this signal is out of Phase 1 and implement only the new-device challenge. The
second is a real weakening of SEC-LOCK-004 and is the repository owner's call, not
implementation's. SEC-LOCK-005 is a different case and not a gap: its input is transaction
history, and Phase 1 does not have that because Phase 1 moves no money.
