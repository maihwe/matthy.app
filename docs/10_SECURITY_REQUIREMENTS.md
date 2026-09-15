# 10 — Security Requirements

**Status:** Phase 0 draft, awaiting review
**Derives from:** 01_PRODUCT_REQUIREMENTS, 02_SYSTEM_ARCHITECTURE, Master Development Prompt §25, §26, §35, §44
**Depends on:** 01, 02, 05

---

## 1. Threat model

The platform holds money and identity data for people who may have limited recourse if
either is compromised. Three adversary profiles shape the requirements:

| Adversary | Capability | Primary defences |
|---|---|---|
| **Opportunistic attacker** | Credential stuffing, scraping, automated abuse | Rate limiting, lockout, MFA, enumeration resistance |
| **Malicious insider** | Legitimate credentials, knowledge of the system | Separation of duties (05 §6), audit trail, dual control, least privilege |
| **Compromised client** | A rooted phone, a stolen device, a tampered app | Server-side authority for all financial figures, token revocation, device binding |

The third is the one that gets under-designed. §35 explicitly asks for offline collection
on devices in markets, and §44 rule 1 forbids trusting the frontend with financial
calculations. Those two together mean the app must work offline **and** be assumed
hostile — the client is a convenience layer over an API that trusts nothing it says.

### 1.1 Scope exclusions

Card data never enters the platform. Payments go through licensed providers and the
platform stores references, never PANs; this keeps the platform out of PCI-DSS scope
rather than merely compliant with it.

---

## 2. Authentication

### 2.1 Credentials

| Requirement | Detail |
|---|---|
| SEC-AUTH-001 | Passwords hashed with Argon2id (`argon2-cffi`), the current Django default. Not MD5, not SHA-*, not unsalted. |
| SEC-AUTH-002 | The transaction PIN is hashed to the same standard as the password (FR-AUTH-005). A 4-digit PIN has ~10⁴ entropy; it is protected by rate limiting and lockout, not by hash strength alone. |
| SEC-AUTH-003 | Passwords are never logged, never included in error reports, and never returned by any API. |
| SEC-AUTH-004 | Password strength is enforced at registration (minimum length plus a breached-password check against a local corpus, so no plaintext is sent to a third-party service). |
| SEC-AUTH-005 | Login accepts phone or email. Failure responses are **identical** whether the account exists or the password was wrong, to prevent account enumeration. |
| SEC-AUTH-006 | The PIN is required for financial actions and is distinct from the login password. |
| SEC-AUTH-007 | Changing password or PIN requires the current credential, and invalidates all other sessions. |

### 2.2 OTP

| Requirement | Detail |
|---|---|
| SEC-OTP-001 | OTPs are 6 digits, generated from a CSPRNG (`secrets`, never `random`). |
| SEC-OTP-002 | Stored hashed, single-use (`consumed_at`), and expiring. 10 minutes for verification; shorter for high-risk actions. |
| SEC-OTP-003 | Attempts are capped (3) per code; exceeding it consumes the code. |
| SEC-OTP-004 | Issuance is rate-limited per destination (e.g. 3 per 15 minutes, 10 per day) — an unbounded OTP endpoint is an SMS-billing attack as much as an auth weakness. |
| SEC-OTP-005 | Comparison is constant-time. |

### 2.3 Two-factor authentication

| Requirement | Detail |
|---|---|
| SEC-MFA-001 | TOTP (RFC 6238) and SMS OTP are both supported (FR-AUTH-010). |
| SEC-MFA-002 | TOTP enrolment requires confirmation with a valid code before it takes effect. |
| SEC-MFA-003 | Recovery codes are single-use and stored hashed. |
| SEC-MFA-004 | **MFA is mandatory for `SUPER_ADMIN`, `PLATFORM_ADMIN`, `TREASURER`, `GROUP_OWNER`, and `CHAIRMAN`** — the roles that can move money or change permissions. Optional for `MEMBER`. |
| SEC-MFA-005 | SMS is the weaker factor and is offered as a fallback, not as the recommended one. |

