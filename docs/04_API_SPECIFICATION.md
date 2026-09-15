# 04 — API Specification

**Status:** Phase 0 draft, awaiting review
**Derives from:** 01_PRODUCT_REQUIREMENTS, 03_DATABASE_DESIGN, 05_ROLE_PERMISSION_MATRIX, Master Development Prompt §19, §29, §43, §44
**Depends on:** 03, 05

---

## 1. Conventions

Every endpoint obeys these. They exist so that a client author writes error handling,
pagination, and money formatting **once**.

| Concern | Convention |
|---|---|
| Base path | `/api/v1/` |
| Content type | `application/json` for both request and response; `multipart/form-data` for uploads only |
| Auth (mobile) | `Authorization: Bearer <access_token>` |
| Auth (web admin) | Session cookie + `X-CSRFToken` |
| Identifiers | UUID `public_id`, never the internal integer (03 §1.1) |
| Money | **Decimal string**, e.g. `"1000.00"` — never a JSON number (§1.3) |
| Timestamps | ISO 8601 with UTC offset, e.g. `"2026-09-15T08:14:22+00:00"` |
| Dates | `"YYYY-MM-DD"` |
| Currency | ISO 4217, e.g. `"NGN"` |
| Pagination | Cursor-based (§1.4) |
| Idempotency | `Idempotency-Key` header on all financial writes (§1.5) |
| Errors | A single envelope (§1.2) |
| Documentation | OpenAPI 3.1, generated from code, served at `/api/schema/` and `/api/docs/` |

### 1.1 Versioning

`/api/v1/`. The API version moves independently of the application version and changes
only on a **breaking** contract change. §43 requires frontend and backend to work from
the same contract; a contract that changes without a version change is a contract that
breaks clients silently.

### 1.2 Error envelope

Every error — validation, permission, conflict, server — uses one shape:

```json
{
  "error": {
    "code": "insufficient_permission",
    "message": "You do not have permission to approve this disbursement.",
    "details": [
      { "field": "status", "issue": "must be in APPROVAL to be approved" }
    ],
    "request_id": "req_01J8XK2M4P"
  }
}
```

| HTTP | `code` | Meaning |
|---|---|---|
| 400 | `validation_error` | Malformed or invalid request |
| 401 | `authentication_required` | Missing or invalid credentials |
| 403 | `insufficient_permission` | Authenticated, not authorised |
| 403 | `separation_of_duties` | A §44 rule blocked the action (self-approval, self-escalation) |
| 404 | `not_found` | No such resource, **or** no permission to know it exists |
| 409 | `conflict` | State conflict — duplicate reference, invalid transition |
| 409 | `idempotency_conflict` | Key reused with a different payload (§1.5) |
| 422 | `unprocessable` | Well-formed but violates a business rule |
| 429 | `rate_limited` | Includes `Retry-After` |
| 500 | `internal_error` | No internals leaked (10 §5.2) |

`403 insufficient_permission` and `404 not_found` are deliberately ambiguous with each
other for resources the caller has no standing in. Revealing that a group exists to
someone with no access to it is an information leak; the API returns `404` in both cases.

`separation_of_duties` is its own code because it is the one 403 a client should never
offer a retry for — no permission change will make it succeed (05 §6).

### 1.3 Money

Following §43's example, money is a **decimal string**:

```json
{ "amount": "1000.00" }
```

A JSON number is rejected on input. `1000.00` and `1000` are different JSON values with
the same meaning, floats lose precision, and some runtimes silently turn a large integer
into a float on parsing. A string is unambiguous, and it is what the spec's own contract
example uses.

Internally this is parsed to integer minor units at the serialiser boundary and never
before, never after, and never anywhere else (02 §5.1).

### 1.4 Pagination

Cursor-based, because offset pagination degrades badly on the large, constantly-growing
tables this API reads most — contributions, ledger entries, notifications.

```json
{
  "data": [ ... ],
  "pagination": {
    "next_cursor": "eyJpZCI6MTg0Mn0",
    "has_more": true,
    "limit": 50
  }
}
```

`?limit=` (default 50, max 200) and `?cursor=`. Cursors are opaque and must not be
constructed by clients.

### 1.5 Idempotency

Every financial write requires an `Idempotency-Key` header. Missing it is a `400`, not a
silent accept — a client that cannot retry safely should find that out in development.

```
POST /api/v1/contributions/
Idempotency-Key: 4f8a1c2e-...
```

Rules:

- The key is scoped to `(user, endpoint)`.
- A replay with the **same** key returns the original response with
  `Idempotency-Replayed: true`, and executes nothing (03 §12.2).
- A replay with the same key and a **different** body returns `409 idempotency_conflict`.
  That is a client bug, and surfacing it is more useful than honouring whichever version
  arrived second.
