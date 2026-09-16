<!--
The self-review record of 11 §3.3. 11 §3.2 is why this exists: with one builder, a
required approving review is either a permanent block or a second account rubber-stamping
itself. Neither is a review. This is a real one — answered at a different time from when
the code was written, which is the only property that makes self-review worth anything.
-->

## What this changes

<!-- One paragraph. What, and why now. -->

## Self-review (11 §3.3)

**1. What could this break that the tests do not cover?**

<!-- If the answer is "nothing", say what you checked to conclude that. -->

**2. Is there a path where money moves without a ledger entry, or a ledger entry without an audit record?**

<!-- Phase 3 onwards. Asked now so it becomes habit rather than something remembered
     under pressure. Answer "not applicable — no money moves in this slice" when true. -->

**3. What did I assume about the caller that the code does not enforce?**

<!-- Auth state, permissions, ordering, idempotency, that a field is present.
     An assumption the code does not enforce is the one that becomes a production bug. -->

**4. If this is wrong in production, how will I know — and before a member does?**

<!-- A log line, an alert, a reconciliation check, a failing test. "It would be obvious"
     is not an answer. -->

## Slice and scope

- Slice: <!-- S0 / S1 / … -->
- Requirement IDs: <!-- FR-XXX-###, NFR-XXX-### -->
- Test obligations closed: <!-- 13 §8 IDs, or "none in this slice" -->

## Gates (11 §6)

All ten must pass. Tick after CI is green — not after a local run.

- [ ] 1–2. `ruff format --check` and `ruff check`
- [ ] 3. `mypy`
- [ ] 4. `lint-imports`
- [ ] 5. `makemigrations --check --dry-run`
- [ ] 6. Tests — 6a unit, 6b integration (real PostgreSQL), 6c API, 6d concurrency, 6e security
- [ ] 7. Coverage floor for this scope met
- [ ] 8. Secret scan
- [ ] 9. Dependency audit
- [ ] 10. OpenAPI schema — regenerated, and `docs/openapi.yaml` committed if endpoints changed

## Financial and security checks

Delete the rows that do not apply; do not tick them.

- [ ] **No `float` reached a money value.** Integer minor units throughout; decimal strings at the API boundary (02 §5.1)
- [ ] **Transaction boundaries are explicit.** No `ATOMIC_REQUESTS`; services own their `transaction.atomic()` (02 §5.3)
- [ ] **Idempotency** is stored in PostgreSQL with `UNIQUE (scope, key)`, not in Redis (04 §1.5)
- [ ] **Permissions** are checked by `has_perm(user, group, code)`, not by a role name string (05 §8)
- [ ] **Separation of duties** is enforced by a database trigger, not only in Python (05 §6)
- [ ] **Nothing sensitive is logged.** No password, PIN, token, OTP or account number (SEC-LOG / DEP-LOG-003)
- [ ] **No secret in the diff.** Not in code, tests, fixtures, Compose files or CI (SEC-SEC-001)
- [ ] **No large binary** (11 §5.1)

## Migrations

- [ ] No migration needed
- [ ] Migration included and reviewed — **and reversible, or the irreversibility is stated here**
- [ ] Backfill is separate from the schema change, and safe to run on a live table

<!-- 12 §5.1: migrations never run implicitly. A migration that is applied by a container
     start is a migration nobody reviewed the timing of. -->

## Documentation

- [ ] No document needed changing
- [ ] Affected document updated <!-- 03 for schema, 04 for endpoints, 05 for permissions,
     06 for posting rules, and PHASE_1_PLAN for a decision or slice change -->

## Deployment notes

<!-- Anything an operator must do, in order, before or after this merges.
     "None" is a valid answer and a useful one. -->
