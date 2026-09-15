# 08 — Disbursement Rules

**Status:** Phase 0 draft, awaiting review
**Derives from:** 01_PRODUCT_REQUIREMENTS, 03_DATABASE_DESIGN, 05_ROLE_PERMISSION_MATRIX, 06_FINANCIAL_RULES, Master Development Prompt §18, §44
**Depends on:** 06
**Decisions applied:** Q1 (custody — design for both), Q3 (settlement shortfall — record, group resolves)

---

## 1. Principles

**D1 — Approval is not payment.** §18's workflow has six stages for a reason. Two
separate postings bracket the gap (06 §4.10), so the ledger reflects a committed
obligation between the two, not money that has already left.

**D2 — Money leaves the group at one moment, and that moment is recorded once.** The
`PAYMENT_PROCESSING → VERIFICATION → COMPLETED` sequence exists because a payment
instruction and a completed payment are different facts. Marking `COMPLETED` when the
provider accepts the instruction would record money as paid when it is merely promised.

**D3 — Nobody approves their own payout.** §44 rule 6, enforced by a database trigger
(03 §10.2.1) rather than a service check, because this is exactly the rule a
well-meaning admin action violates at 2am.

**D4 — An approved disbursement cannot silently become unapproved.** Once approved, the
only paths forward are payment or documented cancellation. There is no "edit the amount
and re-approve" — that is a new request, against a new approval.

**D5 — Failure is a state, not an exception.** A failed payment leaves a disbursement in
`FAILED` with a reason. It never leaves it in `PAYMENT_PROCESSING` forever, and it never
turns into a silent `COMPLETED`.

---

## 2. Disbursement types (§18)

The seven types carried in `disbursements.disbursement_type` (03 §10.1), with their
accounting treatment:

| Type | Purpose | Debit on approval | Payee |
|---|---|---|---|
| `MONTHLY_GROUP_PAYOUT` | The rotation/ajo turn — one member receives the pooled contribution | `1110`/`1100` Scheme/Group Pool | Member |
| `LOAN_DISBURSEMENT` | Loan principal to a borrower | `1200` Loan Receivable | Member |
| `WELFARE_PAYMENT` | Bereavement, medical, or welfare benefit per group rules | `5030` Welfare Expense | Member |
| `EMERGENCY_ASSISTANCE` | Ad-hoc assistance outside the standing welfare rules | `5030` Welfare Expense | Member |
| `REFUND` | Returning a member's own money (over-contribution, exit) | `2000` Member Contribution Balance | Member |
| `APPROVED_GROUP_EXPENSE` | Operational spend — venue, refreshments, transport, levies | `5040` Group Operating Expense | Third party |
| `OTHER` | Anything the above does not cover | Requires explicit account selection | Either |

`OTHER` requires the requester to **name the debit account explicitly** and is the only
type that does. Every other type has exactly one correct treatment, hard-coded, so a
request cannot pick the wrong account by picking the wrong type — and `OTHER` is
conspicuous in reports because it is the type that means "a human chose the accounting".

**Only `LOAN_DISBURSEMENT` links to a `loans` row.** The others are independent of the
lending subsystem, which is why `disbursements.loan_id` is nullable (03 §10.1).

---

## 3. Workflow (§18)

### 3.1 States

```
                 ┌──────────┐
                 │ REQUEST  │  requester creates with idempotency key
                 └────┬─────┘
                      ▼
                 ┌──────────┐
                 │  REVIEW  │  an officer checks details, payee, amount
                 └────┬─────┘
          reject ◄────┤
                      ▼
                 ┌──────────┐
                 │ APPROVAL │  one or more approvers per group policy
                 └────┬─────┘
          reject ◄────┤    ◄── ledger: Dr pool / Cr 2030 Disbursement Payable
                      ▼
            ┌────────────────────┐
            │ PAYMENT_PROCESSING │  instruction sent to the payment provider
            └─────────┬──────────┘
                      ▼
            ┌────────────────────┐
            │    VERIFICATION    │  provider confirms; settlement confirmed
            └─────────┬──────────┘
              fail ◄──┤    ◄── ledger: Dr 2030 / Cr 1010 or 1100
                      ▼
                 ┌──────────┐
                 │ COMPLETED│
                 └──────────┘

  terminal alternatives: REJECTED · FAILED · CANCELLED
```

The status enum in 03 §10.1 is §18's workflow **verbatim**, with the three terminal
alternatives added. Nothing was renamed.

### 3.2 Transitions

