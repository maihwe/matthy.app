# Authentication implementation

The implemented S4 slices provide these routes under `/api/v1/auth/`:

| Method | Route | Behaviour |
|---|---|---|
| POST | `register/` | Create an unverified account and request a phone code |
| POST | `otp/request/` | Resend a phone verification code, with a generic response |
| POST | `phone/verify/` | Consume the code and mark the owning phone verified |
| POST | `email/request/` | Request verification of the signed-in account's saved email |
| POST | `email/verify/` | Consume an emailed token and mark that email verified |
| POST | `password/reset/` | Request recovery through a verified phone or email |
| POST | `password/reset/confirm/` | Consume the proof and replace the password |
| POST | `password/change/` | Change a password with the current password |
| POST | `pin/set/` | Set the first transaction PIN with password reauthentication |
| POST | `pin/change/` | Replace the PIN with proof of the current PIN |
| POST | `login/` | Verify a password; issue tokens or a restricted MFA challenge |
| POST | `mfa/enable/` | Begin TOTP or verified-phone SMS enrollment |
| POST | `mfa/confirm/` | Confirm enrollment; return recovery codes once |
| POST | `mfa/challenge/` | Complete second-factor login and receive tokens |
| POST | `mfa/disable/` | Disable optional MFA with password and factor proof |
| POST | `token/refresh/` | Rotate the refresh token and issue a new access token |
| POST | `logout/` | End the current login family; cookie authentication requires CSRF |
| GET | `sessions/` | List the caller's active sessions, newest first |
| DELETE | `sessions/{public_id}/` | End that login family |
| GET | `devices/` | List the caller's registered, unrevoked devices, newest first |
| DELETE | `devices/{public_id}/` | Revoke the device and all its login families |

The generated contract is [`openapi.yaml`](openapi.yaml). Lists accept `cursor` and
`limit` (default 50, maximum 200), and use UUID identifiers. Foreign resources and
unknown identifiers both return `404`.

## Signing configuration

Set `JWT_SIGNING_KEY` to a PEM RSA private key and `JWT_VERIFYING_KEY` to its
matching PEM public key. Keys must contain at least 2048 bits. The variables accept
literal `\n` escapes for single-line environment configuration. Keys belong in
environment configuration or the deployment secret store.

`JWT_ISSUER` defaults to `ihweza`; `JWT_AUDIENCE` defaults to `ihweza-api`. Public
reads, health checks and migrations can start without signing keys. Token issuance
returns `503 service_unavailable` until a valid, matching keypair is available.

## Login and refresh

Login accepts `identifier` (phone or email), `password`, and an optional `device`:

```json
{
  "identifier": "+2348012345678",
  "password": "...",
  "device": {"fingerprint": "installation-id", "platform": "ANDROID"}
}
```

Use `device.id` to reuse a previously returned device UUID, or `device.fingerprint`
to identify an installation; supplying both is invalid. Omitted device information
creates a new web device. The response's `data` contains `access_token`,
`refresh_token`, `expires_in`, `device_id` and the user's public identity.

Access tokens expire after 15 minutes and use RS256. Their claims contain public
user and session UUIDs, timestamps, issuer, audience, token type and a random token
identifier. The server also checks the backing account, session and device on
authenticated requests, so revocation takes effect before token expiry.

Refresh tokens contain 384 random bits, expire after 30 days, and are stored only
as unique, domain-separated SHA-256 digests. Each rotation retains the login's
`family_id` and device. Reusing a rotated token revokes its family. A separate
login on the same device survives. Explicit logout or device revocation clears
rotation markers so stale tokens do not raise false reuse alerts. If signing
fails during refresh, rotation rolls back and the client can retry its old token.

Migration `accounts/0003_session_families` assigns distinct public device UUIDs,
backfills legacy families by `(user, device)`, and adds revocation reasons before
enforcing non-null identifiers. Historical rows do not contain enough information
to separate prior independent logins on one device; new logins have distinct
family UUIDs.

## Rate limits and remaining scope

Login applies both 10 attempts per 15 minutes per IP/account and 50 per 15 minutes
per IP across accounts. Refresh allows 120 requests per minute per IP. Values live
in `settings.RATE_LIMITS`. Refusals return `429` with `Retry-After`; an unavailable
Redis limiter returns `503`.

Registration and phone-code requests share a 50/hour per-IP limit. Phone
verification allows 120 requests/minute per IP, in addition to each code's three
attempts. Destination issuance limits apply to every registration/resend response,
including unknown or already-verified numbers.

