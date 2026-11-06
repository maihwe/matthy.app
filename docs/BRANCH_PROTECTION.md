# Branch Protection — the manual half of 11 §6

`scripts/ci.sh` and `.github/workflows/ci.yml` are files: they are versioned,
reviewed, and diffable. Branch protection is **not** a file. It lives in GitHub's
settings, nothing in the repository records whether it is on, and a repository with all
ten gates configured and none of them required is a repository where a red build merges.

This document is the checklist to run once, by hand, and to re-run whenever someone
wonders whether it was ever done.

Manage it through GitHub settings or an explicitly authorized administrator CLI
session. Do not give repository CI the credential that can change or remove its
own branch protection.

**Configured and verified on 2026-11-06:** `main` and `develop` have active rulesets.
The saved configuration is recorded in §9 and [PROJECT_HANDOVER.md](PROJECT_HANDOVER.md).

---

## 1. Create the branches first

11 §2 defines the model. Both long-lived branches must exist before protection can be
applied to them — GitHub cannot protect a branch that does not exist.

```bash
git switch main
git push -u origin main
git switch -c develop
git push -u origin develop
```

| Branch | Role |
|---|---|
| `main` | Always deployable. Every commit on it is a release candidate. |
| `develop` | Integration. Feature branches merge here first. |
| `feature/*`, `fix/*`, `chore/*` | Branched from `develop`, merged back into it. |
| `hotfix/*` | Branched from `main`, merged into **both** `main` and `develop`. |

The hotfix rule is the one people get wrong: merging a hotfix into `main` alone means
the next release from `develop` silently reintroduces the bug.

---

## 2. `main` — the strictest rules

Repository → Settings → Rules → Rulesets → New branch ruleset. Target `main`.

| Setting | Value | Why |
|---|---|---|
| Require a pull request before merging | **on** | 11 §3.1: no direct commits to `main` |
| Required approvals | **0** | See §4 below — this is deliberate, not an oversight |
| Dismiss stale approvals on new commits | **on** | An approval of a commit that no longer exists approves nothing |
| Require review from Code Owners | off | There is no second owner to name |
| Require status checks to pass | **on** | The ten gates of 11 §6 |
| Require branches to be up to date before merging | **on** | A green run on a stale base is not a green run on what will merge |
| Require conversation resolution | **on** | A review comment that was never answered is a question left in the code |
| Require linear history | **on** | A merge commit on a release branch makes `git bisect` guess |
| Require signed commits | optional | Worth it if a signing key exists; not a Phase 1 blocker |
| Block force pushes | **on** | 11 §5.1 — a force-push to `main` can discard a commit that was the only copy of a fix |
| Restrict deletion | **on** | — |
| Restrict who can push | on, limit to the owner | Belt and braces alongside the PR requirement |

### Required status checks

Add these by their **exact job names**, which are the `name:` fields in
`.github/workflows/ci.yml`. GitHub matches on the name, not the job ID — renaming a job
silently un-requires it, which is a protection that disappears without an error.

- `1-2. Format & lint`
- `3. Types`
- `4. Import contracts`
- `5. Migrations`
- `6-7. Tests & coverage`
- `8. Secret scan`
- `9. Dependency audit`
- `10. OpenAPI schema`

A check that has never run cannot be selected from the list. Push a trivial PR first so
all eight appear, then add them.

---

## 3. `develop` — the same, minus approval theatre

Same ruleset as `main` except:

- **Require status checks: on** — the same eight. A gate that only runs on `main` finds
  problems one branch too late.
- **Require linear history: off** — integration branches may squash or rebase; the choice
  is the author's.
- **Block force pushes: on** — still.

---

## 4. Why "required approvals" is 0, and what replaces it

11 §3.2 is explicit that **a phantom review step is worse than an absent one.** There is
one person building this project. A GitHub rule requiring one approving review from a
different account means either:

1. every PR is blocked forever, and the rule is disabled in a hurry after the third time
   it gets in the way — taking the status-check requirement with it, because they are
   edited on the same screen; or
2. a second account is created to approve, which is a review step that produces a
   green tick and no review. That is the phantom.

Neither is a control. What replaces it is a check that can actually fail, and a written
artifact:

- **The ten gates are required.** These are real and they run.
- **The PR body records a self-review against 11 §3.3's four questions.** They are
  reproduced in §5 and the PR template prompts for them. The value is not the ceremony;
  it is that answering them is a distinct act from writing the code, done at a different
  time, by someone who has since stopped thinking about how clever the implementation is.
- **Concurrency, reconciliation and security suites run on every PR**, not nightly
  (`.github/workflows/ci.yml`, the `tests` job). These are the classes of bug that pass
  every other gate and corrupt data in production.

