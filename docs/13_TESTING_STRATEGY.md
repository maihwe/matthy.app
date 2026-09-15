# 13 — Testing Strategy

**Status:** Phase 0 draft, awaiting review
**Derives from:** 01_PRODUCT_REQUIREMENTS, 06_FINANCIAL_RULES, 07_LOAN_RULES, 08_DISBURSEMENT_RULES, 09_NOTIFICATION_DESIGN, 10_SECURITY_REQUIREMENTS, Master Development Prompt §37, §44, §47
**Depends on:** 06, 07, 08

---

## 1. Principles

**Y1 — Tests are the only reviewer.** §41's workflow assumes a code review, and this
project has one builder (11 §3.2). CI is therefore not a safety net over review; it *is*
the review, and it is configured accordingly. A financial PR that cannot pass CI has not
been reviewed, not merely failed to be verified.

**Y2 — Correctness over coverage.** A coverage percentage is a poor proxy for a ledger
that balances. 100% coverage of code that never asserts a posting pair proves nothing.
Coverage is tracked and gated at a stated floor, and it is not the headline metric —
§44's invariants are.

**Y3 — Financial tests assert postings, not responses.** *"Contribution recorded"* is a
`201`; *"Debit `1000` Credit `2000` for exactly 100000 minor units"* is a fact. Every
financial test asserts at the ledger layer (04 §6, 06 §4.13), and the HTTP assertion is
secondary.

**Y4 — The invariant is tested on every path, not in a dedicated suite.** A dedicated
"ledger balances" test proves the ledger balances for the one thing that test did. The
balance assertion belongs in the fixture teardown, so it runs after **every** test that
touched the ledger — the same reason the database enforces it at COMMIT (03 §2.4.1).

**Y5 — Tests must be able to fail for the right reason.** A test that passes because a
provider returned `200` from a mock, or because a timestamp happened to sort the right
way, is a test that will pass in production too. Determinism is a correctness
requirement, not hygiene.

**Y6 — Never test against real money or real providers.** `MockProvider` for payments
(02 §5.8), `MockAdapter` for every notification channel (09 §10), no outbound network in
tests. Enforced by CI network policy, not by convention (§10 gate 7).

---

## 2. Categories (§37)

§37's categories, mapped to the pyramid level that carries them:

| §37 Category | Level | Carries |
|---|---|---|
| Unit tests | Base | Money arithmetic, allocation rules, schedule generation, permission resolution, eligibility factors |
| Integration tests | Middle | Service + database, postings, constraints and triggers, state machines |
| API tests | Middle | Endpoint contracts, status codes, error envelopes, pagination, auth |
| Financial calculation tests | Base | Every rounding rule and every posting pair (06 §4.13) |
| Permission tests | Middle | The full matrix (05 §5) |
| Concurrency tests | Middle (special) | Lock ordering, double-spend, lost update |
| Security tests | Top + static | Auth, authz, injection, enumeration, signing |
| Load & performance tests | Top | §38's workload-shaped scenarios (12 §10) |
| Edge-case tests | Base + Middle | Zero amounts, boundary dates, empty sets, maximum values |
| Reconciliation tests | Top (scheduled) | Ledger ↔ provider ↔ derived caches |

Distribution follows the pyramid: most tests are unit and integration; the expensive
levels carry fewer, larger tests.

---

## 3. The pyramid

```
        ▲  Load & reconciliation          few, scheduled, slow
       ███  End-to-end journeys
     █████  API contract tests
   ███████  Integration (service + real PostgreSQL)
 ██████████  Unit (pure logic, no database)
```

**Integration tests run against real PostgreSQL.** Not SQLite: the schema depends on
`CHECK` constraints, deferred constraint triggers, `GENERATED` columns, `CITEXT`, and
`JSONB` (03 §2.4). A test suite on SQLite would pass while the production invariants
never fired — the worst possible outcome for Y4, because it would be a green build
proving the opposite of what it claims.

