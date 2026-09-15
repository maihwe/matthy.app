# 07 — Loan Rules

**Status:** Phase 0 draft, awaiting review
**Derives from:** 01_PRODUCT_REQUIREMENTS, 03_DATABASE_DESIGN, 05_ROLE_PERMISSION_MATRIX, 06_FINANCIAL_RULES, Master Development Prompt §14–§17, §44
**Depends on:** 06
**Decisions applied:** Q2 (no interest default), Q4 (no loan penalties in MVP)

---

## 1. Principles

**L1 — There is no universal loan rule.** §15: *"Do not hard-code one universal loan
rule."* The eligibility engine and the interest configuration are per-group, bounded by a
platform ceiling.

**L2 — Interest is never assumed.** Q2's decision: a scheme cannot be saved without an
explicit choice of interest method. There is no default, because a default would be the
platform inventing a financial rule (§52) — and because which methods are lawful is not
yet confirmed (regulatory checklist §2.7.5).

**L3 — The schedule is the artifact.** Interest and principal are split **at schedule
generation**, not at payment time (03 §9.4). Every payment follows a schedule that
already exists, so a member's obligation never depends on the order payments arrived in.

**L4 — A loan is only ever as good as its guarantors' consent.** §16's workflow is
consent-gated, and a loan does not advance past `GUARANTOR_PENDING` until every required
guarantor has actively accepted.

**L5 — Every decision is explicable after the rules change.** `eligibility_snapshot`
(03 §9.1) makes a past decision reviewable against the rules that produced it.

---

## 2. Eligibility engine (§15)

### 2.1 Design

A configurable rule set, evaluated server-side, returning a decision **and a factor
breakdown** (04 §11.1). The engine is a rules evaluator, not a hard-coded function:

```python
@dataclass
class EligibilityResult:
    eligible: bool
    max_amount: int | None          # minor units
    factors: list[FactorResult]     # each: name, value, required, met
    evaluated_at: datetime
```

`FactorResult` is returned whether the outcome is yes or no, because §15's factors exist
to make the decision legible. A rejection with no explanation is a support ticket; a
rejection with a factor breakdown is a member who knows what to do next.

### 2.2 Factors (§15)

Every factor §15 lists, with its default configuration:

| Factor | Type | Default | Notes |
|---|---|---|---|
| `membership_duration_months` | Minimum | 6 | Months of active membership |
| `contribution_history_months` | Minimum | 3 | Months with at least one contribution |
| `contribution_consistency` | Minimum ratio | 0.80 | On-time contributions ÷ expected, over a lookback window |
| `total_contributions_min` | Minimum | — | Absolute floor |
| `existing_loans_max` | Maximum | 1 | Concurrent active loans |
| `outstanding_debt_max` | Maximum | — | Absolute ceiling on current debt |
| `repayment_history_min` | Minimum ratio | 0.90 | On-time repayments ÷ due, for members with prior loans |
| `max_loan_amount` | Ceiling | — | Group-set absolute cap |
| `max_loan_multiple` | Multiple | 3× | Max loan as a multiple of total contributions |
| `guarantors_required` | Count | 2 | 0–5 |
| `guarantor_capacity_ok` | Boolean | true | Each guarantor has capacity for the guaranteed amount |
| `group_rules_custom` | Custom | — | Group-authored conditions within platform bounds |

**`repayment_history_min` is not applied to a first-time borrower.** A member with no
loan history has a perfect record by definition and a nonexistent one by fact; treating
"no data" as either pass or fail is wrong. The factor reports `met: null` and the engine
handles the null case explicitly, per group configuration.

**`contribution_consistency` uses scheduled-versus-actual, not a count.** A member who
contributed ₦500 on time for twelve weeks and one who contributed ₦6,000 once both have
"12 contributions" in some counting schemes. Consistency is measured against
`contribution_schedules`, which is why that table exists (03 §6.5).

### 2.3 Evaluation

```
1. Load the group's active rule set
2. Load the member's history (membership, contributions, loans, repayments)
3. Evaluate each factor
4. Determine max_amount:
     min(max_loan_amount,
         total_contributions × max_loan_multiple,
         outstanding_debt_max − current_debt)
     … floored at zero
5. eligible = every blocking factor met AND max_amount > 0
6. Return result with the full factor breakdown
```

