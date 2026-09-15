# 11 — Team Workflow

**Status:** Phase 0 draft, awaiting review
**Derives from:** Master Development Prompt §41, §42, §43, §47, §48
**Depends on:** —

---

## 1. Context: one builder, three workstreams

§41–43 describe three development groups working simultaneously — Financial Core,
Applications, Platform Services — and §43 requires API contracts to be agreed before
implementation so the groups can work in parallel.

**This project has one builder.** The three groups are therefore not a coordination
problem; they are a **workstream structure** — a way to keep three genuinely different
kinds of work from tangling. Each has a distinct rhythm:

| Workstream | §42 scope | Rhythm |
|---|---|---|
| **A — Financial Core** | Auth, users, groups, roles, schemes, contributions, collectors, payments, ledger, loans, guarantors, repayments, disbursements | Slow, test-first, correctness-critical. Most of the schedule. |
| **B — Applications** | Mobile app, member/collector/group interfaces, web admin, UX/UI | Consumes frozen API contracts. Faster iteration, lower blast radius. |
| **C — Platform Services** | Payments infra, email/SMS/push, Celery, Redis, Docker, CI/CD, monitoring, logging, backups, infrastructure, security, performance | Infrastructure-shaped; lands early then recedes. |

### 1.1 What is deliberately kept from §43

§43's requirement — *"Frontend and backend teams must work from the same contract"* —
is retained in full, and it is the most valuable part of the section even for one
person.

The failure it prevents is not inter-team miscommunication; it is **a client built
against an API that changed while it was being built**. That happens just as readily to
a solo builder, and it happens worst in the gap between writing the backend and writing
the client — which in a sequential build can be months.

So: **API contracts are frozen in 04_API_SPECIFICATION before either side is
implemented**, and a change to a frozen contract is a deliberate act with a version
consequence, never an incidental one. The contract is the boundary whether or not there
are two people on either side of it.

### 1.2 What is dropped

Parallel execution. Workstreams are sequential in practice, with one exception worth
taking: **C's infrastructure work (Docker, CI, test harness) lands in Phase 1**, before
it is needed, because writing tests with no way to run them is how testing gets
deferred, and deferred testing in a financial system is how §44 rules get broken.

---

## 2. Branch model (§41)

```
main          ──●──────────────────●────────────●──────►  production-ready, always
                 ╲                ╱ ╲          ╱
develop       ────●──●──●──●──●──●────●──●──●──────────►  integration
                   ╲    ╱   ╲   ╱
feature/*           ●──●     ●─●                           short-lived, one concern
```

| Branch | Purpose | Rules |
|---|---|---|
| `main` | Production-ready. Every commit is deployable. | **Protected. No direct pushes** (§41). Merge only via reviewed PR from `develop` or a hotfix branch. Tagged on release. |
| `develop` | Integration. Where features meet. | Protected. Merge only via PR. |
| `feature/*` | One unit of work | Branched from `develop`, short-lived, deleted on merge |
| `hotfix/*` | Production emergency | Branched from `main`, merged to `main` **and** back to `develop` |

§41: *"Do not allow random direct changes to main."* Enforced by branch protection, not
by intention.

### 2.1 Feature branches

Per §41, named for the workstream and the concern:

```
feature/authentication        feature/collector
feature/groups                feature/ledger
feature/contribution-schemes  feature/loans
feature/payments              feature/disbursement
feature/communications        feature/mobile
feature/admin-dashboard
```

A branch that spans two of those names is a branch doing two things, and should be two
branches. The names are deliberately coarse — they map to modules, and §47's rule is
that a module is finished before the next begins.

---

## 3. The workflow (§41)

```
Issue
  │
  ▼
Feature branch
  │
  ▼
Implementation          ← §47's sequence, per module
  │
  ▼
Tests                   ← written alongside, not after
  │
  ▼
Pull Request
  │
  ▼
Code review
  │
  ▼
CI                      ← all gates green
  │
  ▼
Merge
```

### 3.1 Issue first

Every non-trivial change starts as an issue stating: what changes, why, which
requirement IDs it satisfies (`FR-*`, `NFR-*`, `SEC-*`), and what could go wrong. The
requirement IDs are the point — they make a change traceable from spec to test to code,
and make "why does this exist?" answerable in six months.

### 3.2 The review step, honestly

§41 lists code review between PR and CI. **With one builder there is no second
reviewer**, and pretending otherwise is worse than admitting it — a phantom review step
is a control everyone believes exists.

What replaces it, in order of value:

1. **CI is the primary reviewer.** Linters, type checks, the full test suite, the import
   contracts from 02 §5.4, the migration checks from 03 §16, and a secret scan. These
   catch what review catches most often: the mechanical error.
2. **A written self-review on the PR**, before merging, answering four fixed questions
   (§3.3). Written down because the questions that go unasked are the ones that go
   unanswered.
3. **A cooling-off period for financial changes.** A PR touching `ledger/`,
   `payments/`, `loans/`, or `disbursements/` is not merged the same session it was
   written. Sleep on it, re-read the diff. This is cheap and catches a real class of
   error that no tool flags.
4. **An automated review pass** where available, treated as a second opinion rather
   than a gate.

**The honest assessment:** this is weaker than a second engineer. It is much stronger
than an unreviewed commit, which is the alternative actually on offer. The controls that
compensate are the database-level invariants in 03 — the balance trigger, the
separation-of-duties trigger, the audit immutability trigger. Those exist partly because
of this gap, and they are the reason a solo-built financial system can still be sound.