**Unit tests are genuinely unit.** Money arithmetic (`Money`, allocation, amortisation)
is pure and fast; it is tested without a database, which is why the rounding rules live
in a module with no model imports (02 §5.1).

---

## 4. Layout and naming

```
tests/
  unit/            money, allocation, schedules, eligibility, permissions
  integration/     ledger, constraints, triggers, state machines, idempotency
  api/             contract tests per endpoint group
  concurrency/     lock ordering, double-spend, lost update
  security/        auth, authz, enumeration, tampering
  reconciliation/  ledger ↔ cache ↔ provider
  e2e/             full journeys across subsystems
  factories/       shared factories and fixtures
  assertions/      the ledger assertion helpers (§6)
```

Naming states the behaviour:

```python
def test_settlement_with_shortfall_requires_explanation_before_verified(): ...
def test_requester_cannot_approve_own_disbursement_via_direct_sql(): ...
```

Not `test_settlement_3` or `test_disbursement_works`. A failing test name should be a
sentence the reader does not have to open the file to understand — which matters most
when the only reader is the person who wrote it six months ago at 2am.

---

## 5. Determinism

| Requirement | Detail |
|---|---|
| Y-DET-001 | **Time is injected, never read from the clock.** `freezegun` or an injected clock; `django.utils.timezone.now()` is never called directly in business logic. |
| Y-DET-002 | Timezone: tests run in a fixed non-UTC timezone where the logic is timezone-sensitive (quiet hours, daily sweeps), so a UTC-only assumption fails in the suite rather than in Lagos. |
| Y-DET-003 | Randomness is seeded; a failing seed is printed and reproducible. |
| Y-DET-004 | No test depends on dictionary or queryset ordering unless the code explicitly orders. |
| Y-DET-005 | Money and IDs are never generated from `random` without a seed. |
| Y-DET-006 | Tests do not sleep. Concurrency is tested with real parallel transactions and locks, not `sleep`. |
| Y-DET-007 | No outbound network. |
| Y-DET-008 | Every test is independent; no ordering dependency within or across modules. |

Y-DET-002 is the one most often skipped. Quiet hours (09 §8) resolve against the user's
local time; a suite running only in UTC would never exercise the branch that gets it
wrong.

---

## 6. Ledger assertion helpers

The helpers that make Y3 and Y4 practical. In `tests/assertions/ledger.py`:

| Helper | Asserts |
|---|---|
| `assert_balanced()` | Every transaction in scope sums to zero. Called in teardown of any test that posted (Y4). |
| `assert_posted(debit=..., credit=..., amount=...)` | A specific pair exists with that amount |
| `assert_no_posting()` | Nothing was posted — for rejected operations |
| `assert_balance(account, expected, owner=...)` | Derived balance = natural − contra (06 §6.1) |
| `assert_identity(period)` | §13's identity holds (06 §6.3) |
| `assert_suspense_zero(group)` | `2100` is clear (F18) |
| `assert_derived_caches_match()` | `loans.outstanding_*` equals the ledger (F19) |

`assert_posted` takes the account codes as arguments rather than checking a serialised
response, so the test reads like the posting table in 06 §4.13. A reviewer comparing the
test to the spec should not need to translate.

---

## 7. Property-based testing

Applied where the input space is large and the invariant is exact. `hypothesis` for:

| Property | Invariant |
|---|---|
| Amortisation | A generated schedule sums **exactly** to `total_repayable`, for any principal, rate, and term (L5) |
| Allocation | Components sum exactly to the payment, for any payment amount and any balance composition (F10, L11) |
| Allocation order | Penalty → fee → interest → principal, exhausted in that order (L12) |
| Rounding | Every mode in 06 §3.4.1 is stable across the sign domain, and `HALF_UP` is genuinely half away from zero for negatives |
| Currency exponent | The same operation at exponents 0, 2, and 3 produces results consistent with its currency, with no path that assumes 2 |
| Allocation | For every mode, `DOWN` allocation plus remainder disposition still sums exactly to the total (F22, F9, F10) |
| Ledger | Any generated sequence of financial operations leaves the ledger balanced (F1, F19) |
| Reversal | Reverse(Reverse(T)) ≠ T but nets to zero with T — the chain is linear, not idempotent (F5, F6) |
| Minor units | Amounts round-trip through the API's decimal-string representation losslessly (F15) |