**Blocking versus advisory.** A factor is blocking or advisory per group configuration.
`contribution_consistency` might be blocking in a formal cooperative and advisory in a
traders' group. The distinction is configuration (§48), not code.

### 2.4 The platform ceiling

§15: groups configure their lending rules *"within platform limits."* The ceiling is
`platform_settings.loan_rules_ceiling` (03 §12.4) and bounds:

- Maximum interest rate
- Maximum loan multiple
- Minimum required guarantors
- Maximum loan term

A group's configuration that exceeds the ceiling is rejected at save time with a `422`
naming the specific bound exceeded — not silently clamped, which would leave the group
believing it had configured something it had not.

### 2.5 Eligibility snapshot

Recorded on submission (03 §9.1): the rule set version, every factor's evaluated value,
and the resulting decision.

**Why it matters:** §15 makes the rules configurable, so they *will* change. Without a
snapshot, a member rejected in March cannot be told in June why — the rules that rejected
them no longer exist to be read. The snapshot is what makes a configurable engine
reviewable rather than merely flexible.

---

## 3. Interest (Q2 — no default)

### 3.1 The rule

**A scheme cannot be saved without an explicit interest method.** The field is required,
nullable-by-default is rejected, and the API returns `422` naming the missing choice
(06 §12 F16).

This is deliberate friction. The alternative — defaulting to something reasonable —
means every group that never thought about it inherits a financial rule the platform
chose for them, which §52 prohibits.

### 3.2 Methods

| Method | Meaning |
|---|---|
| `NONE` | No interest. The member repays the principal. Groups charge service fees instead. |
| `FLAT` | Interest computed once on the original principal, spread across instalments. |
| `REDUCING_BALANCE` | Interest computed each period on the outstanding principal. |

### 3.3 `NONE`

```
total_repayable = principal
```

Interest columns are zero. `loan_schedules.interest_due = 0` for every instalment.

Legitimate and common: many Nigerian cooperatives and welfare associations lend
interest-free from a pooled fund, charging a flat service fee instead. A system that
assumed every loan bears interest would model these groups wrongly.

A service fee, where charged, is a `FEE`, not interest, and posts to `1220 Fees
Receivable` (§5.3).

### 3.4 `FLAT`

```
total_interest   = principal × rate × periods      (R1: the currency's rounding mode)
total_repayable  = principal + total_interest
```

Allocation across instalments follows **R2** (06 §3.4): compute the totals once, allocate
with floor division, assign the accumulated remainder to the final instalment.

**None of this arithmetic is Naira-shaped.** `principal` is minor units in whatever
currency the scheme is denominated in, and the results are rounded to that currency's
exponent by R1 — 2 for NGN, 0 for JPY, 3 for KWD (06 §3.4.4). The examples below use
NGN because that is what launches first, not because the rules assume it.

**Worked example — ₦100,000 at 5% flat monthly for 6 months:**

```
principal      = 10000000 minor  (₦100,000.00)
rate           = 5% per period
periods        = 6
total_interest = 10000000 × 0.05 × 6 = 3000000 minor  (₦30,000.00)
total_repayable= 13000000 minor  (₦130,000.00)

Instalment principal: 10000000 ÷ 6 = 1666666.67 → 1666666 each, remainder 4
                      5 × 1666666 = 8333330, final = 10000000 − 8333330 = 1666670
Instalment interest:  3000000 ÷ 6 = 500000 exactly, remainder 0

Instalments: 5 × (1666666 + 500000) = 5 × 2166666 = 10833330
             final: 1666670 + 500000 = 2166670
             total:  10833330 + 2166670 = 13000000  ✓
```

The sum is exact. No kobo is lost, and the member's total equals the quoted total.

**Disclosure concern, recorded rather than buried:** at 5% flat per month over 6 months
the *effective* annual rate is roughly double the quoted nominal rate, because interest
is charged on the full principal for the whole term even as the balance falls. This is
why the regulatory checklist's §2.6.2 (disclosure) and §2.7.5 (lawful rates) must be
answered before `FLAT` is enabled in production. The platform implements the method
faithfully; whether it may **offer** it, and what it must disclose, is counsel's answer.

