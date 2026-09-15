# 05 — Role & Permission Matrix

**Status:** Phase 0 draft, awaiting review
**Derives from:** 01_PRODUCT_REQUIREMENTS, 03_DATABASE_DESIGN, Master Development Prompt §5, §18, §25, §26, §44
**Depends on:** 01, 03

---

## 1. Principles

**R1 — Roles are scoped to a group.** There is no global "Treasurer". A user is
Treasurer *of a group*. §5's example is the rule:

```
Mathias
├── Group A → Member
└── Group B → Treasurer
```

Mathias's Treasurer permissions apply to Group B and nowhere else. This is why every
permission check in the system takes `(user, group, permission)` and not `(user,
permission)` — see §8.

**R2 — The role set is configurable, not fixed.** §5: *"Do not assume every group
requires a secretary."* A daily collection group with three roles is correct, not a
degenerate case of a formal association with six. Roles ship as **templates** per group
type, and a group may add, rename, or drop roles within platform bounds (§7).

**R3 — Permissions are data, not code.** A permission check reads
`group_permissions`. Adding a permission to a role is a row, not a deploy.

**R4 — Some rules are not configurable.** Whatever a group configures, §44's mandatory
rules hold: no self-approval of a restricted financial transaction, no unauthorised role
escalation, no bypassing of audit. These are marked ✗ below — never grantable, by
anyone, in any group. A configurable system that can be configured to violate §44 is a
system that will be.

**R5 — The minimum-privilege default.** A permission is granted only where a role
demonstrably needs it. Where a group wants otherwise, it grants explicitly (§7).

---

## 2. Role catalogues

### 2.1 Platform roles (`group_id IS NULL`)

| Role | Purpose | Money-moving |
|---|---|---|
| `SUPER_ADMIN` | Owns the platform. **Protected from ordinary deletion** (§34, FR-APP-010). | No |
| `PLATFORM_ADMIN` | Operates the platform: users, locations, group types, group health. | **No** |
| `SUPPORT_ADMIN` | Resolves user-facing issues. Read-mostly. | **No** |

**No platform role moves money.** This is deliberate and load-bearing. Money moves only
through group-scoped roles exercising group-scoped permissions, so there is no
platform-level account whose compromise drains member funds, and no support engineer who
can be socially engineered into a transfer. A platform admin can suspend an account,
correct a location, or resolve a stuck job. They cannot pay anyone.

The one exception is `ledger.post_adjustment`, which exists because a ledger needs a way
to correct a systemic error — and is therefore subject to §6.5's dual-control rule
rather than being freely grantable.

### 2.2 Group roles (seeded)

| Role | Typical holder | Purpose |
|---|---|---|
| `GROUP_OWNER` | Founders, daily-collection operators | Full authority over the group |
| `CHAIRMAN` | Formal associations, cooperatives | Presides; approves |
| `SECRETARY` | Formal associations | Records, membership administration |
| `TREASURER` | Any group handling funds | Custody of funds; verifies settlements, approves disbursements |
| `COLLECTOR` | Field agents | Records contributions; submits settlements |
| `LOAN_OFFICER` | Cooperatives, associations | Reviews and recommends loans |
| `AUDITOR` | Formal associations | **Read-only.** Sees everything; changes nothing |
| `MEMBER` | Everyone | Contributes, borrows, views own position |

`AUDITOR` is granted no write permission at all, in any group, ever. §5 lists it
alongside the operational roles, and the entire value of the role is that its holder
cannot alter what they inspect. An auditor who can post adjustments is not an auditor.

---

## 3. Permission catalogue

Namespaced `resource.action`. This is the complete initial catalogue; it is seeded, and
the permission table is the registry.

### 3.1 Groups & membership