- Keys expire after 24 hours. Beyond that, the natural uniqueness constraints
  (`reference` on contributions, payments, settlements) remain the backstop.

### 1.6 Filtering, ordering, and field selection

- Filtering: `?status=ACTIVE&scheme=<uuid>&collected_after=2026-09-01`.
- Ordering: `?ordering=-occurred_at`. **Allow-listed per endpoint** (10 §6.1) — an
  arbitrary `ordering` value is a denial-of-service surface.
- Sparse fields: `?fields=id,amount,status` where a client needs a narrow payload.
- Every list endpoint declares its filters in the OpenAPI schema.

### 1.7 Response envelope

| Shape | Used by |
|---|---|
| `{ "data": {...} }` | Single resource |
| `{ "data": [...], "pagination": {...} }` | Collection |
| `{ "error": {...} }` | Errors |

The envelope is uniform so that a client's response interceptor is written once. The
cost is one level of nesting; the benefit is that adding pagination metadata later does
not break every client.

---

## 2. Authentication — `/api/v1/auth/`

| Method | Path | Purpose | Auth |
|---|---|---|---|
| `POST` | `/auth/register/` | Register with name, phone, password | None |
| `POST` | `/auth/login/` | Authenticate; returns tokens | None |
| `POST` | `/auth/logout/` | Revoke the current session family | Any |
| `POST` | `/auth/token/refresh/` | Exchange a refresh token | Refresh token |
| `POST` | `/auth/password/reset/` | Request a reset | None |
| `POST` | `/auth/password/reset/confirm/` | Complete a reset | Reset token |
| `POST` | `/auth/password/change/` | Change password | Any + current password |
| `POST` | `/auth/pin/set/` | Set the transaction PIN | Any |
| `POST` | `/auth/pin/change/` | Change the PIN | Any + current PIN |
| `POST` | `/auth/otp/request/` | Issue an OTP | None / Any |
| `POST` | `/auth/otp/verify/` | Verify an OTP | None / Any |
| `POST` | `/auth/email/verify/` | Verify email with a token | Token |
| `POST` | `/auth/phone/verify/` | Verify phone with an OTP | OTP |
| `POST` | `/auth/mfa/enable/` | Begin MFA enrolment | Any |
| `POST` | `/auth/mfa/confirm/` | Confirm enrolment with a code | Any |
| `POST` | `/auth/mfa/disable/` | Disable MFA | Any + password |
| `POST` | `/auth/mfa/challenge/` | Complete an MFA challenge at login | MFA token |
| `GET` | `/auth/sessions/` | List active sessions | Any |
| `DELETE` | `/auth/sessions/{id}/` | Revoke a session | Any |
| `GET` | `/auth/devices/` | List registered devices | Any |
| `DELETE` | `/auth/devices/{id}/` | Revoke a device | Any |

### 2.1 `POST /auth/register/`

```json
{ "full_name": "Adaeze Okonkwo", "phone": "+2348012345678", "password": "..." }
```

`201`:

```json
{
  "data": {
    "id": "9f1c...",
    "full_name": "Adaeze Okonkwo",
    "phone": "+2348012345678",
    "account_status": "ACTIVE",
    "verification_status": "UNVERIFIED"
  }
}
```

### 2.2 `POST /auth/login/`

```json
{ "identifier": "+2348012345678", "password": "..." }
```

`200`:

```json
{
  "data": {
    "access_token": "...",
    "refresh_token": "...",
    "expires_in": 900,
    "user": { "id": "9f1c...", "full_name": "Adaeze Okonkwo", "mfa_required": false }
  }
}
```

When MFA is enabled, `mfa_required: true` is returned with an `mfa_token` and no access
token. The client completes `/auth/mfa/challenge/`.

**Failure responses are identical** whether the account exists or the password is wrong
(10 §2.1) — same code, same message, same timing.

### 2.3 `POST /auth/token/refresh/`

```json
{ "refresh_token": "..." }
```

`200` returns a **new** access token and a **new** refresh token. The old refresh token
is invalidated (10 §3.1). Presenting an already-rotated token revokes the entire session
family and returns `401 authentication_required`.

---

## 3. Users — `/api/v1/users/`

| Method | Path | Purpose | Permission |
|---|---|---|---|
| `GET` | `/users/me/` | Own profile | Authenticated |
| `PATCH` | `/users/me/` | Update own profile | Authenticated |
| `POST` | `/users/me/avatar/` | Upload a profile picture | Authenticated |
| `GET` | `/users/me/dashboard/` | Member dashboard aggregate | Authenticated |
| `GET` | `/users/{id}/` | View a user | Shared group membership, or `platform.manage_users` |
| `GET` | `/users/` | List users | `platform.manage_users` |
| `POST` | `/users/{id}/suspend/` | Suspend an account | `platform.suspend_account` |
| `POST` | `/users/{id}/reinstate/` | Reinstate an account | `platform.suspend_account` |