SEC-MFA-004 is the requirement most likely to be argued down for convenience, and
should not be. An account that can approve a disbursement is an account worth stealing.

### 2.4 Lockout and risk controls

| Requirement | Detail |
|---|---|
| SEC-LOCK-001 | N consecutive failed logins (default 5) locks the account for a backoff period that escalates with repetition. |
| SEC-LOCK-002 | Lockout is per-account **and** per-source-IP, so that both credential stuffing against one account and spraying across many are slowed. |
| SEC-LOCK-003 | Financial actions (disbursement approval, role change, payment initiation) require re-authentication if the session is older than a configured window. |
| SEC-LOCK-004 | Impossible-travel and new-device signals raise a step-up challenge: re-authentication plus a notification to the account's registered phone. |
| SEC-LOCK-005 | Unusual withdrawal or disbursement patterns (amount, frequency, destination novelty against the account's history) trigger review rather than silent approval. |

---

## 3. Session and token architecture

Two paths, per the Phase 0 decision: JWT for mobile, Django sessions for the web admin.
Both resolve to the same user and permission layer (05 §8), so authorisation logic
exists once.

### 3.1 Mobile — JWT

| Requirement | Detail |
|---|---|
| SEC-JWT-001 | Short-lived access token (15 min default), signed with a strong asymmetric algorithm so services can verify without holding the signing key. |
| SEC-JWT-002 | Long-lived refresh token (30 days), **stored hashed** in `user_sessions`, never in the token store as plaintext. |
| SEC-JWT-003 | **Refresh rotation.** Each refresh issues a new refresh token and invalidates the old one. |
| SEC-JWT-004 | **Reuse detection.** Presenting an already-rotated refresh token means the token was stolen or replayed. The entire session family is revoked immediately and the event is logged as a security event. |
| SEC-JWT-005 | Tokens are bound to a device record (`devices`), and revoking a device revokes its sessions (FR-AUTH-012). |
| SEC-JWT-006 | Refresh tokens are stored in platform secure storage (Keychain / Keystore), never in `SharedPreferences` or `AsyncStorage`. |
| SEC-JWT-007 | Access tokens are **not** stored in browser-accessible storage for the web admin — the admin uses sessions precisely to avoid this. |
| SEC-JWT-008 | JWT claims carry no sensitive data. A JWT is signed, not encrypted, and anyone holding it can read its payload. |
| SEC-JWT-009 | Logout, password change, PIN change, and role revocation invalidate the session family. |

SEC-JWT-004 is the requirement that makes rotation worth doing. Rotation without reuse
detection adds complexity and catches nothing; rotation *with* it converts a stolen
refresh token from a persistent compromise into a detected one.

### 3.2 Web admin — Django sessions

| Requirement | Detail |
|---|---|
| SEC-SES-001 | `HttpOnly`, `Secure`, and `SameSite=Lax` cookies. |
| SEC-SES-002 | CSRF protection enabled on all state-changing endpoints; token rotated on login. |
| SEC-SES-003 | Session fixation prevented — session key is regenerated on authentication. |
| SEC-SES-004 | Idle timeout (30 min) and absolute timeout (12 h). |
| SEC-SES-005 | `SESSION_COOKIE_NAME` is set to a non-default value, so a cookie collision with another Django app on the same domain cannot cause session confusion. |
| SEC-SES-006 | Sessions are stored server-side; the cookie carries only the key. |

### 3.3 Session visibility

A user can list and revoke their active sessions and devices (FR-AUTH-011/012), each
showing device, last activity, and approximate location. Account takeover is often
noticed by the victim before it is noticed by the system, and giving them a lever is
cheaper than detecting every intrusion.

---

## 4. Authorisation

Detail is in 05; the security-relevant requirements:

| Requirement | Detail |
|---|---|
| SEC-AUTHZ-001 | Every endpoint has an explicit permission class. Default-deny: a view with no declared permission is a test failure, not an open endpoint. |
| SEC-AUTHZ-002 | Permission checks are object-scoped `(user, group, permission)`, never role-name comparisons. |
| SEC-AUTHZ-003 | The client's claimed group or role is never trusted; scope is derived server-side from the authenticated user's memberships. |
| SEC-AUTHZ-004 | §6 separation-of-duties rules are enforced in the database, not only in application code. |
| SEC-AUTHZ-005 | Permission caches are invalidated on change (05 §8). |
| SEC-AUTHZ-006 | A revoked user's in-flight requests fail on the next check, not at token expiry. |

---

## 5. Data protection

### 5.1 In transit and at rest

| Requirement | Detail |
|---|---|
| SEC-DATA-001 | TLS 1.2+ everywhere. HSTS with a long max-age. TLS 1.0/1.1 disabled. |
| SEC-DATA-002 | Database volumes encrypted at rest; backups encrypted with a key distinct from the database's. |
| SEC-DATA-003 | Object storage for documents and images is private by default; access is via short-lived signed URLs, never public buckets. |
| SEC-DATA-004 | Sensitive fields where the platform has legitimate need for the plaintext (e.g. next-of-kin phone) are encrypted at the application layer with a managed key. |
| SEC-DATA-005 | **Credentials are never encrypted — they are hashed.** Encryption implies a reversible operation and a key that can leak. |

### 5.2 What never leaves the API

NFR-SEC-006: *"Never expose sensitive information through APIs."* The API never
returns, in any response, log line, or error payload:

- Password hashes, PIN hashes, OTP codes, verification or refresh tokens
- Full bank account or card numbers (masked to last 4 where display is required)
- Raw payment provider payloads (`provider_metadata` is sanitised on write — 03 §8.1)
- Internal integer primary keys (03 §1.1 — only `public_id` is exposed)
- Another member's PII, beyond what their shared group membership warrants
- Stack traces, SQL fragments, or framework versions in error responses

Enforcement: serialisers are explicit allow-lists, not `exclude`-lists. `ModelSerializer`
with `fields = '__all__'` is banned, because it silently publishes every column a future
migration adds.

### 5.3 Personal data

| Requirement | Detail |
|---|---|
| SEC-PII-001 | PII is collected only where a stated purpose requires it. |
| SEC-PII-002 | Access to PII is permission-gated and audit-logged. |
| SEC-PII-003 | Data subject rights — access, correction, and (where compatible with §44 rules 3 and 10) erasure or anonymisation — are supported. Erasure must not delete ledger entries; it anonymises the *person*, not the *transaction*. |
| SEC-PII-004 | PII retention limits are defined and enforced per category. |
| SEC-PII-005 | Nigeria's Data Protection Act obligations apply — see 14_REGULATORY_CHECKLIST. |

SEC-PII-003 contains the tension that has to be resolved explicitly rather than by
whoever implements it first: a data subject can demand erasure of their personal data,
but §44 rule 3 forbids deleting a completed financial transaction, and rule 10 requires
an audit trail. The reconciliation is that the *ledger entry* is immutable and the
*identity link* is severable — the entry keeps its amounts and references, and the
personal identifiers are replaced with a non-reversible token. This must be reviewed
against the applicable data protection law before it is relied on.

---

## 6. Application security

### 6.1 Injection

| Requirement | Detail |
|---|---|
| SEC-INJ-001 | All database access goes through the ORM or parameterised queries. Raw SQL with string interpolation is prohibited; `RawSQL` with user input is a reviewed exception, never a habit. |
| SEC-INJ-002 | ORM field lookups that accept user-supplied keys (e.g. `?ordering=`) are allow-listed. Django's `order_by` will happily interpolate an attacker's string. |
| SEC-INJ-003 | Query count and complexity are bounded — a filter endpoint that lets a client request an unbounded join is a denial-of-service surface. |
| SEC-INJ-004 | No `eval`, no `exec`, no `pickle` on untrusted data, no unsafe YAML loading. |

### 6.2 Output handling

| Requirement | Detail |
|---|---|
| SEC-XSS-001 | Autoescaping is never disabled in templates. Any `|safe` is justified in a comment and reviewed. |
| SEC-XSS-002 | A strict Content-Security-Policy is applied to the web admin, with no `unsafe-inline`. |
| SEC-XSS-003 | API responses are `application/json` with `X-Content-Type-Options: nosniff`, so a JSON body is never sniffed as HTML. |
| SEC-XSS-004 | User-authored content — group rules, message bodies, disbursement descriptions, variance explanations — is treated as untrusted at every render site, on every client. |
| SEC-XSS-005 | File uploads are validated by content, not by extension or client-supplied MIME type, and are served from a separate origin. |

SEC-XSS-004 matters more here than in a typical app because several of these fields are
exactly the kind that a well-meaning future feature renders as rich text —

a variance explanation quoting a member's message, a group's rules page.

### 6.3 Headers

| Requirement | Detail |
|---|---|
| SEC-HDR-001 | `Strict-Transport-Security`, `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`, `Permissions-Policy` restricting unused browser APIs. |
| SEC-HDR-002 | The API sends `Cache-Control: no-store` on authenticated responses, so financial data is not written to a shared or intermediate cache. |
| SEC-HDR-003 | Server version banners are suppressed. |

### 6.4 Rate limiting

| Scope | Suggested default | Rationale |
|---|---|---|
| Login | 10 / 15 min per IP+account | Credential stuffing |
| OTP issue | 3 / 15 min per destination | SMS-billing abuse |
| OTP verify | 5 / 15 min per code | Brute force |
| PIN entry | 5 / 15 min per user | 4-digit space is small |
| Password reset | 3 / hour per account | Mail-bombing, enumeration |
| Financial writes | 60 / min per user | Runaway clients |
| Bulk send | 1 / hour per group | Accidental mass mailing |
| Anonymous reads | 100 / min per IP | Scraping |

Implemented in Redis, keyed per scope. Limits are configuration, not constants, so
they can be tightened in response to an incident without a deploy.

---

## 7. Secrets management

| Requirement | Detail |
|---|---|
| SEC-SEC-001 | No secret in source. Ever. Not in code, not in a default value, not "temporarily". |
| SEC-SEC-002 | Secrets are supplied by environment or a managed secret store, and read at process start. |
| SEC-SEC-003 | A `.env.example` documents required variables with placeholder values; `.env` is git-ignored (committed in `.gitignore`). |
| SEC-SEC-004 | A pre-commit hook and CI check scan for secret-shaped strings. |
| SEC-SEC-005 | Secrets are distinct per environment. A staging key never opens production, and vice versa. |
| SEC-SEC-006 | Rotation is possible without downtime, and is performed on any suspected exposure and on personnel change. |
| SEC-SEC-007 | `DJANGO_SECRET_KEY` is long, random, unique per environment — and its compromise is treated as a security incident, because it forges sessions and signed tokens. |
| SEC-SEC-008 | Provider webhook signing secrets are per-provider and per-environment, never shared. |

---

## 8. Audit

Requirements and schema are in 03 §12.1 and 05; the security-relevant points:

| Requirement | Detail |
|---|---|
| SEC-AUD-001 | Authentication events — success, failure, lockout, MFA challenge, token reuse detection — are logged as security events distinct from business audit. |
| SEC-AUD-002 | Audit records carry actor, action, object, before/after values, IP, device, and timestamp (§25). |
| SEC-AUD-003 | Audit records are append-only, enforced by database trigger (03 §12.1). |
| SEC-AUD-004 | Audit logs are shipped to storage the application cannot rewrite, so a compromised application cannot erase its own tracks. |
| SEC-AUD-005 | Security events generate alerts on threshold (e.g. refresh-token reuse, repeated lockouts across accounts from one IP). |
| SEC-AUD-006 | Audit retention meets the applicable regulatory requirement (14). |

SEC-AUD-004 is the requirement that makes the trigger in 03 §12.1 meaningful. A trigger
stops the application from *casually* editing the log; shipping the log elsewhere stops
a compromised application from editing it *at all*.

---

## 9. Financial-specific controls

Cross-referenced from 03 §15, repeated here because §44 groups them as mandatory:

| # | §44 rule | Implementation |
|---|---|---|
| 1 | Never trust the frontend with financial calculations | All amounts recomputed server-side; client values are treated as a request, never as an input to arithmetic |
| 4 | Never process a payment because the client says it succeeded | Server-side provider verification (02 §5.8) |
| 5 | Never allow unauthorized role escalation | 05 §6.4, enforced by trigger |
| 6 | Never allow self-approval | 05 §6.1, enforced by trigger |
| 7 | Prevent duplicate payment processing | Unique idempotency keys; unique `(provider_code, event_id)` on webhooks |
| 9 | Use database transactions for financial operations | `transaction.atomic()` + `select_for_update()` |
| 11 | Verify payment webhooks | Signature verification where supported; server-side re-verification regardless |
| 12 | Use idempotency keys | Every financial mutation |

**Amount tampering.** Every endpoint that accepts an amount validates it against the
server's own expectation: a contribution is checked against its `contribution_schedules`
row, a loan repayment against its `loan_schedules` row, a disbursement against the
group's available balance and the approval already granted. **A client cannot name an
amount the server has not already derived a reason to accept.**

**Balance integrity as a security control.** A deliberate, well-formed attempt to move
money through the API should fail on `ledger.post_transaction`'s balance check even if
it somehow passes every permission check. The ledger's self-verification (§2.4.1 of 03)
is a security boundary, not only an accounting one.

---

## 10. Offline and device security (§35)

| Requirement | Detail |
|---|---|
| SEC-OFF-001 | Queued offline records are encrypted at rest on the device. |
| SEC-OFF-002 | The app requires device authentication (biometric or passcode) to open after a configurable interval. |
| SEC-OFF-003 | Local caches are cleared on logout, on remote revoke, and after a retention window. |
| SEC-OFF-004 | The server treats every synced record as untrusted input and re-validates it fully — an offline queue is a client-controlled payload with a delay attached. |
| SEC-OFF-005 | Tamper detection: a rooted/jailbroken device is flagged, and high-value actions may be blocked or require step-up. |
| SEC-OFF-006 | Client-supplied timestamps are recorded but never authoritative for financial ordering; the server's `posted_at` and the collector's declared `occurred_at` are distinct (03 §2.3). |

---

## 11. Security testing obligations

Carried into 13_TESTING_STRATEGY:

| # | Test |
|---|---|
| S1 | Password, PIN, and OTP values never appear in any API response, log, or error payload |
| S2 | Login failure responses are byte-identical for existing and non-existing accounts |
| S3 | Refresh-token reuse revokes the whole session family |
| S4 | A revoked permission fails on the next request, not at token expiry |
| S5 | `ModelSerializer(fields='__all__')` does not exist anywhere in the codebase |
| S6 | Every view declares an explicit permission class (static check) |
| S7 | Ordering and filter parameters reject non-allow-listed fields |
| S8 | CSRF is enforced on all session-authenticated state-changing endpoints |
| S9 | Rate limits trigger at their configured thresholds |
| S10 | Account enumeration via registration, password reset, or invitation is not possible |
| S11 | Amount tampering is rejected on every financial endpoint |
| S12 | Audit records cannot be updated or deleted through any application path |
| S13 | Webhook signature verification rejects unsigned and wrongly-signed payloads |
| S14 | A dependency vulnerability scan runs in CI and fails the build on high-severity findings |

---

## 12. Open questions

| Ref | Question | Impact |
|---|---|---|
| Q5 | KYC tiering thresholds | Determines what verification gates which transaction limits (§2.4's risk controls and §9's amount validation both key off it). Needs confirmation against current CBN requirements rather than assumption — see 14. |
| Q1 | Custody model | A `PLATFORM_CUSTODY` posture materially expands the security and compliance surface: the platform would hold funds, its accounts become attack targets, and additional controls (segregation of client funds, reconciliation attestation) apply. |