### 3.5 `REDUCING_BALANCE`

The standard amortising loan. For each period:

```
interest_i    = outstanding × rate        (R1: the currency's rounding mode)
principal_i   = instalment − interest_i
outstanding   = outstanding − principal_i
```

where the level instalment is computed from the annuity formula:

```
instalment = principal × r / (1 − (1 + r)^−n)
```

computed with `Fraction` arithmetic and rounded once (R1) to minor units.

**The schedule is then generated iteratively and reconciled at the end:**

1. Compute the level instalment.
2. For each period, compute interest, then principal = instalment − interest.
3. Track the outstanding balance.
4. **The final instalment absorbs the accumulated rounding difference** (R2), so the
   schedule sums exactly to `total_repayable` and the outstanding balance reaches
   exactly zero.

**Worked example — ₦100,000 at 5% monthly reducing for 6 months:**

```
principal  = 10000000 minor, r = 0.05, n = 6
instalment = 10000000 × 0.05 / (1 − 1.05^−6) ≈ 1970174 minor  (₦19,701.74)

Period  Outstanding      Interest    Principal    Instalment
  1     10000000         500000      1470174      1970174
  2      8529826         426491      1543683      1970174
  3      6986143         349307      1620867      1970174
  4      5365276         268264      1701910      1970174
  5      3663366         183168      1787006      1970174
  6      1876360          93818      1782542      1876360   ← final absorbs difference
                                    ─────────
        total principal             10000000
        total interest               1821048
        total repayable             11821048   ✓
```

Total interest ₦18,210.48 against ₦30,000.00 for the same nominal rate under `FLAT` —
the illustration of why the method must be disclosed and why it must never be defaulted.

**In the final period, interest is computed on the outstanding balance and the principal
component is whatever remains.** The final instalment is therefore usually slightly
smaller than the others. This is correct and must be shown to the member as such rather
than smoothed over — an unexplained smaller last payment looks like an error.

### 3.6 Rate and period

`loans.interest_rate` is `NUMERIC(9,6)` and **periodic**, not annual. A 5%-per-month
rate is stored as `0.050000` with the period implied by `repayment_pattern_id`.

Displaying a periodic rate as though it were annual, or vice versa, is a
misrepresentation. The API returns the rate with its period explicit and the client never
derives one from the other (04 §11).

---

## 4. Guarantors (§16)

### 4.1 Rules

| Requirement | Detail |
|---|---|
| L-GUA-001 | A loan may require zero or more guarantors, per group configuration (`guarantors_required`, 0–5). |
| L-GUA-002 | Workflow: applicant selects → guarantor notified → guarantor reviews → approve/reject → loan proceeds. |
| L-GUA-003 | **A loan does not leave `GUARANTOR_PENDING` until every required guarantor has accepted.** |
| L-GUA-004 | A rejection by any required guarantor returns the loan to the applicant rather than failing it outright — the applicant may propose a substitute. |
| L-GUA-005 | The decision records guarantor, loan, decision, date, time, IP/device, and comments (§16). |
| L-GUA-006 | **A guarantor cannot approve outside their permission scope** (FR-LOAN-010). |
| L-GUA-007 | A guarantor must be an active member of the same group. |
| L-GUA-008 | A guarantor cannot guarantee their own loan. |
| L-GUA-009 | A member's outstanding guarantees count against their own borrowing capacity. |
| L-GUA-010 | A guarantor may withdraw before disbursement; after disbursement, withdrawal requires group officer action. |

### 4.2 Why L-GUA-004 returns rather than fails

A guarantor declining is information, not a verdict. The applicant may have simply asked
the wrong person. Failing the whole application would force a fresh application and lose
the eligibility evaluation and any documents already attached.

### 4.3 Why L-GUA-009 exists

Without it, a member can guarantee unlimited obligations and then borrow against a
capacity that is already encumbered. Guaranteeing is a contingent liability, and a
system that does not count it will happily authorise borrowing that two people cannot
both honour.

### 4.4 Enforcement boundary

L-GUA-006 is enforced **in the service layer**, not by a database constraint, because
"scope" is a permission-graph question the database cannot answer (03 §9.5). It therefore
carries a mandatory test (13). The others are service-layer rules with model-level
validation where the data supports it.