| Code | Description |
|---|---|
| `groups.view` | View group details |
| `groups.create` | Create a group |
| `groups.update` | Edit name, description, logo, type |
| `groups.archive` | Archive the group |
| `groups.transfer_ownership` | Transfer ownership to another member |
| `groups.manage_settings` | Change group configuration |
| `groups.manage_rules` | Edit written group rules |
| `members.invite` | Invite users to the group |
| `members.view` | View the member list |
| `members.approve` | Approve a membership application |
| `members.suspend` | Suspend a membership |
| `members.remove` | Remove a member |
| `members.assign_roles` | Assign or revoke group roles |
| `members.view_history` | View membership history |

### 3.2 Schemes

| Code | Description |
|---|---|
| `schemes.view` | View schemes |
| `schemes.create` | Create a scheme |
| `schemes.update` | Edit a scheme's configuration |
| `schemes.close` | Close or archive a scheme |
| `schemes.manage_members` | Add or remove scheme members |

### 3.3 Contributions

| Code | Description |
|---|---|
| `contributions.record` | Record a contribution (including on behalf of a member) |
| `contributions.view_own` | View one's own contributions |
| `contributions.view_group` | View contributions across the group |
| `contributions.verify` | Verify a recorded contribution |
| `contributions.reverse` | Reverse a contribution |
| `contributions.waive` | Waive an expected contribution |

### 3.4 Collectors & settlement

| Code | Description |
|---|---|
| `collectors.view` | View collector assignments and performance |
| `collectors.assign` | Assign a collector to a scheme |
| `settlements.submit` | Submit a settlement |
| `settlements.view_own` | View one's own settlement history |
| `settlements.view_group` | View settlements across the group |
| `settlements.verify` | Verify a submitted settlement |
| `settlements.dispute` | Dispute a settlement |
| `settlements.resolve_variance` | Resolve a flagged variance with explanation |

### 3.5 Ledger

| Code | Description |
|---|---|
| `ledger.view_own` | View one's own ledger position |
| `ledger.view_group` | View the group's ledger |
| `ledger.view_platform` | View platform-level totals |
| `ledger.reverse_transaction` | Post a reversing transaction |
| `ledger.post_adjustment` | Post a manual adjusting entry — **dual control (§6.5)** |

### 3.6 Loans & guarantors

| Code | Description |
|---|---|
| `loans.apply` | Apply for a loan |
| `loans.view_own` | View one's own loans |
| `loans.view_group` | View loans across the group |
| `loans.review` | Review an application and recommend |
| `loans.approve` | Approve a loan |
| `loans.reject` | Reject a loan |
| `loans.configure_rules` | Configure the group's eligibility and lending rules |
| `guarantors.respond` | Accept or decline a guarantor request directed at oneself |
| `guarantors.view` | View guarantor records for the group |

### 3.7 Disbursements

| Code | Description |
|---|---|
| `disbursements.view` | View disbursements |
| `disbursements.request` | Request a disbursement |
| `disbursements.review` | Review a disbursement request |
| `disbursements.approve` | Approve a disbursement |
| `disbursements.process_payment` | Execute the payment leg |
| `disbursements.verify` | Verify completion |

### 3.8 Reporting, communication, audit

| Code | Description |
|---|---|
| `reports.view_own` | View one's own reports |
| `reports.view_group` | View group reports |
| `reports.view_platform` | View platform-wide reports |
| `reports.export` | Export report data |
| `communications.message_members` | Message members within one's groups |
| `communications.message_group` | Post a group announcement |
| `communications.bulk_send` | Send a bulk campaign |
| `communications.manage_templates` | Manage notification and message templates |
| `communications.view_history` | View communication history |
| `audit.view_group` | View the group's audit log |
| `audit.view_platform` | View the platform audit log |

### 3.9 Platform administration

| Code | Description |
|---|---|
| `platform.manage_users` | Manage platform user accounts |
| `platform.suspend_account` | Suspend or reinstate an account |
| `platform.manage_locations` | Manage the location tree |
| `platform.manage_group_types` | Manage group types and role templates |
| `platform.view_health` | View system health and job status |
| `settings.manage_platform` | Change platform settings |
| `settings.manage_group` | Change group settings |