## Register, verify, log in locally

1. Start the development Compose services and open Mailhog at
   `http://localhost:8025`.
2. Send `POST /api/v1/auth/register/` with `full_name`, `phone`, `password` and
   optional `email`.
3. Read the captured phone-code message addressed to `<digits>@phone.invalid`.
   Codes are never returned by the API or logged.
4. Send `POST /api/v1/auth/phone/verify/` with `phone` and the six-digit `code`.
5. Send `POST /api/v1/auth/login/` with the original phone/password pair.

Use `POST /api/v1/auth/otp/request/` with `phone` to resend. Resending invalidates
the previous code. Registering again does not change an existing password, name or email.
If delivery fails, the account remains available for resend and the undelivered
code is invalidated.

Development settings select `config.phone_verification.LocalEmailCapture`, which
uses Mailhog; tests capture messages in Django's in-memory email outbox. This
transport sends no real SMS or external email. Production has no default delivery transport and
returns `503` until `PHONE_VERIFICATION_BACKEND` is configured. A backend must
implement `check_available()` and `send(destination=..., code=..., purpose=...)`. Local capture
refuses production SMTP configurations. Real SMS/email integration and background
delivery are remaining work; the local capture transport sends synchronously.

`is_phone_verified` and `is_email_verified` are the channel-specific facts.
Successful phone verification sets the summary to `PHONE_VERIFIED`, or
`FULLY_VERIFIED` if email is already verified. It does not change account status.

## Email verification

Include an optional email address during registration. It is saved unverified;
blank or null means absent. A case-insensitive collision returns the same generic
registration response and creates no account. Registering again never changes an
existing address. Adding or changing email after registration belongs to the
future profile-change flow.

After login, send `POST /api/v1/auth/email/request/` with `{}` and the bearer token.
Read the captured `email verification token` message in Mailhog, addressed to the
saved email. Send `POST /api/v1/auth/email/verify/` with `{"token":"<captured token>"}`;
the emailed proof is sufficient and no access token or cookie is required.
Requests for accounts without email or with verified email return the same
message without sending another token. The request cannot change its destination.

Confirmation returns the public user ID, saved email, `is_email_verified: true`
and the verification summary. Email alone gives `EMAIL_VERIFIED`; both channels
give `FULLY_VERIFIED`, regardless of confirmation order. Existing sessions stay
active. Verification neither changes credentials nor unblocks a suspended/closed
account; only active accounts can confirm. Verified email then becomes available
for password recovery.

Each token contains an account UUID and 384 random bits, is stored as a
domain-separated SHA-256 digest, expires after 24 hours, and works once. The digest
binds the saved email, password, PIN and MFA version. Credential/address changes
invalidate older tokens. Resend supersedes earlier email verification proofs;
password-reset proofs have a separate purpose and lifecycle. A failed send consumes
its undelivered token. Proof consumption and verification facts commit together
under the user lock, including concurrent phone/email confirmations.

Requests allow 50/hour per IP, 3/hour per user and 3/hour per destination;
confirmation allows 120/minute per IP. Destinations are hashed before rate-key
construction. Limits are configurable in `RATE_LIMITS`; Redis outages fail closed.
Cookie requests require CSRF. Responses use `Cache-Control: no-store`; raw tokens
and addresses never enter the verification security events or business audits.
Development uses synchronous local capture. Production delivery and a frontend
link handler remain pending.

## Password recovery and transaction PINs

Request password recovery with `POST /api/v1/auth/password/reset/` and
`{"identifier": "<phone or email>"}`. Read the captured message in Mailhog. For
phone recovery, confirm using `phone`, `code` and `new_password`; for email
recovery, use `reset_token` and `new_password`. Both confirmation shapes use
`POST /api/v1/auth/password/reset/confirm/`. Success returns `204`; log in with the
new password afterwards. Recovery is available only through a verified channel
on an active account. Its public response never reveals account availability.

Authenticated password changes accept `current_password` and `new_password` at
`password/change/`. Initial PIN setup accepts `current_password` and `pin` at
`pin/set/`. Once set, replace the PIN using `current_pin` and `new_pin` at
`pin/change/`. PINs are four-digit strings; passwords must pass the configured
strength validators. Replacement credentials must differ from the current ones.

All successful credential mutations revoke every bearer login and invalidate
browser sessions. PIN changes participate in Django's session authentication
hash, including secret-key fallback verification. Existing browser sessions for
accounts without a PIN keep Django's standard hash until a PIN is set.