`GET /users/me/dashboard/` returns exactly what §31 requires:

```json
{
  "data": {
    "total_contributions": "45000.00",
    "current_schemes": 2,
    "todays_due_amount": "1000.00",
    "outstanding_contribution": "3000.00",
    "active_loan": {
      "id": "...", "outstanding_balance": "12000.00", "next_payment": "2026-09-20"
    },
    "next_loan_repayment": { "amount": "2000.00", "due_date": "2026-09-20" },
    "recent_transactions": [ ... ]
  }
}
```

Every figure is server-computed (§44 rule 1). No client calculates a balance.

---

## 4. Locations — `/api/v1/locations/`

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/locations/countries/` | List countries |
| `GET` | `/locations/countries/{code}/types/` | List that country's location tiers |
| `GET` | `/locations/` | List children of a parent, or search |
| `GET` | `/locations/{id}/` | Detail, with `children` and `group_count` |
| `GET` | `/locations/{id}/groups/` | Groups hosted at a street (FR-LOC-004) |
| `POST`/`PATCH`/`DELETE` | `/locations/` … | `platform.manage_locations` |

`GET /locations/?parent=<uuid>&type=STREET&search=opebi&limit=20` returns a paginated
tree level. `GET /locations/{id}/groups/` is the endpoint behind §50's
find-location → find-street → find-group flow.

---

## 5. Groups — `/api/v1/groups/`

| Method | Path | Purpose | Permission |
|---|---|---|---|
| `POST` | `/groups/` | Create a group | `groups.create` |
| `GET` | `/groups/` | List groups the caller belongs to | Authenticated |
| `GET` | `/groups/{id}/` | Group detail | `groups.view` |
| `PATCH` | `/groups/{id}/` | Update | `groups.update` |
| `POST` | `/groups/{id}/archive/` | Archive | `groups.archive` |
| `POST` | `/groups/{id}/transfer-ownership/` | Transfer ownership | `groups.transfer_ownership` |
| `GET` | `/groups/{id}/members/` | Member list | `members.view` |
| `POST` | `/groups/{id}/members/invite/` | Invite | `members.invite` |
| `POST` | `/groups/{id}/members/{mid}/approve/` | Approve a membership | `members.approve` |
| `POST` | `/groups/{id}/members/{mid}/suspend/` | Suspend | `members.suspend` |
| `POST` | `/groups/{id}/members/{mid}/remove/` | Remove | `members.remove` |
| `GET` | `/groups/{id}/members/{mid}/history/` | Membership history | `members.view_history` |
| `GET`/`POST` | `/groups/{id}/roles/` | List / create roles | `groups.manage_settings` |
| `PUT` | `/groups/{id}/roles/{rid}/permissions/` | Set role permissions | `groups.manage_settings` |
| `POST` | `/groups/{id}/members/{mid}/roles/` | Assign a role | `members.assign_roles` |
| `DELETE` | `/groups/{id}/members/{mid}/roles/{rid}/` | Revoke a role | `members.assign_roles` |
| `GET`/`PUT` | `/groups/{id}/settings/` | Group settings | `groups.manage_settings` |
| `GET`/`PUT` | `/groups/{id}/rules/` | Written group rules | `groups.manage_rules` |
| `GET` | `/groups/{id}/dashboard/` | Group admin dashboard | `reports.view_group` |

`POST /groups/{id}/members/{mid}/roles/` returns
`403 separation_of_duties` when a caller attempts to grant a role they do not hold
themselves (05 §6.4), and `422` when the operation would remove the group's last owner
(05 §6.6).

---

## 6. Contribution schemes — `/api/v1/schemes/`

| Method | Path | Purpose | Permission |
|---|---|---|---|
| `POST` | `/schemes/` | Create a scheme | `schemes.create` |
| `GET` | `/schemes/` | List (\(?group=\)) | `schemes.view` |
| `GET` | `/schemes/{id}/` | Detail | `schemes.view` |
| `PATCH` | `/schemes/{id}/` | Update | `schemes.update` |
| `POST` | `/schemes/{id}/close/` | Close or archive | `schemes.close` |
| `GET`/`POST` | `/schemes/{id}/members/` | List / enrol members | `schemes.manage_members` |
| `DELETE` | `/schemes/{id}/members/{mid}/` | Remove a member | `schemes.manage_members` |
| `GET` | `/schemes/{id}/schedule/` | Expected-contribution schedule | `schemes.view` |
| `GET` | `/schedule-patterns/` | Available frequencies | Authenticated |

`GET /schedule-patterns/` exposes the frequency model (03 §6.1) so a client renders the
six required frequencies — and any future one — without a hard-coded list.

### 6.1 `POST /schemes/`

```json
{
  "group": "b71e...",
  "name": "Opebi Daily Contribution",
  "description": "Daily savings for Opebi traders",
  "contribution_pattern": "DAILY",
  "disbursement_pattern": "MONTHLY",
  "amount": "1000.00",
  "minimum_amount": "500.00",
  "maximum_amount": "5000.00",
  "currency": "NGN",
  "start_date": "2026-10-01",
  "end_date": null,
  "grace_period_days": 3,
  "collection_methods": ["CASH_COLLECTOR", "BANK_TRANSFER"]
}
```

`contribution_pattern` and `disbursement_pattern` are independent fields (FR-SCH-004), so
daily contributions with monthly disbursement is expressible without a special case.

`PATCH` on a scheme with existing contributions returns `422` for financial fields
(FR-SCH-008) — the scheme is closed and a successor created instead, so historical
schedules stay reproducible.

---

## 7. Contributions — `/api/v1/contributions/`

| Method | Path | Purpose | Permission |
|---|---|---|---|
| `POST` | `/contributions/` | Record a contribution | `contributions.record` |
| `GET` | `/contributions/` | List, filtered | `contributions.view_own` / `view_group` |
| `GET` | `/contributions/{id}/` | Detail | As above |
| `POST` | `/contributions/{id}/verify/` | Verify | `contributions.verify` |
| `POST` | `/contributions/{id}/reverse/` | Reverse | `contributions.reverse` |
| `GET` | `/contributions/{id}/receipt/` | Receipt | Own, or `view_group` |
| `POST` | `/contributions/{id}/receipt/resend/` | Resend receipt | Own |

### 7.1 The §43 contract

§43 specifies this endpoint exactly. It is reproduced here as the frozen contract:

**Request**

```http
POST /api/v1/contributions/
Idempotency-Key: unique-client-generated-key
```

```json
{
  "scheme_id": "12",
  "amount": "1000.00",
  "idempotency_key": "unique-client-generated-key"
}
```

**Response**

```json
{
  "id": "1842",
  "reference": "CON-20260915-1842",
  "amount": "1000.00",
  "status": "completed"
}
```

### 7.2 How this document refines §43's example

§43's example is the contract; three details are made explicit here so that the client
and server cannot diverge on them:

1. **`scheme_id` and `id` are UUID public identifiers** (`"9f1c..."`), not integers.
   §43's example shows `12` and `1842`; exposing sequential integers is enumerable and
   forbidden by 03 §1.1. The *shape* — an opaque identifier the client passes through —
   is exactly as specified; only the representation is refined.
2. **`idempotency_key` appears both as a header and in the body.** The header is
   canonical (§1.5); the body field is accepted for compatibility with §43's example.
   If both are present and differ, the request is rejected with
   `409 idempotency_conflict`.
3. **`reference` is server-generated** and stable across replays. A client that retries
   receives the same `reference` and the same `id` — it never needs to invent one.

### 7.3 Full request shape

```json
{
  "scheme_id": "b71e...",
  "member_id": "9f1c...",
  "amount": "1000.00",
  "payment_method": "CASH_COLLECTOR",
  "idempotency_key": "...",
  "occurred_at": "2026-09-15T08:14:22+00:00",
  "notes": "Paid in cash at Opebi market"
}
```

`member_id` is required when the caller has `contributions.record` and is recording on
behalf of another member; it defaults to the caller otherwise.

`occurred_at` is **when the money changed hands**, distinct from the server's
`posted_at` (03 §2.3). It defaults to now and is validated against a sane window — a
contribution cannot be backdated beyond the scheme's start or forward into the future.

### 7.4 Amount validation

The server validates `amount` against the member's `contribution_schedules` row and the
scheme's min/max bounds. **The client cannot name an amount the server has no derived
reason to accept** (10 §9). A partial payment is accepted and recorded as `PARTIAL`
against the schedule, with the outstanding amount retained (FR-CLT-005).

### 7.5 Reverse

```json
{ "reason": "Recorded against the wrong member", "approved_by": null }
```

Creates a reversing ledger transaction referencing the original (03 §2.5). The original
contribution is never deleted; its status becomes `REVERSED`. A reversal that exceeds
the caller's authority requires `approved_by` (05 §6.3).

---

## 8. Collectors and settlement — `/api/v1/collectors/`

| Method | Path | Purpose | Permission |
|---|---|---|---|
| `GET` | `/collectors/dashboard/` | Collector's day | `settlements.submit` |
| `GET` | `/collectors/assignments/` | Own assignments | `settlements.submit` |
| `POST` | `/collectors/assignments/` | Assign a collector | `collectors.assign` |
| `GET` | `/collectors/` | List collectors in group | `collectors.view` |
| `GET` | `/collectors/{id}/performance/` | Per-collector metrics | `collectors.view` |
| `POST` | `/settlements/` | Submit a settlement | `settlements.submit` |
| `GET` | `/settlements/` | List settlements | `settlements.view_own` / `view_group` |
| `GET` | `/settlements/{id}/` | Detail | As above |
| `POST` | `/settlements/{id}/verify/` | Verify | `settlements.verify` |
| `POST` | `/settlements/{id}/dispute/` | Dispute | `settlements.dispute` |
| `POST` | `/settlements/{id}/resolve-variance/` | Explain and resolve a variance | `settlements.resolve_variance` |

### 8.1 `GET /collectors/dashboard/`

Exactly what §9 requires:

```json
{
  "data": {
    "date": "2026-09-15",
    "scheme": { "id": "b71e...", "name": "Opebi Daily Contribution" },
    "expected_total": "82000.00",
    "collected_total": "76000.00",
    "outstanding_total": "6000.00",
    "member_count": 82,
    "members": [
      {
        "member_id": "9f1c...",
        "member_name": "Adaeze Okonkwo",
        "expected_amount": "1000.00",
        "payment_status": "PAID",
        "amount_collected": "1000.00",
        "collected_at": "2026-09-15T07:42:10+00:00",
        "payment_method": "CASH_COLLECTOR",
        "reference": "CON-20260915-1842",
        "outstanding_amount": "0.00"
      },
      {
        "member_id": "3a2b...",
        "member_name": "Chidi Nwosu",
        "expected_amount": "1000.00",
        "payment_status": "PARTIAL",
        "amount_collected": "500.00",
        "collected_at": "2026-09-15T07:44:02+00:00",
        "payment_method": "CASH_COLLECTOR",
        "reference": "CON-20260915-1843",
        "outstanding_amount": "500.00"
      }
    ]
  }
}
```

Every field §9 lists is present. This endpoint is optimised for the collector's
real-world loop — it is the one that must work on a poor connection in a market, so it
is cacheable by the client (§35) and its payload is bounded to a single day and scheme.

### 8.2 `POST /settlements/`

```json
{
  "scheme_id": "b71e...",
  "period_start": "2026-09-15",
  "period_end": "2026-09-15",
  "submitted_amount": "76000.00",
  "variance_explanation": "Two members travelled; will collect tomorrow.",
  "contribution_ids": ["1842", "1843"],
  "idempotency_key": "..."
}
```

The server computes `expected_amount` from the settlement's contributions and derives
`variance_amount`. **A client-supplied `expected_amount` is ignored** (§44 rule 1).

When the derived variance is non-zero, `variance_explanation` is **required**. §10:
*"The system must flag the difference and require an explanation."* Omitting it returns
`422`, and the database refuses to move the settlement to `SUBMITTED` or `VERIFIED`
without one (03 §7.2).

Response:

```json
{
  "data": {
    "id": "c93f...",
    "reference": "SET-20260915-0042",
    "expected_amount": "82000.00",
    "submitted_amount": "76000.00",
    "variance_amount": "-6000.00",
    "status": "SUBMITTED",
    "requires_explanation": true
  }
}
```

§10's example arithmetic — Expected ₦82,000, Submitted ₦76,000, Difference ₦6,000 —
appears in the contract as a signed `variance_amount` so that both directions (short and
over) are representable without a separate field.

---

## 9. Payments — `/api/v1/payments/`

| Method | Path | Purpose | Permission |
|---|---|---|---|
| `POST` | `/payments/initiate/` | Begin a payment | Authenticated |
| `GET` | `/payments/{id}/` | Payment status | Own, or `ledger.view_group` |
| `GET` | `/payments/` | List payments | Own, or group-scoped |
| `POST` | `/payments/{id}/verify/` | Force server-side verification | Authenticated |
| `POST` | `/payments/{id}/refund/` | Refund | `disbursements.approve` |
| `GET` | `/payments/providers/` | Available methods for a scheme | Authenticated |

### 9.1 `POST /payments/initiate/`

```json
{
  "purpose": "CONTRIBUTION",
  "contribution_id": "1842",
  "method": "PAYMENT_GATEWAY",
  "idempotency_key": "..."
}
```

The amount is **not** supplied by the client — it is read from the referenced
contribution (10 §9).

Response:

```json
{
  "data": {
    "payment_id": "e12a...",
    "status": "PENDING",
    "redirect_url": "https://checkout.provider.example/...",
    "provider_reference": "..."
  }
}
```

### 9.2 Status is never client-reported

There is no endpoint that accepts "the payment succeeded". `POST /payments/{id}/verify/`
triggers server-side verification with the provider (FR-PAY-003) and is a **hint** — the
webhook is what actually concludes the payment, and verification is idempotent so a
client polling does no harm.

### 9.3 Webhooks — `/api/v1/webhooks/{provider}/`

| Method | Path | Auth |
|---|---|---|
| `POST` | `/webhooks/paystack/` | Provider signature |
| `POST` | `/webhooks/{provider}/` | Provider signature |

Not under the authenticated API surface — the caller is a provider, not a user. The
endpoint:

1. Verifies the provider's signature (FR-PAY-004). Invalid is `401` and is logged.
2. Inserts into `payment_webhooks` with `UNIQUE (provider_code, event_id)`. A duplicate
   is a **no-op returning `200`** — providers retry, and a retry that returns an error
   gets retried again.
3. Enqueues processing. **The webhook handler does no synchronous financial work** —
   it records and returns, so a slow provider cannot time out the endpoint.
4. Returns `200` immediately for accepted events.

```json
{ "status": "accepted", "event_id": "evt_..." }
```

---

## 10. Ledger — `/api/v1/ledger/`

| Method | Path | Purpose | Permission |
|---|---|---|---|
| `GET` | `/ledger/accounts/` | Chart of accounts | `ledger.view_group` / `view_platform` |
| `GET` | `/ledger/accounts/{id}/` | Account detail | As above |
| `GET` | `/ledger/accounts/{id}/entries/` | Entry history | As above |
| `GET` | `/ledger/accounts/{id}/balance/` | Derived balance | As above |
| `GET` | `/ledger/transactions/` | Journal | As above |
| `GET` | `/ledger/transactions/{id}/` | Transaction with its entries | As above |
| `POST` | `/ledger/transactions/{id}/reverse/` | Reverse | `ledger.reverse_transaction` |
| `POST` | `/ledger/adjustments/` | Manual adjusting entry | `ledger.post_adjustment` — **dual control** |
| `GET` | `/ledger/statement/` | Statement for a member/scheme/group | `ledger.view_own` / `view_group` |

### 10.1 `GET /ledger/accounts/{id}/balance/`

```json
{
  "data": {
    "account_id": "...",
    "account_code": "2000",
    "currency": "NGN",
    "opening_balance": "0.00",
    "total_credits": "450000.00",
    "total_debits": "120000.00",
    "current_balance": "330000.00",
    "as_of": "2026-09-15T10:00:00+00:00",
    "derived_from_entries": 1847
  }
}
```

`derived_from_entries` is the count of entries the figure was computed from, so a caller
can distinguish "balance is zero" from "no entries exist". §13's
opening + contributions + credits − disbursements − refunds − debits = current balance
is exactly this response.

**There is no `PATCH` or `PUT` on a balance.** Balances are derived (03 D4); the absence
of a write endpoint is the contract expressing that. `ledger.post_adjustment` is the
only way to change a balance other than by a business event, and it takes a reason and
a second approver.

### 10.2 `POST /ledger/adjustments/`

```json
{
  "entries": [
    { "account_id": "...", "direction": "DEBIT",  "amount": "500.00" },
    { "account_id": "...", "direction": "CREDIT", "amount": "500.00" }
  ],
  "reason": "Correcting a mis-posted contribution from 2026-09-01",
  "idempotency_key": "..."
}
```

Returns `403 separation_of_duties` if the approver is the proposer (05 §6.5), and `422`
if the entries do not sum to zero — the API's first line before the database's
constraint trigger (03 §2.4.1).

---

## 11. Loans — `/api/v1/loans/`

| Method | Path | Purpose | Permission |
|---|---|---|---|
| `GET` | `/loans/eligibility/` | Caller's eligibility for a group | `loans.apply` |
| `POST` | `/loans/applications/` | Apply | `loans.apply` |
| `GET` | `/loans/applications/` | List | `loans.view_own` / `view_group` |
| `GET` | `/loans/applications/{id}/` | Detail | As above |
| `POST` | `/loans/applications/{id}/submit/` | Submit a draft | `loans.apply` |
| `POST` | `/loans/applications/{id}/review/` | Review and recommend | `loans.review` |
| `POST` | `/loans/applications/{id}/approve/` | Approve | `loans.approve` |
| `POST` | `/loans/applications/{id}/reject/` | Reject | `loans.reject` |
| `POST` | `/loans/applications/{id}/cancel/` | Cancel | `loans.apply` |
| `POST` | `/loans/applications/{id}/documents/` | Attach a document | `loans.apply` |
| `GET` | `/loans/` | List loans | `loans.view_own` / `view_group` |
| `GET` | `/loans/{id}/` | Loan detail | As above |
| `GET` | `/loans/{id}/schedule/` | Repayment schedule | As above |
| `GET` | `/loans/{id}/history/` | Loan history | As above |
| `POST` | `/loans/{id}/repayments/` | Record a repayment | `contributions.record` or own loan |
| `GET` | `/loans/{id}/repayments/` | List repayments | As above |
| `GET` | `/guarantors/requests/` | Guarantor requests directed at the caller | `guarantors.respond` |
| `POST` | `/guarantors/requests/{id}/respond/` | Accept or decline | `guarantors.respond` |
| `GET` | `/guarantors/` | Guarantor records for a group | `guarantors.view` |
| `GET`/`PUT` | `/groups/{id}/loan-rules/` | Group lending rules | `loans.configure_rules` |

### 11.1 `GET /loans/eligibility/`

```json
{
  "data": {
    "group_id": "b71e...",
    "eligible": true,
    "max_amount": "50000.00",
    "factors": [
      { "factor": "membership_duration_months", "value": 14, "required": 6, "met": true },
      { "factor": "contribution_consistency", "value": 0.94, "required": 0.80, "met": true },
      { "factor": "outstanding_debt", "value": "0.00", "max": "0.00", "met": true },
      { "factor": "guarantors_required", "value": 2, "met": null }
    ],
    "evaluated_at": "2026-09-15T10:00:00+00:00"
  }
}
```

§15 requires the eligibility engine be configurable and its factors be visible. Returning
the factor breakdown is what makes "why was I rejected?" answerable — and returning it
**without** returning the group's raw rule configuration preserves the group's privacy
about its lending policy.

### 11.2 `POST /loans/applications/`

```json
{
  "group_id": "b71e...",
  "amount_requested": "40000.00",
  "purpose": "Restock my shop for the festive season",
  "duration_periods": 6,
  "repayment_pattern": "MONTHLY",
  "proposed_repayment_amount": "7200.00",
  "guarantor_ids": ["3a2b...", "7c4d..."],
  "idempotency_key": "..."
}
```

Every field §14 requires. `supporting_documents` are attached via the documents
sub-resource.

The server records an `eligibility_snapshot` at submission (03 §9.1) so that a decision
remains explicable after the group changes its rules.

### 11.3 `POST /guarantors/requests/{id}/respond/`

```json
{ "decision": "APPROVED", "comment": "Happy to guarantee." }
```

`403` when the caller is not the named guarantor, and `403` when the request falls
outside their permission scope (FR-LOAN-010). The decision records IP and device (§16).

### 11.4 `POST /loans/{id}/repayments/`

```json
{
  "amount": "7200.00",
  "payment_method": "CASH_COLLECTOR",
  "schedule_id": "...",
  "occurred_at": "2026-09-20T09:00:00+00:00",
  "idempotency_key": "..."
}
```

The server allocates the payment across principal, interest, fees, and penalties
according to the loan's schedule, and the resulting components sum to `amount` by
constraint (03 §9.6). **The client does not supply the split** — allocation is a
financial calculation (§44 rule 1).

---

## 12. Disbursements — `/api/v1/disbursements/`

| Method | Path | Purpose | Permission |
|---|---|---|---|
| `POST` | `/disbursements/` | Request | `disbursements.request` |
| `GET` | `/disbursements/` | List | `disbursements.view` |
| `GET` | `/disbursements/{id}/` | Detail | `disbursements.view` |
| `POST` | `/disbursements/{id}/review/` | Review | `disbursements.review` |
| `POST` | `/disbursements/{id}/approve/` | Approve | `disbursements.approve` |
| `POST` | `/disbursements/{id}/reject/` | Reject | `disbursements.approve` |
| `POST` | `/disbursements/{id}/process/` | Execute the payment leg | `disbursements.process_payment` |
| `POST` | `/disbursements/{id}/verify/` | Verify completion | `disbursements.verify` |
| `POST` | `/disbursements/{id}/cancel/` | Cancel | `disbursements.request` |
| `GET` | `/disbursements/{id}/approvals/` | Approval trail | `disbursements.view` |

### 12.1 `POST /disbursements/`

```json
{
  "group_id": "b71e...",
  "disbursement_type": "MONTHLY_GROUP_PAYOUT",
  "amount": "50000.00",
  "payee_user_id": "9f1c...",
  "description": "September monthly payout",
  "idempotency_key": "..."
}
```

Returns `422` when the amount exceeds the group's available balance, and `403
separation_of_duties` when the approver is the requester — including when the same user
holds both `disbursements.request` and `disbursements.approve` and attempts both steps
on one request (05 §6.1).

The status enum follows §18's workflow exactly: `REQUEST` → `REVIEW` → `APPROVAL` →
`PAYMENT_PROCESSING` → `VERIFICATION` → `COMPLETED`. An invalid transition is `409`.

---

## 13. Notifications and communications — `/api/v1/`

| Method | Path | Purpose | Permission |
|---|---|---|---|
| `GET` | `/notifications/` | Caller's notifications | Authenticated |
| `POST` | `/notifications/{id}/read/` | Mark read | Authenticated |
| `POST` | `/notifications/read-all/` | Mark all read | Authenticated |
| `GET`/`PUT` | `/notifications/preferences/` | Notification preferences | Authenticated |
| `POST` | `/devices/push-token/` | Register a push token | Authenticated |
| `GET`/`POST` | `/communications/campaigns/` | List / create a campaign | `communications.bulk_send` |
| `GET` | `/communications/campaigns/{id}/` | Campaign status | `communications.bulk_send` |
| `POST` | `/communications/campaigns/{id}/send/` | Enqueue a campaign | `communications.bulk_send` |
| `GET` | `/communications/campaigns/{id}/recipients/` | Per-recipient delivery | `communications.view_history` |
| `GET` | `/communications/audience-estimate/` | Count a prospective audience | `communications.bulk_send` |
| `POST` | `/messages/` | Post a message | `communications.message_group` |
| `GET` | `/messages/` | Caller's messages | Authenticated |
| `GET`/`POST` | `/communications/templates/` | Templates | `communications.manage_templates` |

### 13.1 `POST /communications/campaigns/`

```json
{
  "subject": "Contribution reminder",
  "body": "...",
  "channel": "EMAIL",
  "audience_type": "OUTSTANDING_CONTRIBUTIONS",
  "audience_filter": { "group_id": "b71e...", "scheme_id": null }
}
```

Every audience in §21 is a supported `audience_type` (03 §11.3).

`POST .../send/` **enqueues** and returns `202 Accepted` — bulk email is processed
asynchronously and never inside the request (FR-COM-003, §21).

A caller scoped to a group receives `403` when `audience_filter` names a group they have
no standing in (FR-COM-007). The audience is resolved **server-side** from the filter;
a client cannot supply a recipient list directly.

---

## 14. Reports — `/api/v1/reports/`

| Method | Path | Purpose | Permission |
|---|---|---|---|
| `GET` | `/reports/contributions/` | Contribution report | `reports.view_own` / `view_group` |
| `GET` | `/reports/members/{id}/` | Member report | As above |
| `GET` | `/reports/groups/{id}/` | Group report | `reports.view_group` |
| `GET` | `/reports/collectors/{id}/` | Collector report | As above |
| `GET` | `/reports/collections/performance/` | Collection performance | As above |
| `POST` | `/reports/export/` | Request an export | `reports.export` |
| `GET` | `/reports/exports/{id}/` | Export status and download | `reports.export` |

Reports cover exactly §24's four categories. Every figure is scoped to the caller's
permissions (FR-RPT-005) — a group report never includes another group's data.

**Large reports are generated asynchronously**: `POST /reports/export/` returns `202`
with an export id; the client polls or receives a notification.

---

## 15. Contract change policy

§43: *"Frontend and backend teams must work from the same contract."* This document is
that contract, and changing it is governed:

| Change | Version impact |
|---|---|
| Adding an optional request field | None |
| Adding a response field | None (clients ignore unknown fields) |
| Adding an endpoint | None |
| Adding an enum value to a **response** | None, provided clients handle unknown values |
| Adding an enum value to a **request** | None |
| Removing or renaming a field | **Breaking** — `/api/v2/` |
| Changing a field's type or representation | **Breaking** — `/api/v2/` |
| Making an optional field required | **Breaking** — `/api/v2/` |
| Changing validation to reject previously-accepted input | **Breaking** — `/api/v2/` |
| Changing an error code's meaning | **Breaking** |

The OpenAPI schema is generated from code and **diffed in CI** (11 §6). A PR that changes
the contract fails the build until the change is acknowledged in this document, so the
contract cannot drift from the implementation silently.

### 15.1 The client-side obligation

Clients must: ignore unknown response fields, handle unknown enum values without
crashing, and never construct an identifier. Those three rules are what make the
non-breaking column above actually non-breaking rather than non-breaking-in-theory.

---

## 16. Open questions affecting this contract

| Ref | Effect on the API |
|---|---|
| Q2 | The interest fields accepted on a loan application and returned by `/loans/{id}/schedule/` depend on the interest model. |
| Q3 | Whether a settlement shortfall returns as a collector liability affects `/collectors/{id}/performance/`. |
| Q4 | Whether late fees are charged, on what, and to whom affects the contribution and repayment contracts. |
| Q5 | KYC thresholds determine which endpoints return `422 kyc_required` and at what verification level. |
| Q1 | Under `PLATFORM_CUSTODY`, additional account and reconciliation endpoints appear. |

Each is a field or an endpoint, not a restructuring. The contract is designed so that
resolving these adds to it rather than revising it — which is the practical reason for
settling the open questions before Phase 3 rather than after.