If a second maintainer joins, set required approvals to 1 and enable Dismiss stale
approvals. Record the change here.

---

## 5. The PR self-review record

Paste into the PR body, answered, every time. 11 §3.3:

1. **What could this break that the tests do not cover?**
2. **Is there a path where money moves without a ledger entry, or a ledger entry without
   an audit record?**
3. **What did I assume about the caller that the code does not enforce?**
4. **If this is wrong in production, how will I know — and before a member does?**

Question 2 is Phase 3 onwards, but it is asked from now on so that it is habit rather
than a thing remembered under pressure.

---

## 6. Environments and secrets

| Environment | Protection | Secrets |
|---|---|---|
| `staging` | none | Staging credentials only. SEC-SEC-005: never shared with production |
| `production` | **Required reviewers: on** | Production credentials. The only place they exist |

Production secrets go in Repository → Settings → Environments → `production` →
Environment secrets, **never** in Repository secrets. Repository secrets are readable by
every workflow on every branch, including a workflow in a PR from a branch that has not
been reviewed.

SEC-SEC-001: no secret in source, ever. Gate 8 (`gitleaks`) scans history on every push,
because a secret that reached a commit is in the history permanently — rotating it is the
only remedy, and rotation is a manual step this repository cannot perform.

---

## 7. Verification

Run this to confirm the ruleset is actually in force, rather than merely intended:

```bash
gh api repos/{owner}/{repo}/rulesets --jq '.[].name'
gh api repos/{owner}/{repo}/rules/branches/main \
  --jq '.[] | select(.type == "required_status_checks") | .parameters'
gh api repos/{owner}/{repo}/rules/branches/develop \
  --jq '.[] | select(.type == "required_status_checks") | .parameters'
```

Both branch-rules responses must list all eight check names from §2 and
`strict_required_status_checks_policy: true`. Inspect the full effective rules for
the PR, discussion-resolution, force-push/deletion and main linear-history controls.
The branch API should report `protected: true` for both branches.

`branches/main/protection` reads classic branch protection; it may return 404 when
active rulesets provide protection. That response alone does not establish that a
branch is unprotected. The effective branch-rules endpoints above verify rulesets.

---

## 8. Checklist

- [x] `develop` branch created at the passing `main` implementation baseline
- [x] Ruleset on `main`: PR required, linear history, no force push, no deletion
- [x] Ruleset on `develop`: PR required, no force push, no deletion
- [x] All eight checks have run — S4/S5 PR #2 and merged-commit CI passed
- [x] All eight check names added as required on both branches
- [x] "Require branches to be up to date before merging" enabled
- [x] "Require conversation resolution" enabled
- [ ] `production` environment created with required reviewers
- [x] Effective branch-rules and protected-branch verification completed; results saved below

The production environment/reviewer configuration was not verified or created in
this session and remains an operational setup item.

## 9. Verified setup — 2026-11-06

Repository: `maihwe/matthy.app`. `develop` was created from passing `main` commit
`733691a730e61dce84aa64ff6640bfa83f373293`.

| Ruleset | Target | Verified enforcement |
|---|---|---|
| [26717900 — Main branch protection](https://github.com/maihwe/matthy.app/rules/26717900) | `main` | Active; PR/check/discussion requirements, linear history, squash/rebase only, no deletion/non-fast-forward updates, no bypass actors |
| [26717994 — Develop branch protection](https://github.com/maihwe/matthy.app/rules/26717994) | `develop` | Active; PR/check/discussion requirements, all merge methods allowed, no deletion/non-fast-forward updates, no bypass actors |
| [26718024 — Owner administration of protected branches](https://github.com/maihwe/matthy.app/rules/26718024) | Both | Active; update restriction, bypassable only by repository administrators |

The administrator bypass applies only to the separate update restriction. It does
not bypass either primary ruleset's PR or CI requirements. `maihwe` was the sole
collaborator/administrator when verified; adding administrators would extend update
permission and calls for reviewing the owner-only policy.

Both effective branch-rules endpoints returned all eight required check names,
bound to GitHub Actions app ID `15368`, with strict up-to-date checking. PR rules
returned zero approving reviews, stale-review dismissal and discussion resolution
enabled; code-owner, last-push and extra unattributed-change approvals disabled.
The branches API returned:

```json
{"name":"main","protected":true,"sha":"733691a730e61dce84aa64ff6640bfa83f373293"}
{"name":"develop","protected":true,"sha":"733691a730e61dce84aa64ff6640bfa83f373293"}
```

This is a dated configuration record. Recheck GitHub before relying on it after
subsequent settings changes; documentation does not enforce protection by itself.