| From | To | Actor permission | Requires |
|---|---|---|---|
| — | `REQUEST` | `disbursements.request` | Idempotency key; amount > 0; payee |
| `REQUEST` | `REVIEW` | `disbursements.review` | — |
| `REVIEW` | `APPROVAL` | `disbursements.review` | Review notes recorded |
| `REVIEW` | `REJECTED` | `disbursements.review` | Reason |
| `APPROVAL` | `PAYMENT_PROCESSING` | `disbursements.approve` | **All required approval steps satisfied** |
| `APPROVAL` | `REJECTED` | `disbursements.approve` | Reason |
| `PAYMENT_PROCESSING` | `VERIFICATION` | System | Provider accepted the instruction |
| `PAYMENT_PROCESSING` | `FAILED` | System | Provider rejected, or timeout with no confirmation |
| `VERIFICATION` | `COMPLETED` | System | Provider confirms settlement |
| `VERIFICATION` | `FAILED` | System | Provider confirms non-settlement |
| `REQUEST`/`REVIEW`/`APPROVAL` | `CANCELLED` | `disbursements.request` (own) or `disbursements.approve` | Reason; **no payment instruction issued** |

Any transition not in this table returns `409`. In particular:

- **`APPROVAL → COMPLETED` is impossible.** There is no shortcut past payment, whatever
  the caller's permissions (05 §6). This is D2 made mechanical.
- **`COMPLETED` is terminal.** A completed disbursement is corrected by a reversal
  (06 §4.11), not by editing its status.

### 3.3 Why REVIEW is a separate stage from APPROVAL

They are different questions. `REVIEW` asks *"is this request correct?"* — the right
payee, the right amount, the right type, supporting documents present. `APPROVAL` asks
*"should the group pay this?"* Collapsing them would mean the person checking the
arithmetic is also the person authorising the spend, and in a small group those are
often the same person anyway — but the system should not require it.

A group may configure review to be skipped for low-value types (a ₦2,000 welfare
payment does not need a review stage). Approval **never** skips.

---

## 4. Approval authority

### 4.1 Permission

`disbursements.approve` is held by `GROUP_OWNER` and `CHAIRMAN`, and by `TREASURER` only
where the group grants it (05 §5.2). `AUDITOR` holds no write permission anywhere in the
matrix (05 §6.7).

### 4.2 Multi-step approval

Group-configurable approval chains, recorded one row per step in
`disbursement_approvals` with `approval_step` (03 §10.2):

| Configuration | Steps required |
|---|---|
| Single approval | 1 step, any holder of `disbursements.approve` |
| Dual control | 2 steps, **two distinct users** |
| Threshold-based | 2+ steps above a configured amount, 1 below |

`UNIQUE (disbursement_id, approver_user_id, approval_step)` prevents the same person
filling two steps. The service additionally rejects a second approval from a user who
has already approved at a *different* step, because dual control means two people, not
two clicks — the constraint alone would not catch `step 1` and `step 2` by one user.

### 4.3 Separation of duties

| Rule | Enforcement |
|---|---|
| Requester ≠ approver | **Database trigger** (03 §10.2.1) |
| Approver ≠ requester's delegate/proxy | Service layer + test (13) |
| Two-step approval requires two distinct users | Service layer + `UNIQUE` constraint |
| A reviewer cannot be the sole approver where dual control applies | Service layer |

The trigger is the load-bearing one. It fires on `INSERT OR UPDATE`, so neither a
migration nor a `manage.py shell` session can create a self-approval — §44 rule 6 has no
application-layer dependency to bypass.

A violation surfaced to the API is a `403` with code `separation_of_duties`, not a raw
database error (04 §1.4).

---

## 5. Payment execution

### 5.1 Hand-off

At `APPROVAL → PAYMENT_PROCESSING` the service:

1. Posts the approval transaction (06 §4.10) — Dr pool, Cr `2030`
2. Commits
3. Enqueues the payment instruction via Celery
4. Returns `202` to the caller

**The provider call happens outside the transaction** (02 §5.3). A provider that hangs
must not hold a database transaction open, and a provider call inside a transaction that
then rolls back would have moved real money against a rolled-back record.

### 5.2 Payment method

`disbursements.payee_details` (JSONB) carries the destination — bank account, mobile
money number, or a marker for cash collection. Validation is per method, and the
provider adapter validates before the instruction is sent (02 §5.8).

A payee with no valid destination cannot reach `PAYMENT_PROCESSING`.

### 5.3 Idempotency