---

## 4. Role templates per group type

§5: the role set must fit the group, not the other way round. Seeded templates:

| Group type | Seeded roles | Rationale |
|---|---|---|
| `DAILY_CONTRIBUTION` | `GROUP_OWNER`, `COLLECTOR`, `MEMBER` | §5's example verbatim. A daily collector operation needs someone who owns it, someone who collects, and members. A secretary role here is ceremony. |
| `MONTHLY_SAVINGS` | `GROUP_OWNER`, `TREASURER`, `MEMBER` | Funds accumulate between disbursements; someone must be accountable for them. |
| `TRADERS` | `GROUP_OWNER`, `COLLECTOR`, `MEMBER` | Market-based; cash collected in person. |
| `ASSOCIATION` | `CHAIRMAN`, `SECRETARY`, `TREASURER`, `COLLECTOR`, `AUDITOR`, `MEMBER` | §5's example verbatim. |
| `COOPERATIVE` | `CHAIRMAN`, `SECRETARY`, `TREASURER`, `LOAN_OFFICER`, `AUDITOR`, `MEMBER` | Lending is central, so it gets a dedicated officer. |

A group is seeded from its type's template and may then diverge (§7). Changing a group's
type does **not** silently rewrite its roles — it offers the new template, and the group
decides.

---

## 5. The matrix

Legend: **✓** granted by default · **○** not granted by default, but grantable ·
**✗** never grantable by anyone · **D** dual control required (§6.5)

### 5.1 Platform roles

| Permission | SUPER_ADMIN | PLATFORM_ADMIN | SUPPORT_ADMIN |
|---|:--:|:--:|:--:|
| `platform.manage_users` | ✓ | ✓ | ○ |
| `platform.suspend_account` | ✓ | ✓ | ○ |
| `platform.manage_locations` | ✓ | ✓ | ○ |
| `platform.manage_group_types` | ✓ | ✓ | ✗ |
| `platform.view_health` | ✓ | ✓ | ✓ |
| `settings.manage_platform` | ✓ | ○ | ✗ |
| `ledger.view_platform` | ✓ | ✓ | ✗ |
| `ledger.post_adjustment` | **D** | ✗ | ✗ |
| `reports.view_platform` | ✓ | ✓ | ○ |
| `audit.view_platform` | ✓ | ✓ | ○ |
| `groups.view` | ✓ | ✓ | ✓ |
| `groups.archive` | ✓ | ✓ | ✗ |
| `members.view` | ✓ | ✓ | ✓ |
| `members.suspend` | ✓ | ✓ | ○ |
| `communications.bulk_send` | ✓ | ✓ | ✗ |
| `communications.manage_templates` | ✓ | ✓ | ✗ |
| **Any `contributions.*`** | ✗ | ✗ | ✗ |
| **Any `loans.*` (approve/reject)** | ✗ | ✗ | ✗ |
| **Any `disbursements.*`** | ✗ | ✗ | ✗ |
| **Any `settlements.*`** | ✗ | ✗ | ✗ |

The three rows of ✗ at the bottom are the point of §2.1: **no platform role moves money.**
A super admin has no `disbursements.approve`. This is enforced by the permission seed,
by a service-layer assertion, and by a test asserting the platform roles hold no
money-moving permission.

### 5.2 Group roles

