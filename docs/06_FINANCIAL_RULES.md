# 06 — Financial Rules

**Status:** Phase 0 draft, awaiting review
**Derives from:** 01_PRODUCT_REQUIREMENTS, 03_DATABASE_DESIGN, 05_ROLE_PERMISSION_MATRIX, Master Development Prompt §10–§13, §19, §28, §44
**Depends on:** 01, 03
**Decisions applied:** Q2 (no interest default), Q3 (variance recorded, group resolves), Q4 (late fees to group, on contributions), Q7 (integer minor units), Q9 (double-entry)

---

## 1. Principles

**F1 — The ledger is the only source of truth.** Every figure a user sees is derived
from entries. Nothing else is authoritative (03 D1, D4).

**F2 — Nothing is deleted.** Corrections are reversals. A completed transaction is
immutable (§44 rule 3, §12).

**F3 — Nothing is computed twice.** One function posts to the ledger, one function
derives a balance, one function allocates a payment across components. Duplicated
arithmetic is duplicated behaviour, and the duplicates drift.

**F4 — No division without a stated rounding rule.** Every place the system divides —
splitting interest across instalments, apportioning a fee, allocating a partial payment
— names its rounding method and the destination of the remainder. §3.4 specifies them.

**F5 — Money is integer minor units.** No floating point, ever (03 §1.2).

**F6 — Every financial operation is idempotent.** §44 rule 12.

**F7 — A financial rule that is not written down does not exist.** Every rule below is
implemented once and tested (13). §48 forbids hard-coded business rules, which is why
most of these are configuration with a stated default rather than constants.

---

## 2. Chart of accounts — additions

03 §3 seeds the chart. The posting rules in §4 require four further accounts:

| Code | Name | Type | Normal | Owner | Purpose |
|---|---|---|---|---|---|
| `2030` | Disbursement Payable | LIABILITY | CREDIT | GROUP | Approved but not yet paid |
| `2110` | Member Late Fee Receivable | ASSET | DEBIT | MEMBER | Accrued late fee not yet settled |
| `5020` | Variance Loss | EXPENSE | DEBIT | GROUP | A settlement shortfall the group has accepted |
| `4030` | Variance Gain | INCOME | CREDIT | GROUP | A settlement surplus the group has accepted |

`5020` and `4030` are only used when a variance is **resolved** (§5.3). While a variance
is open, it sits in `2100 Settlement Variance Suspense` and belongs to nobody.

---

## 3. Money arithmetic

### 3.1 Representation

`BIGINT` minor units. ₦1,000.00 is `100000`. The `Money` type carries
`(amount_minor: int, currency: str)` and refuses cross-currency arithmetic without an
explicit rate (03 §1.2).

The currency's minor-unit exponent comes from the **`currencies` table** — NGN 2, JPY 0,
KWD 3 — never from an assumption of 2 (03 §4.5.2, 09 §5.1). The same table carries the
currency's rounding mode (§3.4.1), so both live in one place and neither can be
configured inconsistently for two countries that share a currency.

### 3.2 Addition and subtraction

Exact, always. Integers. No rounding, no special cases.

### 3.3 Multiplication

`amount × rate` where `rate` is a rational (`Fraction` or a scaled integer), never a
float.

```
interest_minor = (principal_minor * rate_numerator) // rate_denominator
remainder      = (principal_minor * rate_numerator) %  rate_denominator
```

The `//` and `%` pair is deliberate. Python's floor division on positive integers gives
a consistent, documented result, and the remainder is **kept** rather than dropped —
discarding it is how a system loses kobo across ten thousand transactions and produces a
ledger that will not reconcile.

### 3.4 Rounding and allocation

Rounding is **not a platform constant**. It is a property of the currency and, where the
law prescribes one, of the jurisdiction. So it is **data**, resolved per currency, with
the platform supplying only a default (03 §4.5.2).

Two decisions are involved, and conflating them is what makes rounding rules look
complicated. They are orthogonal — any allocation can use any rounding mode:

| Concept | Question | Values |
|---|---|---|
| **Rounding mode** | How does a rational become a whole minor unit? | `HALF_UP`, `HALF_EVEN`, `DOWN`, `UP`, `TOWARD_ZERO` |
| **Remainder disposition** | When a total splits across N shares, where does the residue go? | `LAST_SHARE`, `FIRST_SHARE`, `SUSPENSE` |