---

## 5. Repayment (§17)

### 5.1 Schedules

`loan_schedules` is generated once at approval and is the artifact every payment follows
(03 §9.4). Each row carries `expected_amount`, `principal_due`, `interest_due`,
`fee_due`, `paid_amount`, and `status`.

### 5.2 Allocation

A repayment is allocated across components in the R3 priority order (06 §3.4):

```
1. Penalty   (unused in the MVP — Q4)
2. Fees
3. Interest
4. Principal
```

and the components sum exactly to the payment (03 §9.6 constraint).

**This ordering is a policy choice with member consequences and must be reviewed under
regulatory checklist §2.7.6.** Paying penalty and fees before principal means interest
accrues for longer on a larger balance than the alternative — a difference that is real
money to a borrower. It is stated here, implemented once, and flagged for review, rather
than emerging from whichever branch a developer wrote first.

### 5.3 Components

| Component | Account | Notes |
|---|---|---|
| Principal | `1200` Loan Receivable | |
| Interest | `1210` Interest Receivable | Interest is *not* recognised at disbursement (§4.8 of 06) |
| Fees | `1220` Fees Receivable | Service charges — the `NONE`-interest case uses this |
| Penalty | — | **Unused in the MVP** (Q4). `penalty_component` defaults to 0. |

### 5.4 Repayment frequencies

Daily, weekly, monthly, and other configured patterns (§17), served by the same
`schedule_patterns` table as contributions (03 §6.1). Daily repayment exists for the
daily-collection use case and is collected through the same collector workflow as
contributions — which is why `loan_repayments` carries a `collector_id` (03 §9.6).

### 5.5 Early repayment

Permitted. The remaining schedule is settled:

- **`REDUCING_BALANCE`**: interest is earned only to the settlement date, so the member
  pays outstanding principal plus interest accrued to date. Settling early must not
  charge unearned interest — a schedule that charges it would make early repayment
  punitive and is a consumer-protection problem.
- **`FLAT`**: interest was contractually computed on the full term at origination. The
  treatment of unearned interest on early settlement is a disclosure question under
  §2.7.5 and is **not decided here**; the group's configuration records which treatment
  applies, and the default until counsel answers is to settle at `total_repayable`
  remaining rather than to discount.
- **`NONE`**: outstanding principal.

### 5.6 Partial repayment

Accepted. The schedule's `status` becomes `PARTIAL` and `paid_amount` is updated. The
outstanding balance carries forward; the loan is not considered in arrears on a partly
paid instalment until its due date passes.

### 5.7 Overpayment

The excess is applied to the next unpaid instalment. If no instalment remains, it is
rejected as a `422` — a loan does not carry a credit balance, and silently accepting one
creates a liability the ledger has no account for.

---

## 6. Lifecycle (§14)

### 6.1 States

§14's statuses, with their transitions:

```
DRAFT ──► SUBMITTED ──► UNDER_REVIEW ──► GUARANTOR_PENDING ──► APPROVED
  │           │              │                  │                 │
  │           │              │                  │                 ▼
  │           │              │                  │        DISBURSEMENT_PENDING
  │           │              │                  │                 │
  │           │              │                  │                 ▼
  │           │              │                  │             DISBURSED ──► ACTIVE
  │           │              │                  │                             │
  ▼           ▼              ▼                  ▼                     ┌───────┼────────┐
CANCELLED  CANCELLED      REJECTED           REJECTED                  ▼       ▼        ▼
                                                                    OVERDUE  COMPLETED  DEFAULTED
```

| Transition | Actor | Requires |
|---|---|---|
| `DRAFT → SUBMITTED` | Applicant | Eligibility satisfied; required documents present |
| `SUBMITTED → UNDER_REVIEW` | `loans.review` | — |
| `UNDER_REVIEW → GUARANTOR_PENDING` | `loans.review` | `guarantors_required` > 0 |
| `UNDER_REVIEW → APPROVED` | `loans.approve` | `guarantors_required` = 0 |
| `GUARANTOR_PENDING → APPROVED` | `loans.approve` | **All required guarantors accepted** |
| `GUARANTOR_PENDING → REJECTED` | `loans.reject` | Reason |
| `APPROVED → DISBURSEMENT_PENDING` | `disbursements.request` | A disbursement is created |
| `DISBURSEMENT_PENDING → DISBURSED` | System | Disbursement reaches `COMPLETED` |
| `DISBURSED → ACTIVE` | System | First repayment recorded, or immediately |
| `ACTIVE → OVERDUE` | System | An instalment passes its due date unpaid |
| `ACTIVE/OVERDUE → COMPLETED` | System | Outstanding balance reaches zero |
| `ACTIVE/OVERDUE → DEFAULTED` | `loans.approve` | Group's default policy threshold met |

