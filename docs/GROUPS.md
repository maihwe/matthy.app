# Groups and permissions

S5 implements group structure and authorization on PostgreSQL 16 and Redis.
It includes group types, frozen role templates, membership/history, invitations,
bounded custom roles, ownership transfer, platform assignments, location
administration and scoped read-only group admin screens. The dashboard shows
membership and role counts. Phase 1 has no financial posting or balances.

## Setup

Apply the normal project migrations. `groups/0002_seed_catalogue` installs
71 permission codes, 11 role templates and six group types: DAILY_CONTRIBUTION,
MONTHLY_SAVINGS, TRADERS, ASSOCIATION, COOPERATIVE and WELFARE. Every group has
structural GROUP_OWNER and MEMBER roles.

`python manage.py seed_group_catalogue` repairs the frozen reference catalogue
without resetting group permissions, optional platform grants, or platform overrides
to a group type's defaults. Template policies and permission safety metadata are
immutable; a future catalogue revision needs a new migration.

The first platform SUPER_ADMIN can be assigned explicitly:

```sh
python manage.py bootstrap_platform_admin <public-user-uuid>
```

That user must already be active, a Django superuser, and enrolled in confirmed
MFA. Bootstrap is audited and works only while no platform SUPER_ADMIN exists.
Sign in again afterward. Later platform assignments require an existing platform
SUPER_ADMIN. Django superuser status alone does not grant group permissions.

## Create and join a group

Use authenticated POST `/api/v1/groups/`, for example:

```json
{
  "name": "Opebi savings",
  "group_type": "MONTHLY_SAVINGS",
  "location": "<public-location-uuid>",
  "membership_mode": "INVITE_ONLY",
  "requires_membership_approval": true,
  "officers": [
    {"user": "<public-officer-user-uuid>", "roles": ["TREASURER"]}
  ]
}
```

The founder receives OWNER/MEMBER authority. Initial nominees receive dormant
roles on INVITED memberships. Creation returns 201 with `{data: ...}` and
`data.invitations`, each containing `id`, `user` and a token returned once.
Creation and invitation responses carry `Cache-Control: no-store`.
The founder must sign in again because role bootstrap invalidates prior sessions.

The nominated user accepts with POST `/api/v1/groups/invitations/accept/` and
`{"token": "..."}`. Acceptance becomes PENDING_APPROVAL when the group requires
approval, otherwise ACTIVE. Only ACTIVE memberships confer role authority.
Decline uses `/invitations/decline/` and returns 204. Invitation tokens contain
384 bits of randomness, expire after seven days by default, are stored hashed,
and cannot be redeemed by another user or twice. Resend retires earlier tokens.
Token delivery is currently a caller integration; this slice does not send messages.

OPEN/APPLICATION groups accept authenticated POST `/groups/{id}/join/`.
Membership approval follows `requires_membership_approval`. INVITE_ONLY groups
require an invitation. Owners/officers can approve, suspend, reinstate or remove
members through their permission-guarded endpoints. A member can leave their own
active/suspended membership. Leaving/removal clears roles; rejoining starts with
MEMBER after approval. Status changes create immutable database-owned history.

## Roles and ownership

All permission resolution goes through `apps.groups.permissions.has_perm`.
API permission classes and services both check it. Group and membership IDs must
belong to the requested group; inaccessible group objects return 404.

Group managers create custom roles from frozen templates, rename/delete unassigned
custom roles, and toggle optional grants. Required grants cannot be disabled and
forbidden grants cannot be enabled. AUDITOR denies all group writes, including
writes inherited from another role. Dual-control permissions always return false
from the boolean resolver until the future two-user financial flow supplies proof.

Subsequent role assignments require the assigner to hold that role. Custom roles
can also be delegated from a held template if their grants fit within the assigner's
current authority. Assigning oneself a role or increasing one's own grants is
prohibited. Initial officer nomination is restricted to group creation.

The canonical owner must use `/groups/{id}/transfer-ownership/` with another active
member's public membership UUID before leaving or being demoted. The database
protects the owner and the last platform SUPER_ADMIN. Group archival preserves
membership/history and permits scoped reads while denying writes.

## MFA, caching and administration

Active OWNER, CHAIRMAN and TREASURER templates require MFA, including derived custom
roles. Platform SUPER_ADMIN/PLATFORM_ADMIN require MFA globally. Database triggers
derive `User.role_mfa_required`, increment `authorization_version`, revoke bearer
and browser sessions, and bump group/platform permission versions on changes.
Pending MFA challenges bind the authorization version. A role change therefore
requires fresh sign-in and, for an unenrolled privileged user, enrollment.

Permission cache keys include user, group and platform versions. Cache outages
fall back to current database checks. Write services lock their group before
checking authority, so concurrent role revocation and writes serialize.
Platform mutations lock the singleton permission state before user locks.

Platform operators edit group-type names/default templates for future groups;
existing groups retain their role configuration. Location creation validates
country/type/parent consistency. Location PATCH edits descriptive fields and active
status; DELETE deactivates the node and preserves references. Group admin screens
are scoped and read-only; audited mutations use the API/services.

The generated [OpenAPI schema](openapi.yaml) describes UUIDs, strict input fields,
response envelopes, cursor pages, invitation tokens and 204 responses. List pages
return `{data: [...], pagination: {next_cursor, has_more, limit}}`, defaulting to
50 rows with a maximum of 200. Group/platform writes share the configured
`financial_write` rate scope (60/minute per user), with 429 and Retry-After.

## Verification

S5 is complete and locally verified on 2026-11-06. All ten CI gates passed:
formatting, lint, types, import contracts, migration drift, tests, coverage,
secret scan, dependency audit and OpenAPI contract matching. The full suite passed
1,034 tests against PostgreSQL 16 and Redis. A final focused run passed 120 browser,
MFA, concurrency, seed and compatibility tests, including three additional
regressions added after full-suite collection: 1,037 distinct tests verified.

Final combined line/branch coverage for accounts and groups is 95.90% against the
90% security scope floor; other code has 96.01% line coverage against its 80% floor.
The permission resolver has 97% coverage. Financial/ledger scopes are skipped
because their modules have not been built. The dependency audit found no known
vulnerabilities and schema generation produced no warnings or errors.

Verified cases include every forbidden matrix grant, auditor read-only behavior,
cross-group denial, invitation consent/replay, ownership continuity, role-triggered
MFA and bearer/browser revocation, cache failure fallback, audit rollback, immutable
history, concurrent transfers/revocation/bootstrap, populated migrations, scoped
read-only admin, pagination, CSRF and write rate limits. Zero-version legacy browser
digests survive the additive upgrade, and reseeding preserves grant history after
its original editor loses authority.

Hosted CI, real SMS/email delivery, member-facing screens and financial modules
remain separate integration and implementation work.