These are the tests that find the kobo. A worked example (07 §3.4) proves one case;
the property proves the rule.

---

## 8. The obligation register

Every labelled test obligation from the Phase 0 documents. **The source document owns the
wording**; this table is an index and a progress tracker, not a second copy — a
duplicated obligation would eventually diverge from its original.

| Source | Range | Count | Area |
|---|---|---|---|
| [05](05_ROLE_PERMISSION_MATRIX.md) §10 | T1–T12 | 12 | Roles, permissions, separation of duties |
| [06](06_FINANCIAL_RULES.md) §12 | F1–F26 | 26 | Ledger, postings, rounding, currency, idempotency |
| [07](07_LOAN_RULES.md) §8 | L1–L20 | 20 | Interest, schedules, guarantors, lifecycle |
| [08](08_DISBURSEMENT_RULES.md) §10 | D1–D16 | 16 | Workflow, approval, payment execution |
| [09](09_NOTIFICATION_DESIGN.md) §11 | N1–N14 | 14 | Delivery, preferences, campaigns |
| [10](10_SECURITY_REQUIREMENTS.md) §11 | S1–S14 | 14 | Auth, authz, data protection |
| | | **102** | |

Plus §37's categories that carry no per-document labels and are covered by the strategy
itself: API contract tests, load and performance tests (12 §10), and reconciliation.

**Every one of the 102 is a required test.** A document's obligations are not advisory —
they are the acceptance criteria for that document's implementation. Phase 1 is not
complete until they exist and pass, and the register is the checklist.

### 8.1 Cross-cutting obligations

Several obligations appear in more than one document under different labels because they
are load-bearing in more than one area. They are one test, referenced twice:

| Obligation | Labeled as |
|---|---|
| Requester cannot approve own disbursement | T5, D1 |
| Settlement variance requires explanation | F3, F4 |
| Idempotent replay | F13, F20, D7 |
| Concurrent writes do not lose updates | F11, F12, L19 |
| No secret in any response or log | S1, and 10 §5.2's logging rules |

This is intentional. The alternative — one label, one document — means the disbursement
document has no view of its own safety rule.

---

## 9. Coverage

| Scope | Floor | Rationale |
|---|---|---|
| `ledger/` | **100% branch** | Every branch is a posting. An uncovered branch is an unposted transaction. |
| `loans/`, `payments/`, `disbursements/`, `contributions/` | 95% branch | Financial |
| `accounts/`, `groups/`, permissions | 90% branch | Security-relevant |
| Everything else | 80% line | |

**A coverage drop fails CI** (§10, gate 7) — a new uncovered branch in `ledger/` is a
regression regardless of how good the rest of the PR is.

Coverage is a floor, not a goal. Where coverage and §44 conflict, §44 wins: a branch that
is unreachable because a database constraint prevents it is documented as such, not
tested through an application path that cannot execute it.

---

## 10. CI gates

[11 §6](11_TEAM_WORKFLOW.md) defines the ten gates every PR runs. This section expands
the one gate that is ours — **Tests** — into the suites that satisfy it, and states the
two test-specific conditions the gate list does not carry.

| # | Gate (11 §6) | Expanded here as | Fails on |
|---|---|---|---|
| 1 | Formatting | `ruff format` | Any file not formatted |
| 2 | Linting | `ruff` | Any finding |
| 3 | Type checking | `mypy` — strict on financial modules | Any error in `ledger/`, `payments/`, `loans/`, `disbursements/` |
| 4 | Import contracts | `import-linter` | A layer rule or the ledger inversion violated (02 §4.1, §5.4) |
| 5 | Migrations | `makemigrations --check` | A model change with no migration |
| 6 | **Tests** | **Suites 6a–6e below** | Any failure |
| 7 | Coverage | Threshold per §9 | Below floor, or **any drop in `ledger/`** |
| 8 | Secret scan | `detect-secrets` / `gitleaks` | Any finding |
| 9 | Dependency audit | `pip-audit` | **High-severity** finding (S14) |
| 10 | OpenAPI schema | Generated and diffed | A breaking API change without a version bump (04 §15) |