Any transition not listed is a `409`. A loan cannot skip approval to reach disbursement,
whatever the caller's permissions (05 §6).

### 6.2 Approval authority

`loans.approve` is held by `GROUP_OWNER`, `CHAIRMAN`, and optionally `TREASURER`
(05 §5.2). §44 rule 6 applies in spirit: **an applicant cannot approve their own loan**,
enforced by the same trigger pattern as disbursements (03 §10.2.1), because a group owner
who can approve their own borrowing has an unlimited line of credit on other members'
money.

### 6.3 Overdue (§17)

| Trigger | Action |
|---|---|
| Instalment due date passes | `loan_schedules.status` → `OVERDUE`; loan → `OVERDUE` |
| N days overdue (group-configured) | Notification to borrower and group officers (§23 trigger 9) |
| M days overdue | Flagged on the group dashboard |
| Threshold met | Eligible for `DEFAULTED` by decision of `loans.approve` |

**No automatic penalty accrues in the MVP** (Q4). Overdue is a state, not a fee, until a
future release enables penalties deliberately.

### 6.4 Default

`DEFAULTED` is a **human decision**, not a timer. The threshold is group-configured, but
the transition requires a user holding `loans.approve`. Automating default would be the
platform making a determination about a member's creditworthiness, with consequences for
their guarantors, on the basis of an elapsed duration alone.

On default, the outstanding balance is flagged. §17 requires only that the system
*manage* overdue loans; write-off, guarantor liability, and recovery are group policies
the platform supports but does not decide — the same posture as collector variances (06
§5.3).

---

## 7. Not in scope for the MVP

Recorded so the boundary is visible:

- Loan penalties (Q4) — `penalty_component` exists, defaults to 0, unused
- Risk-based pricing
- Automated credit scoring beyond the eligibility engine
- Loan restructuring, moratoria, and refinancing
- Secondary market for loan positions

---

## 8. Test obligations

Carried into 13_TESTING_STRATEGY:

| # | Test |
|---|---|
| L1 | A scheme cannot be saved without an explicit interest method (Q2) |
| L2 | `NONE`: total repayable equals principal; all interest components zero |
| L3 | `FLAT`: §3.4's worked example reconciles exactly to the kobo |
| L4 | `REDUCING_BALANCE`: §3.5's worked example reconciles exactly; final instalment absorbs the difference |
| L5 | Every generated schedule sums exactly to `total_repayable` — property test over random principal, rate, term, **and currency exponent (0, 2, 3)** |
| L6 | A loan cannot leave `GUARANTOR_PENDING` until every required guarantor accepts |
| L7 | A guarantor cannot approve outside their scope (FR-LOAN-010) |
| L8 | A member cannot guarantee their own loan |
| L9 | Outstanding guarantees reduce borrowing capacity (L-GUA-009) |
| L10 | An applicant cannot approve their own loan |
| L11 | A repayment's components sum exactly to the repayment amount |
| L12 | R3 allocation order is penalty → fee → interest → principal |
| L13 | Early settlement on `REDUCING_BALANCE` does not charge unearned interest |
| L14 | Overpayment beyond the final instalment is rejected |
| L15 | `eligibility_snapshot` reproduces the original decision after the rules change |
| L16 | A group rule exceeding `loan_rules_ceiling` is rejected with a `422` naming the bound |
| L17 | `repayment_history_min` reports `met: null` for a first-time borrower, and the engine handles null |
| L18 | Invalid lifecycle transitions return `409` |
| L19 | Concurrent repayments do not double-allocate or lose an update |
| L20 | `DEFAULTED` cannot be reached without a human decision |