#### 3.4.1 Rounding modes

| Mode | Behaviour | Applicable when |
|---|---|---|
| `HALF_UP` | Half away from zero. `2.5 → 3`, `−2.5 → −3` | **The platform default** |
| `HALF_EVEN` | Banker's rounding. `2.5 → 2`, `3.5 → 4` | A jurisdiction mandates it for reporting |
| `DOWN` | Toward zero | Intermediate allocation steps |
| `UP` | Away from zero | A jurisdiction requires charges to round in the platform's favour |
| `TOWARD_ZERO` | Truncate | Explicit only; never a default |

`HALF_UP` is the default because it is what a member reproduces when they check the
arithmetic by hand. A figure that disagrees with the member's own paper calculation is a
support ticket even when the platform is right.

`HALF_EVEN` is not a curiosity. Some jurisdictions mandate it, and a platform that can
round only one way cannot enter those markets without a code change per country — which
is precisely the coupling this section exists to avoid.

#### 3.4.2 Remainder disposition

Splitting a total across N shares and rounding each independently loses residue. This is
arithmetic, not currency-specific — it happens with kobo, fils, yen, or krona:

| Strategy | Residue goes to | Use |
|---|---|---|
| `LAST_SHARE` | The final share | **Default** |
| `FIRST_SHARE` | The first share | Where the final share differing is undesirable |
| `SUSPENSE` | `2100` | Where the group must decide; never automatic |

**Why `LAST_SHARE` is the default.** The final share falls due last, so any
member-visible irregularity appears in the last payment — after the member has seen
several regular ones and the explanation is available — rather than in the first, where
it looks like the platform made an error.

#### 3.4.3 Where the rules apply

The four contexts. **These IDs are stable** — 07, 13, and the test obligations cite them.

| # | Context | Mode | Disposition |
|---|---|---|---|
| R1 | Percentage fees and charges (late fee, service charge) | the currency's mode | N/A — single value |
| R2 | Allocating a total across instalments (interest over a schedule) | `DOWN` | `LAST_SHARE` |
| R3 | Allocating a payment across components (principal / interest / fee / penalty) | `DOWN` | priority order; last component absorbs |
| R4 | Prorating a partial period | the currency's mode | N/A — single value |

**R1 and R4 take the currency's mode**, because each produces a single member-facing
figure — and that is the figure a jurisdiction's rounding law actually speaks to.

**R2 and R3 force `DOWN` regardless of the currency's mode.** They are intermediate
allocation steps whose entire purpose is to leave a residue for deterministic
assignment. A half-rounding mode there would round *and* leave a residue, which is
incoherent — and the shares would no longer sum to the total they were derived from.

#### 3.4.4 Worked examples

The exponent changes the answer; the rules above are unchanged by it.

**Exponent 2 — NGN (kobo).** ₦10,000.00 of interest over 3 instalments:

```
total = 1000000 minor
1000000 ÷ 3 = 333333.33…      → DOWN each: 333333, residue 1
LAST_SHARE: 333333 + 333333 + 333334 = 1000000  ✓
```

**Exponent 0 — JPY (no minor unit).** ¥10,001 of interest over 3 instalments:

```
total = 10001 minor
10001 ÷ 3 = 3333.67           → DOWN each: 3333, residue 2
LAST_SHARE: 3333 + 3333 + 3335 = 10001  ✓
```

**Exponent 3 — KWD (fils).** KD 1.001 of interest over 3 instalments:

```
total = 1001 minor
1001 ÷ 3 = 333.67             → DOWN each: 333, residue 2
LAST_SHARE: 333 + 333 + 335 = 1001  ✓
```

**One charge, three exponents.** A 2.5% service charge, computed as
`(amount_minor × 25) // 1000`:

```
JPY  ¥1,001        → 25 minor  → HALF_UP → ¥25          (exponent 0)
NGN  ₦10.01        → 25 minor  → HALF_UP → ₦0.25        (exponent 2)
KWD  KD 1.001      → 25 minor  → HALF_UP → KD 0.025     (exponent 3)
```

The integer arithmetic is identical in all three. What differs is what a minor unit
*means* — and the platform never assumed it was two decimal places.