**Gate 6, expanded:**

| Suite | Runs |
|---|---|
| 6a | Unit — money, allocation, schedules, eligibility |
| 6b | Integration — ledger, constraints, triggers, state machines, idempotency (real PostgreSQL) |
| 6c | API contract — per endpoint group |
| 6d | Concurrency — lock ordering, double-spend, lost update |
| 6e | Security — auth, authz, enumeration, tampering |

Two conditions on top of 11 §6:

| Condition | Detail |
|---|---|
| **6b must run on real PostgreSQL.** | A SQLite run would pass while the deferred balance trigger and the `CHECK` constraints never fired — a green build proving the opposite of what it claims (§3). |
| **6d runs on every PR, not nightly.** | Concurrency bugs are the class most likely to pass every other gate and then corrupt data in production, and a nightly run finds them a day after the merge that caused them. |

Two further checks run outside the PR gate and fail the build if they regress:

| Check | Detail |
|---|---|
| Network isolation | Any outbound connection attempted during the suite (Y6) |
| Cross-document consistency | The 102 obligations in §8 still exist and still pass |

---

## 11. What is not tested automatically

Stated so the gap is visible rather than assumed covered:

| Area | Why | Compensating control |
|---|---|---|
| Real SMS/email delivery | Costs money, non-deterministic | Adapter mocks; provider sandbox before go-live (12 §11) |
| Real payment provider behaviour | Phase 3 decision (Q1) | Interface contract tests against `MockProvider`; Paystack sandbox before go-live |
| Mobile client behaviour | Separate codebase (§30–33) | API contract tests are the boundary; the contract is the test |
| Provider outage recovery | Needs a live provider | Queue-and-drain tested with a mock in failure mode |
| Regulatory compliance | Not a code property | 14's checklist and legal review (§45) |

The first two are tested against sandboxes **before go-live**, and that is a release
gate, not a test suite item — it is an item on 12 §11's checklist precisely because a
mock cannot prove a provider integration works.

---

## 12. Test data

| Requirement | Detail |
|---|---|
| Y-DAT-001 | Factories generate data; hand-written fixtures only where a specific value is the point. |
| Y-DAT-002 | **No production data in tests, ever** — not anonymised, not a subset, not once. |
| Y-DAT-003 | No real phone numbers, account numbers, or names in fixtures. |
| Y-DAT-004 | The test database is destroyed and rebuilt per run; no state carries between runs. |
| Y-DAT-005 | Financial fixtures are amounts that make arithmetic errors visible — ₦100,000.00 not ₦10.00, and a case with a non-zero remainder. |

Y-DAT-002 has no exception. A test database seeded from production is a copy of members'
financial data on a developer machine and in CI logs, which is the specific harm §44
rule 15's backup discipline exists to prevent (12 §8).

---

## 13. Reconciliation tests

The scheduled checks (06 §9) are tests too, and they run in production as well as in CI:

| Check | Frequency | Failure severity |
|---|---|---|
| `2100` suspense is zero | Daily | P2 — an open variance is expected; an *unexplained* one is not |
| Derived caches match the ledger | Nightly | **P1** — the ledger is truth and a cache disagrees |
| Every transaction sums to zero | Continuous (trigger) + nightly audit | **P1** |
| Provider settlement matches expected | Per settlement | P1 |
| Group balances match a recomputation from raw entries | Weekly | **P1** |

A nightly reconciliation failure pages, regardless of the hour (12 §7.2 — financial
integrity is a P1 alert category). These are the tests whose failure means the numbers
are wrong, which outranks every availability concern in the system.