`disbursements.idempotency_key` is `UNIQUE` (03 §10.1, §15 table: *"Double disbursement
prevented"*). The key is sent to the provider too, so a retried instruction is
deduplicated at both layers — the platform does not send a second instruction, and if it
somehow does, the provider does not execute it twice.

This is the highest-consequence duplicate in the system: **a double-paid disbursement is
money that has left the group and cannot be un-sent.**

### 5.4 Payment states

The disbursement's payment is tracked as a `payments` row (03 §8) through the payment
abstraction layer. `MockProvider` in development and test; a real provider from Phase 3.

| Disbursement | Payment attempt | Meaning |
|---|---|---|
| `PAYMENT_PROCESSING` | `PENDING` / `PROCESSING` | Instruction sent, outcome unknown |
| `VERIFICATION` | `SUCCESS` | Provider confirms; settling |
| `FAILED` | `FAILED` | Provider rejected, or timed out without confirmation |
| `COMPLETED` | `SUCCESS` + settled | Funds confirmed at the destination |

---

## 6. Verification and completion

### 6.1 What `COMPLETED` means

`COMPLETED` requires **confirmation of settlement**, not acceptance of the instruction.
Where the provider supports a status query or webhook, that is the source. Where it does
not, the platform polls, and a disbursement that neither confirms nor fails within the
configured window is escalated to a human rather than left in `PAYMENT_PROCESSING`.

**There is no timeout that resolves to `COMPLETED`.** An unknown outcome is `FAILED` plus
an alert, because marking an unconfirmed payment complete is how a group's books come to
disagree with its bank account.

### 6.2 The second posting

On `COMPLETED` (06 §4.10): Dr `2030 Disbursement Payable`, Cr `1010` Funds Clearing —
Provider (or `1100` Group Pool for immediate/immediate-settlement methods).

The two postings together mean that between approval and settlement, `2030` carries the
committed-but-unpaid obligation — visible on the group's balance as money spoken for.
This is the answer to *"how much have we approved but not yet paid?"*, and it exists as a
ledger balance rather than a status query because a status query cannot be reconciled
against the bank.

---

## 7. Failure handling

| Situation | Handling |
|---|---|
| Provider rejects the instruction | `PAYMENT_PROCESSING → FAILED`; reason stored; **no ledger posting** — the approval posting stands, and the obligation remains |
| Provider timeout, no confirmation | `→ FAILED` after the configured window, plus an alert; a human reconciles |
| Retry after a transient failure | Permitted from `FAILED` back to `PAYMENT_PROCESSING`, **with the same idempotency key**, up to a configured attempt limit |
| Payee details invalid | Caught before `PAYMENT_PROCESSING`; never a provider failure |
| Group cancels after approval but before instruction | `APPROVAL → CANCELLED`, and the approval posting is **reversed** (06 §4.11) — the obligation is discharged, and the reversal is a visible transaction rather than a deleted one |
| Disbursement failed permanently | Requires explicit human action to cancel or re-request. Nothing auto-resolves. |

**A `FAILED` disbursement keeps its approval posting.** The group still owes the money —
they just have not managed to pay it yet. Reversing the posting on failure would show the
funds as available again when the obligation has not gone away, and the next request
would be approved against a pool that is already committed.

---

## 8. Cancellation and reversal

| Scenario | Path |
|---|---|
| Cancelled before approval | `CANCELLED`. No ledger effect — nothing was posted. |
| Cancelled after approval, before payment | `CANCELLED` + **reversal of the approval posting** (06 §4.11) |
| Paid to the wrong payee | Not cancellable. A new `REFUND` disbursement returns the funds, or the payment is reversed at the provider. Requires `disbursements.approve` and an audit record. |
| Duplicate payment discovered | Reversal of the duplicate transaction (06 §4.11), not deletion. The original stays visible. |

**A completed disbursement is never deleted** (03 §2.5, §44 rule 4). Every correction is
a new transaction referencing the original, with a reason and an actor.

---

## 9. Not in scope for the MVP

- Scheduled recurring disbursements (a standing monthly welfare payment)
- Batch disbursement across many payees in one approval
- Multi-currency payout
- Partial payment of a disbursement (a disbursement is paid in full or not at all)

---

## 10. Test obligations

Carried into 13_TESTING_STRATEGY:

| # | Test |
|---|---|
| D1 | A requester cannot approve their own disbursement — attempted at the database level, not only through the API |
| D2 | Dual-control approval rejects the same user at two different steps |
| D3 | `APPROVAL → COMPLETED` is rejected with `409` regardless of permissions |
| D4 | Approval posts Dr pool / Cr `2030`; payment posts Dr `2030` / Cr `1010`; the pair balances |
| D5 | A `FAILED` disbursement retains its approval posting |
| D6 | Cancellation after approval reverses the approval posting rather than deleting it |
| D7 | Re-sending the same idempotency key produces exactly one disbursement |
| D8 | A retry after a transient failure uses the same idempotency key and produces one provider instruction |
| D9 | No provider call occurs inside an open database transaction |
| D10 | A provider timeout resolves to `FAILED` with an alert, never to `COMPLETED` |
| D11 | An unconfirmed disbursement never reaches `COMPLETED` |
| D12 | A payee with no valid destination cannot reach `PAYMENT_PROCESSING` |
| D13 | Every disbursement type posts to its §2 debit account; `OTHER` requires an explicit account |
| D14 | A completed disbursement cannot be deleted through any code path |
| D15 | All seven types appear in the group balance's `2030` total between approval and settlement |
| D16 | A `LOAN_DISBURSEMENT` reaching `COMPLETED` advances its loan to `DISBURSED` (§7 of 07) |