### 3.3 The self-review questions

Every PR answers these in its description:

1. **What happens if this fails halfway?** Is the operation atomic, or does a partial
   failure leave state that cannot be recovered?
2. **Can this move money twice?** Where is the idempotency key, and what makes a retry
   safe?
3. **Who can call this, and who should not be able to?** Which permission, checked
   where, and what does the test assert?
4. **What did I not test, and why?** An honest answer here is worth more than a
   coverage number.

### 3.4 Pull request requirements

A PR is not ready to merge unless:

- [ ] It links its issue and the requirement IDs it satisfies
- [ ] The four self-review questions are answered
- [ ] Tests are included and pass locally
- [ ] CI is green, including migration and import-contract checks
- [ ] Migrations (if any) follow 03 §16's financial-migration discipline
- [ ] New endpoints are in the OpenAPI schema
- [ ] New permissions are in 05's catalogue and matrix
- [ ] No new secret, no new `fields = '__all__'`, no new raw SQL with interpolation
- [ ] Documentation updated if behaviour or contract changed

---

## 4. Definition of done

§47 is prescriptive about how a module is built, and its rule — *"Each module must be
completed and tested before moving to the next major module"* — is the schedule.

```
Understand → Design → Database → Models → Serializers → Services
          → API → Tests → Documentation
```

A module is done when **all nine** are done. The two that get skipped are the last two,
and they are the two that make the next module cheaper, so they are not optional.

| Stage | Done means |
|---|---|
| Understand | Requirements read; ambiguities raised rather than assumed (§52) |
| Design | Approach recorded, alternatives noted |
| Database | Migrations written, indexed per 03 §14, constraints in place |
| Models | Domain invariants expressed in the model, not only in the service |
| Serializers | Explicit field allow-lists; no `__all__` |
| Services | All business logic here; views are thin (02 P4) |
| API | Versioned, permission-classed, in the OpenAPI schema |
| Tests | The module's row in 13's matrix is green |
| Documentation | Docs updated; contract frozen if it is a contract module |

---

## 5. Commits

```
<area>: <imperative summary>

Why this change exists, and what it affects.

Refs: FR-LED-009, FR-CON-003
```

- Present tense, imperative mood.
- The body explains **why**, not what — the diff already shows what.
- Requirement IDs referenced where they exist.
- One logical change per commit. A commit that fixes a bug and reformats a file is two
  commits, and the formatter one is noise in the blame.

### 5.1 Never commit

Secrets, `.env` files, database dumps, real member data, or large binaries. `.gitignore`
covers these; the pre-commit hook enforces it.

**A production data dump must never reach the repository.** It contains real people's
financial records, and once pushed it is in the history forever. Test fixtures are
generated, synthetically, from `scripts/`.

---

## 6. CI gates (§40)

Every PR runs, in order, failing fast:

| Gate | Tool | Blocks merge |
|---|---|---|
| Formatting | `ruff format` | Yes |
| Linting | `ruff` | Yes |
| Type checking | `mypy` (strict on `ledger/`, `payments/`, `loans/`, `disbursements/`) | Yes |
| Import contracts | `import-linter` (02 §5.4) | Yes |
| Migrations | `makemigrations --check`; no missing migrations | Yes |
| Tests | `pytest` with the 13 matrix as the target | Yes |
| Coverage | Threshold on financial modules (high), advisory elsewhere | Financial yes |
| Secret scan | `detect-secrets` / `gitleaks` | Yes |
| Dependency audit | `pip-audit` | Yes on high severity |
| OpenAPI schema | Generated and diffed — an undocumented endpoint change fails | Yes |

**Type checking is strict on the financial modules specifically.** A `float` where an
`int` was meant is the money bug this project most needs to not have, and `mypy` catches
it at the boundary before it reaches the ledger.

### 6.1 Migrations gate

03 §16's financial-migration discipline is enforced here:

- No destructive operation on a financial table without an explicit override and a
  recorded decision.
- A migration touching a money column must be accompanied by a test proving pre- and
  post-migration balances agree.
- Backfills run as management commands, not data migrations.

---

## 7. Releases

- Semantic versioning. The API version (`/api/v1/`) moves independently of the app
  version, and only on a breaking contract change.
- `main` is tagged on release (`v0.4.0`).
- A release includes a changelog entry noting contract changes and migrations.
- Deployments are from tags, not from branch heads.
- Database migrations run as a discrete, reversible-where-possible step, never
  implicitly on application start.

---

## 8. Branch protection

Configured on the remote, not merely documented:

| Setting | `main` | `develop` |
|---|---|---|
| Direct pushes | Blocked | Blocked |
| Force pushes | Blocked | Blocked |
| Deletion | Blocked | Blocked |
| Required status checks | All §6 gates | All §6 gates |
| Required review | 1 (waived while solo, **recorded as waived**) | 1 (same) |
| Linear history | Required | Preferred |

The review requirement is waived while this is a solo project rather than silently
absent — so that when a second contributor appears, the setting is already correct and
the waiver is a visible thing being removed rather than a control nobody added.

---

## 9. Environments and promotion (§40)

```
feature/*  ──► develop ──► staging ──► main ──► production
                 │            │                    │
              CI test      QA / smoke          monitored
              suite        verification        deployment
```

**Never develop directly against production** (§40). Production credentials are never
present on a development machine, and the production database is never a target for
local development, however convenient.

Promotion is a deliberate act at each arrow. A change that has not passed staging does
not reach production, including — especially — a "trivial" one, because the changes
misjudged as trivial are the ones that take the platform down.