| Permission | OWNER | CHAIRMAN | SECRETARY | TREASURER | COLLECTOR | LOAN_OFFICER | AUDITOR | MEMBER |
|---|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| `groups.view` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `groups.update` | ✓ | ✓ | ○ | ✗ | ✗ | ✗ | ✗ | ✗ |
| `groups.archive` | ✓ | ○ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| `groups.transfer_ownership` | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| `groups.manage_settings` | ✓ | ○ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| `groups.manage_rules` | ✓ | ✓ | ○ | ✗ | ✗ | ✗ | ✗ | ✗ |
| `members.invite` | ✓ | ✓ | ✓ | ○ | ○ | ✗ | ✗ | ✗ |
| `members.view` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `members.approve` | ✓ | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ |
| `members.suspend` | ✓ | ✓ | ○ | ✗ | ✗ | ✗ | ✗ | ✗ |
| `members.remove` | ✓ | ✓ | ○ | ✗ | ✗ | ✗ | ✗ | ✗ |
| `members.assign_roles` | ✓ | ✓ | ○ | ✗ | ✗ | ✗ | ✗ | ✗ |
| `members.view_history` | ✓ | ✓ | ✓ | ○ | ✗ | ✗ | ✓ | ✗ |
| `schemes.view` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `schemes.create` | ✓ | ✓ | ○ | ○ | ✗ | ✗ | ✗ | ✗ |
| `schemes.update` | ✓ | ✓ | ✗ | ○ | ✗ | ✗ | ✗ | ✗ |
| `schemes.close` | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| `schemes.manage_members` | ✓ | ✓ | ✓ | ○ | ✗ | ✗ | ✗ | ✗ |
| `contributions.record` | ✓ | ✓ | ○ | ✓ | ✓ | ✗ | ✗ | ✗ |
| `contributions.view_own` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `contributions.view_group` | ✓ | ✓ | ✓ | ✓ | ○ | ✓ | ✓ | ✗ |
| `contributions.verify` | ✓ | ✓ | ✗ | ✓ | ✗ | ✗ | ✗ | ✗ |
| `contributions.reverse` | ✓ | ○ | ✗ | ✓ | ✗ | ✗ | ✗ | ✗ |
| `contributions.waive` | ✓ | ○ | ✗ | ○ | ✗ | ✗ | ✗ | ✗ |
| `collectors.view` | ✓ | ✓ | ✓ | ✓ | ○ | ✗ | ✓ | ✗ |
| `collectors.assign` | ✓ | ✓ | ✗ | ○ | ✗ | ✗ | ✗ | ✗ |
| `settlements.submit` | ✗ | ✗ | ✗ | ✗ | ✓ | ✗ | ✗ | ✗ |
| `settlements.view_own` | ✗ | ✗ | ✗ | ✗ | ✓ | ✗ | ✗ | ✗ |
| `settlements.view_group` | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ | ✓ | ✗ |
| `settlements.verify` | ✓ | ✓ | ✗ | ✓ | ✗ | ✗ | ✗ | ✗ |
| `settlements.dispute` | ✓ | ✓ | ✗ | ✓ | ✓ | ✗ | ✓ | ✗ |
| `settlements.resolve_variance` | ✓ | ✓ | ✗ | ✓ | ✗ | ✗ | ✗ | ✗ |
| `ledger.view_own` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `ledger.view_group` | ✓ | ✓ | ○ | ✓ | ✗ | ○ | ✓ | ✗ |
| `ledger.reverse_transaction` | ✓ | ○ | ✗ | ✓ | ✗ | ✗ | ✗ | ✗ |
| `ledger.post_adjustment` | **D** | **D** | ✗ | **D** | ✗ | ✗ | ✗ | ✗ |
| `loans.apply` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ | ✓ |
| `loans.view_own` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `loans.view_group` | ✓ | ✓ | ✓ | ✓ | ✗ | ✓ | ✓ | ✗ |
| `loans.review` | ✓ | ✓ | ✗ | ○ | ✗ | ✓ | ✗ | ✗ |
| `loans.approve` | ✓ | ✓ | ✗ | ○ | ✗ | ○ | ✗ | ✗ |
| `loans.reject` | ✓ | ✓ | ✗ | ○ | ✗ | ✓ | ✗ | ✗ |
| `loans.configure_rules` | ✓ | ✓ | ✗ | ✗ | ✗ | ○ | ✗ | ✗ |
| `guarantors.respond` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `guarantors.view` | ✓ | ✓ | ✓ | ✓ | ✗ | ✓ | ✓ | ✗ |
| `disbursements.view` | ✓ | ✓ | ✓ | ✓ | ✗ | ○ | ✓ | ✗ |
| `disbursements.request` | ✓ | ✓ | ○ | ✓ | ✗ | ○ | ✗ | ✗ |
| `disbursements.review` | ✓ | ✓ | ✗ | ✓ | ✗ | ○ | ✗ | ✗ |
| `disbursements.approve` | ✓ | ✓ | ✗ | ✓ | ✗ | ✗ | ✗ | ✗ |
| `disbursements.process_payment` | ✓ | ○ | ✗ | ✓ | ✗ | ✗ | ✗ | ✗ |
| `disbursements.verify` | ✓ | ✓ | ✗ | ✓ | ✗ | ✗ | ○ | ✗ |
| `reports.view_own` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `reports.view_group` | ✓ | ✓ | ✓ | ✓ | ✗ | ✓ | ✓ | ✗ |
| `reports.export` | ✓ | ✓ | ○ | ✓ | ✗ | ○ | ✓ | ✗ |
| `communications.message_members` | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ | ✗ |
| `communications.message_group` | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ |
| `communications.bulk_send` | ✓ | ✓ | ○ | ○ | ✗ | ✗ | ✗ | ✗ |
| `communications.view_history` | ✓ | ✓ | ✓ | ✓ | ○ | ✗ | ✓ | ✗ |
| `audit.view_group` | ✓ | ✓ | ○ | ○ | ✗ | ✗ | ✓ | ✗ |
| `settings.manage_group` | ✓ | ○ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |

Two cells in that table are worth reading twice.

**`settlements.submit` belongs to `COLLECTOR` alone.** It is the one permission that is
deliberately not granted to the owner or the treasurer, because submitting a settlement
is a statement about money *you* are handing over. An owner who can submit a settlement
on a collector's behalf can fabricate one.

**`disbursements.request` and `disbursements.approve` are both granted to `TREASURER`
and `GROUP_OWNER`** — but §6.1 means the same person can never exercise both on the same
disbursement. The role can hold both permissions; the transaction cannot use both.

---

## 6. Separation of duties

§18: *"The person requesting a disbursement should not automatically be allowed to
approve their own request."* §44 rule 6 makes it mandatory. These rules override
configurability — none is grantable away.

### 6.1 Disbursement: requester ≠ approver

`disbursement_approvals` rejects an approval where the approver is the requester. There
is no permission that permits this (03 §10.2.1). Enforced in the service layer **and**
by a database trigger, so it holds against Django admin, a data migration, or a shell.

### 6.2 Settlement: recorder ≠ verifier

A collector who recorded a contribution cannot verify the settlement that contains it.
`settlements.submit` and `settlements.verify` are never both granted to `COLLECTOR`.

### 6.3 Contribution reversal requires a different hand than the original

Where a contribution was recorded by a collector, reversing it requires
`contributions.reverse` — held by `TREASURER`/`CHAIRMAN`/`OWNER`, not `COLLECTOR`.

### 6.4 Role escalation

`members.assign_roles` cannot be used to grant a role the assigner does not themselves
hold, and cannot be used on one's own membership. **Nobody grants themselves a
permission.** Enforced by trigger (03 §10.2.1) and tested.

### 6.5 `ledger.post_adjustment` requires dual control

A manual adjusting entry can move money without a corresponding business event, which
makes it the single most dangerous permission in the system. It requires two distinct
authorised users: one to propose, one to approve.

### 6.6 Ownership cannot be abandoned

A group must always retain at least one `GROUP_OWNER` (FR-RBAC-007). The last owner
cannot be removed or demoted; ownership must be transferred first.

### 6.7 Auditor is read-only

`AUDITOR` holds **no** write permission, in any group, under any configuration. The role
exists to verify; a verifying role that can write verifies nothing.