Recovery secrets expire after 10 minutes and can be used once. The email secret
contains 384 random bits; its stored digest also binds it to the email and current
credential state. Failed phone-code guesses remain counted after an error.
Password updates, proof consumption and session revocation commit together.
Immutable PIN audit entries contain only the actor and `pin_set` metadata; actual
PINs, passwords and hashes never enter the audit record. Browser mutations require
CSRF. Local recovery capture is synchronous; real delivery remains pending.

Recovery requests use the existing 3/hour per-account limit plus 50/hour per IP.
Confirmations allow 120/minute per IP before proof lookup. Password changes and
initial PIN setup allow five requests per 15 minutes per user. PIN changes use the
existing five-per-15-minute PIN-entry budget and account lockout.

## MFA enrollment and login

Configure `MFA_ENCRYPTION_KEYS` with a Fernet key supplied by your secret store.
There is no default or fallback to the Django/JWT keys. For local development,
generate a key with `python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`
and put it in the ignored `.env`. Missing or invalid keys return `503` for TOTP
setup/verification; recovery-code verification does not need seed decryption.

1. Log in and use the bearer token to send `mfa/enable/` with
   `{"method":"TOTP","current_password":"..."}`. TOTP is recommended. The response
   contains `secret`, `provisioning_uri` and `expires_in: 600`; add it to an
   authenticator app. SMS setup uses `method: "SMS"`, requires a verified phone,
   and captures a code in Mailhog locally.
2. Send `mfa/confirm/` with `{"code":"123456"}`. Success enables MFA, ends all
   existing sessions and returns ten `recovery_codes` once. Store them safely;
   they cannot be retrieved later. Setup permits three guesses and expires in ten minutes.
3. Log in again. The response contains `mfa_required: true`, a five-minute
   `mfa_token`, `method`, `enrollment_required: false`, and no access/refresh tokens.
   For SMS, `sms_sent` reports delivery; `false` means use a recovery code. An SMS
   outage or loss of the verified phone does not prevent recovery-code login.
4. Send `mfa/challenge/` with `mfa_token` and `code` (six authenticator/SMS digits,
   or a 20-character recovery code). Successful proof returns the normal token
   pair; refresh retains its MFA verification. A challenge permits three guesses,
   can be spent once, and is superseded by the next password login.

Staff/superuser login without enrollment returns `enrollment_required: true`.
Pass that `mfa_token` alongside the setup password to `mfa/enable/`, then alongside
the confirmation code to `mfa/confirm/`. This proof authorizes only enrollment and
confirmation. After confirmation, log in again to get a verification challenge.

TOTP uses RFC 6238 SHA-1 with six digits and 30-second steps; verification allows
one step of clock drift in either direction. Each accepted step can be used once
per account, including setup confirmation. Wait for the next code if you confirmed
setup with the current code. Recovery codes are single-use and stored only as
domain-separated SHA-256 digests of 80-bit random values. SMS codes use the same
slow password hash as other OTPs and share destination issuance limits.

Disable optional MFA with `mfa/disable/` and `current_password` plus a fresh TOTP
code or unused recovery code. SMS accounts use an unused recovery code for this
operation. Mandatory staff/superuser MFA cannot be disabled. A successful change
increments `mfa_version`, clears seeds/recovery codes, and ends every session.

MFA setup/disable allows five requests per 15 minutes per user; verification
allows ten across all proofs per 15 minutes per user. Public MFA setup/challenge
lookups allow 120 requests/minute per IP. Redis outages fail closed. Wrong guesses
commit their counters; signing failure rolls back session creation and proof
consumption so the client can retry.

Active and pending TOTP seeds use authenticated Fernet encryption. To rotate keys,
configure `MFA_ENCRYPTION_KEYS=new,old`, run `python manage.py rotate_mfa_keys`, then
remove the old key after successful verification. Keep old keys needed to restore
older encrypted backups. The command re-encrypts all seeds atomically under user
locks without changing authenticators or session hashes. This seed protection is
the explicit exception to the credential-hashing rule; other sensitive-field
encryption and deployment-managed key provisioning remain separate work.

Responses carrying seeds, recovery codes, login challenges and token pairs set
`Cache-Control: no-store`. Those values never enter security events or business audits.

## Browser admin MFA

Open `/admin/login/` and enter the staff account's phone/email and password. A
password proof redirects to `/admin/mfa/` without authenticating the cookie. Enter
a TOTP, SMS or unused recovery code to finish sign-in. Staff without MFA choose an
authenticator app or verified-phone SMS, confirm their password, and verify a code.
The setup page provides a copyable authenticator key and provisioning URI. Save
the ten recovery codes shown once, then sign in again with MFA.