**R3 — why a fixed priority order.** When a ₦5,000 payment must be split across
principal, interest, fee, and penalty components that together exceed it, something must
be paid first. The order is:

```
1. Penalty       (if any)
2. Fees
3. Interest
4. Principal
```

This is the conventional order and it is defensible — but the ordering has real
consequences for the borrower (paying penalty first means more interest accrues for
longer) and it **must be reviewed under 2.7.6 of the regulatory checklist** before
launch. It is stated here so it is visible and reviewable rather than emergent from
whichever `if` statement ran first.

**A worked example:**

```
Payment: 500000 minor (₦5,000.00 at exponent 2)
Owed:    penalty 0, fee 10000, interest 80000, principal 450000

Priority order: fee → interest → principal
  fee:       min(10000, 500000)  = 10000,  remaining 490000
  interest:  min(80000, 490000)  = 80000,  remaining 410000
  principal: min(450000, 410000) = 410000, remaining 0

Components: 10000 + 80000 + 410000 = 500000  ✓  (03 §9.6 constraint satisfied)
Outstanding principal after: 450000 - 410000 = 40000
```

### 3.4.5 The boundary: this model assumes fiat

Minor units as integers work for every fiat currency and stop working for
crypto-assets. The reason is arithmetic, not policy:

- A `BIGINT` holds ≈ 9.2 × 10¹⁸. At exponent 2 that is ₦92 quadrillion — ample.
- At exponent 18 (ETH, wei) it is **9.2 units**. Not ample.

So the design supports fiat currencies with exponents 0–4, and cannot be extended to
18-decimal assets by configuration. That is recorded as an open question (Q10) rather
than quietly assumed: if digital assets are ever in scope they need a different
representation, not a wider column.

### 3.5 What is never done

- No `float` or `Decimal` for a monetary value at rest.
- No rounding applied to a stored value (R1–R4 round at computation, before storage).
- No percentage applied to a percentage.
- No figure accepted from a client (F1, §44 rule 1).
- No balance computed by summing a column other than `ledger_entries`.

---

## 4. Posting rules

Every financial event maps to exactly one balanced transaction. This section is the
definitive list. A business event not listed here is a design gap, not an invitation to
improvise — §52.

Notation: **Dr** = debit, **Cr** = credit. Amounts in minor units.

### 4.1 Contribution — cash, recorded by a collector

The collector takes cash. The platform does not yet hold it; the group does not yet have
it. It is **in transit**.

| Account | Dr | Cr |
|---|---|---|
| `1000` Cash in Transit — Collector | amount | |
| `2000` Member Contribution Balance | | amount |

`2000` is a **liability**: the group owes the member. The member's contribution has
increased their claim on the group. This is the accounting fact that makes "total
contributions" on the member dashboard (04 §3) a real balance rather than a counter.

**What if the member paid more than expected?** `amount` is what was received, not what
was expected. The schedule records the expectation; the ledger records the money. Where
they differ, `contribution_schedules.status` becomes `PARTIAL` or the excess is held
against the next period's schedule (a scheme-level configuration). §9's requirement to
show both expected and actual falls out of keeping them separate.

### 4.2 Contribution — electronic, via payment provider

**On verified webhook** (never on client claim — §44 rule 4):

| Account | Dr | Cr |
|---|---|---|
| `1010` Funds Clearing — Provider | amount | |
| `2000` Member Contribution Balance | | amount |

**On settlement from provider to group:**

| Account | Dr | Cr |
|---|---|---|
| `1100` Group Pool | amount | |
| `1010` Funds Clearing — Provider | | amount |