---

## 7. Configuration bounds

A group configures its roles (§5, R2). The bounds:

**A group may:**
- Rename any non-system role
- Create new roles
- Add or remove the `○` permissions in its own roles
- Choose which roles its group type seeds

**A group may not:**
- Grant a `✗` permission to anyone
- Remove a `✓` permission where it is required for the role to function (e.g.
  `settlements.submit` from `COLLECTOR`, `disbursements.approve` from `TREASURER`)
- Grant `AUDITOR` any write permission
- Grant a permission in a group the role does not belong to
- Exceed `settings.manage_platform`'s `loan_rules_ceiling` (§15) when configuring
  lending rules

The `is_system` column on `group_roles` distinguishes a seeded role a group may
reconfigure from one it may not delete. System roles can be modified but not removed,
because `GROUP_OWNER` and `MEMBER` are structural.

---

## 8. Enforcement

**One function.** Every permission check goes through:

```python
def has_perm(user, group, permission_code) -> bool
```

It resolves the code against `group_permissions` for the user's active memberships in
that group (03 §5.5), caches the result in Redis keyed by
`(user_id, group_id, permission_version)`, and is the only place the question is asked.
An ad-hoc `user.roles.filter(...)` in a view is a defect, and the linter rule and code
review enforce it.

**Three layers, deliberately redundant:**

| Layer | Mechanism | Catches |
|---|---|---|
| API | DRF permission classes per view | The ordinary case: wrong role, wrong group |
| Service | Explicit assertions in the service function | The endpoint that forgot its permission class |
| Database | Triggers for §6's hard rules | Migrations, admin, shells, and future mistakes |

The third layer exists because §44 calls these rules mandatory, and a "mandatory" rule
enforced only in application code is a rule enforced by everyone who remembers it.

**Object-scope, not just role-scope.** Every check is `(user, group, permission)`.
FR-COM-007's rule — a group admin cannot message outside their groups — falls out of
this: `communications.message_members` is checked against each target's group, and a
target outside the requester's groups fails the check. There is no "message anyone"
permission.

**Cache invalidation.** `permission_version` is a counter bumped whenever
`group_roles`, `group_permissions`, `group_members`, or `group_member_roles` changes for
that group. Revocation is immediate rather than eventual (02 §5.6).

---

## 9. Seed data

| Seed | Contents |
|---|---|
| `permissions` | The catalogue in §3, `is_system = TRUE` |
| `group_roles` (platform) | `SUPER_ADMIN`, `PLATFORM_ADMIN`, `SUPPORT_ADMIN`, `group_id = NULL` |
| `group_roles` (templates) | The eight group roles in §2.2, template-scoped |
| `group_permissions` | The default grants in §5 — the `✓` cells |
| `group_types` | Five types (§4), each pointing at its role template |

Seeded via a data migration that is **idempotent** — re-running it corrects drift
without duplicating rows or resetting a group's customisations.

---

## 10. Testing obligations

Carried into 13_TESTING_STRATEGY:

| # | Test |
|---|---|
| T1 | A user with `TREASURER` in Group B has no Treasurer permission in Group A |
| T2 | Every `✗` cell in §5 is unreachable through the API for the stated role |
| T3 | No platform role holds any `contributions.*`, `loans.approve`, `disbursements.*`, or `settlements.*` permission |
| T4 | `AUDITOR` holds zero write permissions across the full catalogue |
| T5 | A requester cannot approve their own disbursement — via API, via Django admin, and via direct SQL |
| T6 | A collector cannot verify their own settlement |
| T7 | A user cannot grant themselves a role or permission |
| T8 | The last owner of a group cannot be removed |
| T9 | A permission revoked mid-session takes effect on the next request |
| T10 | A group admin cannot message a user outside their groups |
| T11 | `has_perm` is the only permission-resolution path (static analysis) |
| T12 | The seeded matrix in §5 matches the database after migration |