The pending proof stays in a server-side anonymous Django session, expires after
five minutes and is bound to that cookie. SMS resend replaces the code without
extending the password proof. Browser and mobile proofs have separate audiences.
Session keys change after password proof and again after MFA; CSRF rotates on
completed sign-in. Browser pages carry no access or refresh tokens and are not
cached. Logout and setup/resend/cancel actions require POST and CSRF.

Migration `0005_browser_mfa` adds a `kind` discriminator, browser key and credential
digests, and challenge audience binding. Legacy rows retain `TOKEN`. Browser
rows require verified MFA and cannot authorize JWT bearer access or refresh.
Cookies use a `BROWSER` row bound to a WEB device, MFA/credential/role state and the
actual Django session key. Every request validates that row. Session/device
revocation, password/PIN/MFA changes, account suspension and role changes end
access. Verified admin cookies also authorize session-authenticated API requests
with CSRF. Password-only privileged cookies remain denied.

`SESSION_IDLE_TIMEOUT_SECONDS` defaults to 1800 and
`SESSION_ABSOLUTE_TIMEOUT_SECONDS` to 43200. Activity renews only the idle window,
bounded by the original absolute deadline. Production cookies are Secure,
HttpOnly and SameSite=Lax. Retained Django secret-key fallbacks preserve valid
cookies while their key binding moves to Django's rotated session key.

## Remaining authentication work

Forgotten-PIN recovery, sensitive profile changes and security-event shipping
remain later work. Staff, superusers and active privileged group/platform role
holders must enroll before receiving bearer sessions. The group permission model
is documented in [GROUPS.md](GROUPS.md). Unsupported legacy MFA configurations
fail closed and require operator repair.

Group and platform role changes increment `authorization_version`, derive
`role_mfa_required` from current assignments, and revoke existing bearer and browser
sessions. MFA challenges bind the authorization version too, so a proof issued
before a role change cannot create a new session. Active OWNER, CHAIRMAN and
TREASURER templates require MFA, including custom roles derived from those templates.
SUPER_ADMIN and PLATFORM_ADMIN require it globally. Removing one role preserves
the requirement when another privileged role remains.

Real SMS/email delivery and the frontend email-link handler still require
integration. Other cookie mutations retain CSRF enforcement.

## Pre-S5 verification (2026-11-06)

All ten local CI gates passed: formatting, lint, types, import contracts, migration
drift, tests, coverage, secret scan, dependency audit and OpenAPI contract matching.
The suite passed 977 tests against PostgreSQL 16 and Redis, including the complete
register/verify/login flow for both channels, verified-email recovery, PIN management, failed-guess
persistence, atomic rollback, browser and bearer revocation, delivery failure
recovery, concurrent resends, concurrent reset confirmations, MFA enrollment and
login, recovery-code/TOTP replay protection, concurrent second-factor attempts,
populated migrations, encryption-key rotation, atomic email verification rollback,
concurrent email resends/confirmations and simultaneous phone/email confirmation.
Browser coverage includes staged sign-in/enrollment, CSRF and cookie rotation,
browser/mobile proof isolation, cookie-data transplant rejection, logout/device/role
revocation, idle/absolute deadlines, session-storage rollback, cached orphan denial,
JWT/keyring independence and concurrent browser/mobile factor consumption.
The phone resend concurrency test also passed immediate repeated runs after its
test destination was isolated from Redis counters left by earlier runs.
Accounts achieved 97.49% combined
line/branch coverage against the 90% security scope floor; other measured code
achieved 95.63% line coverage against its 80% floor. Financial and
ledger coverage scopes are skipped because those modules have not been built.

The dependency audit found no known vulnerabilities after upgrading development
Pillow to 13.0.0 and pip to 26.3. `requirements/dev.txt` records the fixed minimum
versions. Hosted CI has not been run as part of this local verification.

## S5 integration verification (2026-11-06)

The full accounts/groups regression suite passed 1,034 tests and all ten local CI
gates. A final 120-test browser/MFA/concurrency/seed run includes three additional
regressions, bringing the distinct verified count to 1,037. Final accounts/groups
combined line/branch coverage is 95.90%; other code has 96.01% line coverage.
Role changes revoke bearer/browser sessions and pending proofs, while untouched
zero-version legacy browser digests remain valid. See [GROUPS.md](GROUPS.md).