The two-step posting is not ceremony. Between the member paying and the group receiving,
the money is genuinely somewhere — with the provider — and a single-entry system would
have to either pretend the group has it (overstating the group's position) or pretend it
does not exist (losing track of it). `1010` is where money that has been paid but not yet
settled lives, and reconciling it against the provider's own statement is a primary
control (§9.2).

### 4.3 Collector settlement — no variance

Collector submits exactly what was expected.

| Account | Dr | Cr |
|---|---|---|
| `1100` Group Pool | expected | |
| `1000` Cash in Transit — Collector | | expected |

Cash in Transit for that collector returns to zero. Any collector whose `1000` balance is
non-zero outside a collection window is a reconciliation exception (§9.3).

### 4.4 Collector settlement — shortfall (₦6,000)

§10's worked example: Expected ₦82,000, Submitted ₦76,000.

| Account | Dr | Cr |
|---|---|---|
| `1100` Group Pool | 76000 | |
| `2100` Settlement Variance Suspense | 6000 | |
| `1000` Cash in Transit — Collector | | 82000 |

**The ₦6,000 is not written off, not deducted, and not hidden.** It is in suspense,
owned by nobody, visible on the group's books, and paired with a mandatory written
explanation (03 §7.2).

This is Q3's answer expressed as accounting: the platform records the difference
faithfully and **does not decide who bears it**. §10 requires that the difference be
flagged and explained, and is silent on liability; inventing a liability rule would be
exactly what §52 prohibits.

### 4.5 Collector settlement — surplus (₦3,000 over)

| Account | Dr | Cr |
|---|---|---|
| `1100` Group Pool | 85000 | |
| `1000` Cash in Transit — Collector | | 82000 |
| `2100` Settlement Variance Suspense | | 3000 |

A surplus is also a variance and is treated identically. It is tempting to treat
over-submission as harmless; it is not. A collector who repeatedly over-submits may be
covering for a member who paid and was not recorded, which is a member-facing error
hiding inside a group-facing surplus.

### 4.6 Variance resolution (Q3)

A variance is resolved by a group officer holding `settlements.resolve_variance`, with a
reason, and is audited.

**Shortfall accepted by the group:**

| Account | Dr | Cr |
|---|---|---|
| `5020` Variance Loss | 6000 | |
| `2100` Settlement Variance Suspense | | 6000 |

**Shortfall recovered from the collector:**

| Account | Dr | Cr |
|---|---|---|
| `1100` Group Pool | 6000 | |
| `2100` Settlement Variance Suspense | | 6000 |

(The recovery lands as cash or as a deduction from a later settlement; either way the
suspense clears and the group is made whole.)

**Surplus accepted:**

| Account | Dr | Cr |
|---|---|---|
| `2100` Settlement Variance Suspense | 3000 | |
| `4030` Variance Gain | | 3000 |

Note what is **not** here: a path that adjusts the original contribution, or edits the
settlement, or touches `1000` retroactively. §10's *"never silently alter the records"* is
satisfied by construction — the only way to clear suspense is a new, attributed,
auditable transaction.

### 4.7 Late fee (Q4 — group, on contributions)

A missed contribution past its grace period accrues a late fee to the group.

| Account | Dr | Cr |
|---|---|---|
| `2110` Member Late Fee Receivable | fee | |
| `4000` Late Fee Income | | fee |

`4000` is owned by the **group** (Q4). When the member pays:

| Account | Dr | Cr |
|---|---|---|
| `1000` Cash in Transit — Collector | fee | |
| `2110` Member Late Fee Receivable | | fee |

**Late fees are not charged on loan repayments in the MVP** (Q4). That keeps the
penalty-disclosure question (regulatory checklist §2.7.6) out of the initial launch, and
`loans` carries a `penalty_component` that defaults to 0 and is unused until a future
release enables it deliberately.

**The fee amount is calculated at the moment it accrues, not at the moment it is paid.**
A fee that grows while unpaid is a compounding penalty, which is a regulatory question
this document does not answer. The accrued amount is fixed when it is charged.

### 4.8 Loan disbursement

| Account | Dr | Cr |
|---|---|---|
| `1200` Loan Receivable *(owner: the loan)* | principal | |
| `1100` Group Pool | | principal |

The group's pool decreases; a receivable against the borrower appears. The interest is
**not** posted at disbursement — it is recognised as it is earned, at each instalment
(§4.9).

### 4.9 Loan repayment

Applied in the R3 priority order (§3.4):

| Account | Dr | Cr |
|---|---|---|
| `1000` Cash in Transit — Collector *(or `1100` for electronic)* | amount | |
| `1200` Loan Receivable | | principal_component |
| `1210` Interest Receivable | | interest_component |
| `1220` Fees Receivable | | fee_component |
| *(penalty component, when enabled)* | | |

The components sum to `amount` by the constraint in 03 §9.6, so the transaction balances
by construction.

Where the group uses flat or reducing-balance interest (Q2), the interest recognised per
instalment comes from `loan_schedules.interest_due`, which was computed at schedule
generation (03 §9.4) — not recalculated at payment time. The schedule is the auditable
artifact; the payment follows it.

### 4.10 Disbursement

Two postings, matching §18's workflow: approval is not payment.

**On approval:**

| Account | Dr | Cr |
|---|---|---|
| `1110` Scheme Pool *(or `1100` Group Pool)* | amount | |
| `2030` Disbursement Payable | | amount |

**On payment:**

| Account | Dr | Cr |
|---|---|---|
| `2030` Disbursement Payable | amount | |
| `1010` Funds Clearing — Provider *(or `1100` for immediate)* | | amount |

Between approval and payment the group knows it owes the money. A single-step posting
would show the funds as still available for the whole window between approving a payout
and executing it — which is exactly the window in which a second payout gets approved by
mistake.

### 4.11 Reversal

The exact mirror of the original: every debit becomes a credit and vice versa, for
identical amounts and accounts.

Original contribution of ₦1,000 cash:

| Account | Dr | Cr |
|---|---|---|
| `1000` Cash in Transit | 1000 | |
| `2000` Member Contribution Balance | | 1000 |

Reversal:

| Account | Dr | Cr |
|---|---|---|
| `2000` Member Contribution Balance | 1000 | |
| `1000` Cash in Transit | | 1000 |

Constraints (03 §2.5): the original is never modified; its status becomes `REVERSED`; a
transaction can be reversed at most once; the reversal references the original and
carries a mandatory reason.

**Reversing a reversal is a new transaction, not a deletion.** The chain stays linear and
each link is attributable.

### 4.12 Manual adjustment

Any balanced pair, proposed by one authorised user and approved by another
(`ledger.post_adjustment`, dual control — 05 §6.5). Requires a reason. Audited.
**Never used to correct something that has a proper business transaction** — a
mis-recorded contribution is reversed and re-recorded, not adjusted.

### 4.13 Posting summary

| Event | Debit | Credit |
|---|---|---|
| Contribution (cash) | `1000` | `2000` |
| Contribution (electronic, verified) | `1010` | `2000` |
| Provider settles to group | `1100` | `1010` |
| Settlement, no variance | `1100` | `1000` |
| Settlement, shortfall | `1100` + `2100` | `1000` |
| Settlement, surplus | `1100` | `1000` + `2100` |
| Variance resolved — accepted loss | `5020` | `2100` |
| Variance resolved — recovered | `1100` | `2100` |
| Variance resolved — surplus accepted | `2100` | `4030` |
| Late fee accrued | `2110` | `4000` |
| Late fee paid | `1000` | `2110` |
| Loan disbursed | `1200` | `1100` |
| Loan repaid | `1000` / `1100` | `1200` + `1210` + `1220` |
| Disbursement approved | `1110` / `1100` | `2030` |
| Disbursement paid | `2030` | `1010` / `1100` |
| Reversal | mirror of original | mirror of original |
| Adjustment | *any* | *any* — must sum equal |

---

## 5. Collector settlement rules

### 5.1 Expected amount

`expected_amount` is derived server-side:

```
expected = Σ over the settlement period of
             (contribution_schedules.expected_amount
              for schedules belonging to this collector's scheme and members)
```

**Never supplied by the client** (§44 rule 1). A collector who could state their own
expected total could settle against any number they chose.

### 5.2 Variance

```
variance = submitted_amount - expected_amount
```

A generated column (03 §7.2), so it cannot disagree with its inputs. Any non-zero value:

1. Is flagged on the settlement
2. **Requires a written explanation** before the settlement can be `SUBMITTED` or
   `VERIFIED` — enforced by a database `CHECK`, not only by a service
3. Posts to `2100 Settlement Variance Suspense` (§4.4, §4.5)
4. Remains visible until resolved

### 5.3 Resolution (Q3)

**A variance is resolved by a human decision, per case.** The platform records it, keeps
it visible, and provides an audited path to clear it. It does not auto-deduct, auto-write-
off, or auto-bill.

| Resolution | Who | Effect |
|---|---|---|
| Accepted loss (group bears) | `settlements.resolve_variance` | `5020` Variance Loss |
| Recovered from collector | `settlements.resolve_variance` | Group made whole |
| Corrected by a later collection | `contributions.record` | Member's schedule updated; suspense cleared against the new contribution |
| Surplus accepted | `settlements.resolve_variance` | `4030` Variance Gain |

The requirement on the resolver is a written reason, and the whole path is audited with
who, when, and why (§25).

**Why not automate it.** Q3 was deliberately left open because §10 is silent and the
answer has legal weight (regulatory checklist §2.6.4). Automating a deduction from a
collector's pay is a decision about employment liability; automating a write-off is a
decision about who funds the loss. Neither is a decision a schema should make by
default, and a system that resolves variance silently is precisely what §10 forbids.

### 5.4 Settlement state transitions

```
DRAFT ──► SUBMITTED ──► VERIFIED
  │           │
  │           └──► DISPUTED ──► (resolved) ──► VERIFIED
  │
  └──► CANCELLED
```

| Transition | Actor | Requires |
|---|---|---|
| `DRAFT → SUBMITTED` | Collector | Variance explained if non-zero |
| `SUBMITTED → VERIFIED` | `settlements.verify` | Recorder ≠ verifier (§6.2 of 05) |
| `SUBMITTED → DISPUTED` | `settlements.dispute` | Reason |
| `DISPUTED → VERIFIED` | `settlements.verify` | Variance resolved |
| any `→ CANCELLED` | `settlements.verify` | Reason; audit-logged |

---

## 6. Balance derivation

### 6.1 The rule

A balance is the signed sum of an account's entries, in its normal direction:

```
natural     = Σ(amount) where direction = normal_balance
contra      = Σ(amount) where direction ≠ normal_balance
balance     = natural - contra          # in minor units, signed
```

Never `SUM(amount)` alone. A balance computed without regard to direction is correct only
while the account has entries in one direction, and silently wrong forever after the
first reversal. This is the single most likely arithmetic error in the system, which is
why the entry table stores direction separately from a positive amount (03 §2.4) rather
than a signed value.

### 6.2 The balances the platform maintains

| Balance | Derived from | Scope |
|---|---|---|
| Member balance | `2000`/`2010`, owner = member | Per member, per group |
| Scheme balance | `1110` + `1200` + `1210` + `1220`, owner = scheme | Per scheme |
| Group balance | `1100` + `2100` + `2030` + `1200` + `2100`, owner = group | Per group |
| Platform totals | All accounts, by type | Platform-wide |
| Outstanding principal | `1200`, owner = loan | Per loan |

§13 requires the platform "at all times" be able to determine member, scheme, group, and
platform balances. All four reduce to the same query with a different owner scope.

### 6.3 The §13 identity

§13 gives this form:

```
Opening Balance + Contributions + Other Credits
                − Disbursements − Refunds − Other Debits
                = Current Balance
```

Which is the same statement as §6.1 expressed in business terms:

```
opening_balance = Σ(entries before the period)
contributions   = Σ(CONTRIBUTION transactions in the period, credit side)
other_credits   = Σ(other credit-direction entries in the period)
disbursements   = Σ(DISBURSEMENT transactions in the period, debit side)
refunds         = Σ(REFUND transactions, debit side)
other_debits    = Σ(remaining debit-direction entries in the period)
current_balance = opening + credits − debits
```

The identity is not asserted; it is **arithmetically guaranteed** by the entries summing
to zero per transaction (§4). The platform can therefore *test* §13 rather than hope for
it, and 13_TESTING_STRATEGY includes exactly that.

### 6.4 Derived caches

`loans.outstanding_*` are the only cached balances (03 §9.3.1). Conditions:

- Written only inside the transaction that posts the entry
- Recomputed nightly from the ledger
- **A mismatch is a P1 alert**, not a silent correction
- The ledger wins, always

### 6.5 What is never a balance

- A column a user can edit
- A figure stored in Redis
- A figure computed on a client
- A running total maintained by application code outside a transaction

---

## 7. Reversals

| Rule | Detail |
|---|---|
| F-REV-001 | A completed transaction is never deleted or mutated (§44 rule 3). |
| F-REV-002 | A reversal is a new transaction with mirrored entries, referencing the original. |
| F-REV-003 | A transaction may be reversed at most once (03 §2.5). |
| F-REV-004 | A reversal requires a reason. |
| F-REV-005 | Reversing beyond the caller's authority requires an approver (05 §6.3). |
| F-REV-006 | A reversal of a reversal is a new transaction, not a deletion of the first reversal. |
| F-REV-007 | Reversals are audited (§25). |
| F-REV-008 | A downstream transaction is not auto-reversed. If a reversed contribution funded a loan repayment, the platform flags the dependency and a human resolves it. |

F-REV-008 is the rule that resists a tempting automation. Cascading reversals sound
correct until two legitimate operations depend on one another, and the cascade either
loops or silently unwinds work nobody intended to undo. Reversal is a human action with
a human reason (F-REV-004); its consequences get a human decision too.

---

## 8. Idempotency and concurrency

### 8.1 Idempotency

Every financial mutation carries a unique `idempotency_key` (03 §12.2). The write of the
key and the execution of the operation share one transaction, so a crash mid-operation
cannot leave a key that claims success without the work having happened.

`request_hash` catches a key reused with a different payload — a client bug, surfaced as
`409` rather than silently honoured (04 §1.5).

### 8.2 Concurrency

| Situation | Control |
|---|---|
| Two contributions to one member, concurrent | `select_for_update()` on the member's balance-bearing account |
| Two disbursements from one group, concurrent | `select_for_update()` on the group pool account |
| Settlement verification racing a new contribution | `select_for_update()` on the collector's `1000` account |
| Duplicate webhook, concurrent | `UNIQUE (provider_code, event_id)` — the database arbitrates |
| Ledger balance under concurrency | Deferred constraint trigger at commit (03 §2.4.1) |

**Lock ordering** is fixed and documented in `ledger/services.py`: accounts are always
locked in ascending `id` order. Two operations locking the same two accounts in opposite
orders deadlock, and the deadlock appears under production load rather than in review.

### 8.3 Transaction boundaries

```
┌─ transaction.atomic() ─────────────────────────┐
│  select_for_update() on balance-bearing rows   │
│  validate business rules                       │
│  insert transactions + ledger_entries          │
│  update derived caches                         │
│  write idempotency key                         │
└────────────────────────────────────────────────┘
                   │  COMMIT
                   ▼
   enqueue notifications, provider calls, webhooks
```

**External calls never occur inside the transaction** (02 §5.3). A provider timeout
inside a transaction holds row locks for the timeout's duration, and enough of those is
an outage.

---

## 9. Reconciliation

§44 rule 8 requires every important financial action be recorded; reconciliation is how
the platform proves the recording is *correct* rather than merely present.

### 9.1 Ledger self-consistency (continuous)

| Check | Frequency | On failure |
|---|---|---|
| Every transaction's entries sum to zero | Continuous (commit-time trigger) | Commit rejected |
| No entry references an inactive account | Hourly | P1 |
| No entry against a `REVERSED` transaction | Continuous (trigger) | Commit rejected |

### 9.2 Provider reconciliation (scheduled)

The platform's `1010 Funds Clearing — Provider` balance is compared against the
provider's own statement.

| Check | Frequency | On failure |
|---|---|---|
| Platform's unsettled total = provider's | Daily | P1 — escalate |
| Every `COMPLETED` payment has a provider record | Daily | P1 |
| Every provider transaction has a platform record | Daily | **P1 — this is the one that matters** |

The third row is the one that catches money the platform does not know about. A payment
taken by the provider and never recorded is invisible to every other check, because every
other check starts from the platform's own data.

### 9.3 Cash reconciliation

The collector model's core control.

| Check | Frequency | On failure |
|---|---|---|
| Every collector's `1000` balance is zero outside a collection window | Daily | Investigate |
| Every settlement is verified within N days | Daily | Flag to group officers |
| No `2100` variance older than N days | Weekly | Flag to group officers |
| Settlement items match the contributions they claim | Continuous | Commit rejected |

### 9.4 Derived-cache reconciliation

| Check | Frequency | On failure |
|---|---|---|
| `loans.outstanding_*` equals the ledger | Nightly | **P1 — alert, then repair from the ledger** |
| Any cached aggregate equals its derivation | Nightly | P2 |

**The repair direction is never in doubt: the ledger is authoritative** (F1). The job's
first duty is to *detect*, and a mismatch is treated as an incident even after automatic
repair, because a cache that drifted means something wrote it outside the transaction
that should have.

---

## 10. Failure modes and their handling

| Failure | Behaviour |
|---|---|
| Provider call times out mid-payment | Payment stays `PROCESSING`; verification resolves it. **Never assumed failed, never assumed succeeded.** |
| Webhook arrives before the client returns | Both paths are idempotent; whichever arrives first posts, the second is a no-op |
| Webhook never arrives | A reconciliation sweep verifies pending payments server-side and posts what the provider confirms |
| Worker dies mid-task | Celery redelivers; the idempotency key makes the redelivery safe (§8.1) |
| Duplicate settlement submission | `reference` and `idempotency_key` uniqueness reject the second |
| Client retries after an ambiguous timeout | Key returns the original result |
| Ledger insert would unbalance | Commit rejected; the caller gets a `500` and an alert fires |
| Derived cache drifts | Nightly detection; P1; repaired from the ledger |

The first row is the one that gets implemented wrong. A timeout is **not** a failure — it
is an unknown, and the only correct handling of an unknown in a payment system is to go
and find out.

---

## 11. Configuration versus constants

§48 forbids hard-coded business rules. What a group configures, and its bounds:

| Setting | Scope | Bound |
|---|---|---|
| Scheme amount, min, max | Scheme | — |
| Contribution and disbursement frequency | Scheme | Independent (FR-SCH-004) |
| Grace period | Scheme | — |
| Late fee amount or percent | Scheme | Per `platform_settings` ceiling |
| Interest method and rate | Scheme | **Explicit choice required, no default (Q2)** |
| Loan eligibility rules | Group | Beneath `loan_rules_ceiling` (§15) |
| Max loan amount | Group | Beneath the platform ceiling |
| Guarantors required | Group | 0–5 |
| Disbursement approval chain | Group | Within the permission model (05) |

Platform ceilings live in `platform_settings` (03 §12.4) so they are adjustable without a
deploy. A hard-coded ceiling in a module is a defect even when its value is currently
correct.

---

## 12. Test obligations

Carried into 13_TESTING_STRATEGY:

| # | Test |
|---|---|
| F1 | Every transaction posts entries summing to zero; an unbalanced one is rejected at commit |
| F2 | A contribution of ₦1,000 posts exactly the §4.1 pair |
| F3 | §10's example: expected 82000, submitted 76000 → variance −6000, suspense 6000, explanation required |
| F4 | A settlement with a non-zero variance and no explanation cannot reach `VERIFIED` — via API, admin, or SQL |
| F5 | A reversal mirrors exactly and leaves the original unmodified |
| F6 | A transaction cannot be reversed twice |
| F7 | Balance = natural − contra, including after a reversal |
| F8 | §13's identity holds for every account over every period |
| F9 | R2: interest split over N instalments sums exactly to the total |
| F10 | R3: a partial payment's components sum exactly to the payment |
| F11 | Concurrent contributions to one member do not lose an update |
| F12 | Concurrent disbursements from one group cannot overdraw |
| F13 | A duplicate webhook produces exactly one ledger transaction |
| F14 | A payment timeout resolves to the provider's actual outcome, not to "failed" |
| F15 | No `float` or `Decimal` appears in any monetary model field |
| F16 | A scheme cannot be saved without an explicit interest choice (Q2) |
| F17 | A late fee posts to `4000` owned by the group, on contributions only (Q4) |
| F18 | `2100` suspense is zero for a group with no open variances |
| F19 | Derived caches match the ledger after a randomised operation sequence |
| F20 | Replaying any financial request with its idempotency key is a no-op |
| F21 | R1 and R4 use the currency's `rounding_mode`; changing it changes the result, with no code change |
| F22 | R2 and R3 use `DOWN` regardless of the currency's mode, for every mode in §3.4.1 |
| F23 | Every §3.4.4 worked example reconciles exactly, at exponents 0, 2, and 3 |
| F24 | Formatting and parsing round-trip correctly for exponents 0, 2, and 3 — no assumption of 2 anywhere |
| F25 | A currency's minor-unit exponent is read from `currencies`, never inferred from the country |
| F26 | Cash rounding to `cash_rounding_increment` applies only to cash methods; electronic amounts are unaffected |
