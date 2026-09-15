# 09 — Notification Design

**Status:** Phase 0 draft, awaiting review
**Derives from:** 01_PRODUCT_REQUIREMENTS, 02_SYSTEM_ARCHITECTURE, 04_API_SPECIFICATION, Master Development Prompt §20–§23, §36
**Depends on:** 04

---

## 1. Principles

**N1 — Notifications are a side effect, never a step.** No request waits for a
notification. A contribution is recorded whether or not the SMS provider is reachable,
and the record is unaffected by a delivery failure (§23, FR-COM-009). This is why the
entire subsystem is asynchronous.

**N2 — A notification is not a financial record.** It is a copy of one, sent by a
fallible channel to a device that may be offline. The ledger is authoritative; an SMS
that never arrived is a delivery problem, not a data problem. No system decision reads
notification state.

**N3 — The channel is chosen by reliability and cost, not by novelty.** SMS reaches
everyone and costs money per message; push is free and reaches only those with the app
installed and a data connection; email is free and frequently unread. For a Nigerian
community-finance platform the ordering is: in-app and push first, SMS for what must
arrive, email for records and long-form.

**N4 — Silence is a feature.** A platform that notifies on everything trains people to
ignore it, and then the one message that mattered — a failed payment, a disbursement
awaiting approval — is ignored too. §23's twelve triggers are the *complete* list of
automatic notifications, not a starting point.

**N5 — Every notification is auditable.** What was sent, to whom, on which channel, when,
and whether it arrived (§21's tracking requirements). When a member says "I was never
told", the platform must be able to answer factually.

---

## 2. Architecture

```
   domain event
   (contribution recorded, loan approved, …)
          │
          ▼
   ┌──────────────────┐
   │ Notification      │  decides: which users, which channels,
   │ Engine (service)  │  which template, immediate or batched
   └────────┬─────────┘
            │  enqueues (never sends inline)
            ▼
   ┌──────────────────┐
   │  Celery + Redis  │  retries, backoff, rate limits
   └────────┬─────────┘
            │
     ┌──────┼──────┬──────────┬───────────┐
     ▼      ▼      ▼          ▼           ▼
   In-app  Push   SMS      Email      (future)
     │      │      │          │
     ▼      ▼      ▼          ▼
   notifications table   ──► provider adapters ──► delivery receipts
```

The engine is called **after** the transaction that produced the event has committed.
Not inside it: a notification enqueued inside a transaction that then rolls back
announces something that did not happen.

```python
# correct ordering
with transaction.atomic():
    contribution = record_contribution(...)
    post_to_ledger(...)
# commit has happened
notify.contribution_received(contribution)   # enqueues; returns immediately
```

---

## 3. Channels

| Channel | Reaches | Cost | Latency | Used for |
|---|---|---|---|---|
| **In-app** | Users with the app open | Free | Instant | Everything; the default |
| **Push** | App installed + token valid | Free | Seconds | Time-sensitive, low-stakes |
| **SMS** | Any phone | **Per message** | Seconds–minutes | Money-related, must-arrive |
| **Email** | Any address on file | Free | Minutes | Records, statements, long-form, campaigns |

### 3.1 Channel selection

The engine resolves a channel set per trigger, then applies the user's preferences
(§7) and the trigger's delivery class:

| Class | Behaviour | Used by |
|---|---|---|
| `CRITICAL` | All enabled channels; bypasses quiet hours; not suppressible | Payment failed, disbursement approved, guarantor request |
| `TRANSACTIONAL` | Preferred channel, fallback to next on failure; respects quiet hours with a delay, not a drop | Contribution received, loan approved |
| `INFORMATIONAL` | In-app only by default; the user opts into more | New announcement, group invitation |
| `REMINDER` | Batched, respects quiet hours, suppressed if the condition has resolved | Contribution due, loan repayment due |

**A reminder that arrives after the member has already paid is worse than no reminder.**
`REMINDER`-class notifications re-check their condition at send time and drop silently
if it no longer holds. This is the difference between a helpful reminder system and one
that generates support tickets.

### 3.2 Cost control

SMS is the only channel with a marginal cost per message and it is the one most likely
to be over-used. Controls:

- SMS is reserved for §4's trigger table, not offered as a general-purpose bulk channel.
- Per-user daily SMS cap (default 5), beyond which further messages fall back to in-app.
- Bulk campaigns are **email-first**; SMS campaigns require an explicit confirmation
  showing the projected cost and recipient count.
- Provider spend is monitored with an alert threshold, because a runaway campaign is a
  financial incident as much as an operational one.

---

## 4. Trigger catalogue (§23)

The complete list. Each maps to a requirement and carries its delivery class.

| # | Trigger | Recipient | Channels | Class | Source |
|---|---|---|---|---|---|
| 1 | **Contribution due** | Member | Push, SMS (opt-in), in-app | REMINDER | `contribution_schedules` sweep |
| 2 | **Contribution received** | Member | In-app, push; SMS if the member opts in | TRANSACTIONAL | `contribution.completed` |
| 3 | **Payment failed** | Member | All enabled | **CRITICAL** | `payment.failed` |
| 4 | **Loan application submitted** | Applicant; group loan officers | In-app, push | TRANSACTIONAL | `loan_application.submitted` |
| 5 | **Guarantor request** | Named guarantor | All enabled | **CRITICAL** | `loan_guarantor.requested` |
| 6 | **Loan approved** | Applicant | In-app, push, SMS | TRANSACTIONAL | `loan.approved` |
| 7 | **Loan rejected** | Applicant | In-app, push, SMS | TRANSACTIONAL | `loan.rejected` |
| 8 | **Loan repayment due** | Borrower | Push, SMS (opt-in), in-app | REMINDER | `loan_schedules` sweep |
| 9 | **Loan overdue** | Borrower; group officers | All enabled | **CRITICAL** | `loan_schedules` overdue sweep |
| 10 | **Disbursement approved** | Payee; requesting officer | All enabled | **CRITICAL** | `disbursement.approved` |
| 11 | **Group invitation** | Invitee | SMS, push, email | TRANSACTIONAL | `group_invitation.created` |
| 12 | **New announcement** | Group or scheme members | In-app, push | INFORMATIONAL | `message.created` |

Three trigger-specific notes:

**#5 Guarantor request is `CRITICAL` because the loan stalls without it.** §16's
workflow is member applies → guarantor selected → guarantor notified → guarantor reviews.
The workflow has no timeout that advances it, so a guarantor who never learns of the
request is a loan in permanent limbo. The engine escalates: if no response after a
configured interval, the guarantor is reminded, then the applicant is told, then the
group's loan officers are notified.

**#9 Loan overdue reaches group officers, not only the borrower.** §14 gives groups
overdue management responsibility; they cannot manage what they are not told about.

**#1 and #8 are the only two that fire at scale** — a daily sweep across every active
schedule. They are therefore the two that must be batched, rate-limited, and
condition-checked, and they are the ones most likely to cause an incident if built
naively. A single daily-contribution scheme with 10,000 members generates 10,000
reminders in one sweep.

---

## 5. Templates (§20, §27)

`notification_templates`: `code` + `channel` + `locale`, with a body template and a
declared variable set. `UNIQUE (code, channel, locale)`.

| Requirement | Detail |
|---|---|
| N-TPL-001 | Templates are data, not code. Wording changes without a deploy. |
| N-TPL-002 | Variable substitution is **strict** — an undefined variable is a template error at send time, never rendered as an empty string or a literal `{amount}`. |
| N-TPL-003 | Templates are locale-aware from the start; adding a language is rows, not a refactor. |
| N-TPL-004 | Every template declares its required variables, validated at save time. |
| N-TPL-005 | Content is versioned; a delivery record references the template version used, so a past notification's wording is reconstructable. |
| N-TPL-006 | **User-authored content is escaped at render** (10 §6.2). A group name containing markup must not break an announcement. |
| N-TPL-007 | Financial figures are rendered server-side from the ledger, never supplied by a client. |

N-TPL-005 matters for disputes. "You were told the amount was ₦1,000" is answerable only
if the platform kept the rendered text, not just a template id that has since changed.

### 5.1 Formatting money in notifications

Amounts render through the same `Money` type as everything else (02 §5.1), formatted per
the currency's minor units from `currencies.minor_units` (03 §4.5.2). `₦1,000.00` and
`₦1,000` are different strings and members notice; the format is configurable per locale
and applied consistently.

---

## 6. Bulk campaigns (§21)

Separate from the trigger engine: a deliberate, admin-initiated send.

| Requirement | Detail |
|---|---|
| N-CMP-001 | Audiences are resolved **server-side** from a declared filter (04 §13.1); a client never supplies a recipient list. |
| N-CMP-002 | `POST /communications/campaigns/{id}/send/` returns `202` immediately. |
| N-CMP-003 | A campaign is chunked — default 500 recipients per Celery task — so a failure retries a chunk, not the campaign. |
| N-CMP-004 | Progress is tracked per recipient (03 §11.4); the `UNIQUE (campaign_id, user_id, channel)` constraint makes a resumed campaign skip anyone already sent. |
| N-CMP-005 | A campaign can be paused and resumed. |
| N-CMP-006 | Every campaign shows an audience **count before sending** — nobody sends to 40,000 people by accident. |
| N-CMP-007 | Test sends to a single address are supported and encouraged. |
| N-CMP-008 | Campaigns respect unsubscribe and preferences (§7) at send time, not only at creation. |
| N-CMP-009 | Campaign scope is permission-checked: a group admin cannot reach outside their groups (FR-COM-007). |

### 6.1 Audiences (§21)

`ALL_USERS`, `GROUP`, `SCHEME`, `COLLECTORS`, `TREASURERS`, `GROUP_ADMINS`,
`OUTSTANDING_CONTRIBUTIONS`, `ACTIVE_LOANS`, `OVERDUE_LOANS`, `CUSTOM`.

The last four are **derived, not stored**. `OUTSTANDING_CONTRIBUTIONS` is computed from
`contribution_schedules` at send time; a stored membership list would go stale between
campaign creation and sending, and the members who paid in between would receive a
dunning message. Deriving at send time is the only correct answer.

### 6.2 Tracking (§21)

Per campaign: total recipients, sent, delivered, failed, bounced, timestamps.
Per recipient: channel, status, `sent_at`, `delivered_at`, error.

**Delivery vs sent is provider-dependent.** Email providers report delivery and bounces;
most SMS providers in Nigeria report submission, not handset delivery. The schema stores
both independently and the UI **does not present "sent" as "received"** — a distinction
that matters when a member disputes being notified.

---

## 7. Consent and preferences (§21)

FR-COM-005 and §21's *"respect applicable consent, privacy, and unsubscribe
requirements"*, plus 10 §5.3's data protection obligations.

| Requirement | Detail |
|---|---|
| N-PRF-001 | Per-user, per-channel, per-category preferences (`notification_preferences`, 03 §11.6). |
| N-PRF-002 | **`CRITICAL` notifications cannot be disabled.** A member cannot opt out of "your payment failed" or "your loan is overdue". These are not marketing. |
| N-PRF-003 | Unsubscribe is one action, honoured immediately, and applies to campaigns. |
| N-PRF-004 | Consent for SMS and email campaigns is captured explicitly, with a timestamp and source, and is auditable. |
| N-PRF-005 | Marketing content is distinguishable from transactional content, and preference governs marketing only. |
| N-PRF-006 | A user who has never consented receives no campaigns, whatever the audience filter says. |
| N-PRF-007 | Deleted or unsubscribed contacts are suppressed across all campaigns, including ones already queued. |

N-PRF-002 is the requirement that will be argued for. It should not move: a financial
platform that lets a member switch off default warnings is a platform that will later
have to explain why it did not warn them.

---

## 8. Quiet hours and batching

Not in the spec, but required for a notification system people do not resent.

| Requirement | Detail |
|---|---|
| N-QUIET-001 | Non-`CRITICAL` notifications are held between 21:00 and 07:00 local time, delivered at the start of the window. |
| N-QUIET-002 | A held notification is re-evaluated before sending; if its condition resolved, it is dropped (§3.1). |
| N-QUIET-003 | Multiple `REMINDER` notifications for one user are collapsed into one message where the channel allows. |
| N-QUIET-004 | `CRITICAL` notifications ignore quiet hours — a failed payment at 23:00 is worth knowing at 23:00. |

Quiet hours are **local time**, resolved from the user's location, because Nigeria is
UTC+1 and assuming UTC would deliver "morning" reminders at 2am.

---

## 9. Failure handling

| Requirement | Detail |
|---|---|
| N-FAIL-001 | Delivery failure is retried with exponential backoff and jitter — up to 3 attempts for SMS, 5 for email. |
| N-FAIL-002 | A permanently invalid destination (dead push token, hard-bounced email) is marked and suppressed rather than retried forever. |
| N-FAIL-003 | **A failed notification never fails the operation that triggered it** (N1). The contribution is recorded; the SMS is a separate, retryable concern. |
| N-FAIL-004 | Provider outages degrade gracefully: messages queue and drain when the provider returns. |
| N-FAIL-005 | A dead push token is removed on the provider's signal, so the device list stays accurate (FR-AUTH-012). |
| N-FAIL-006 | A queue backlog beyond threshold alerts — an unbounded notification queue is a silent failure. |

N-FAIL-003 is the requirement that protects §44. If sending a receipt SMS were allowed
to throw, a provider outage would start failing contribution recording, which is exactly
backwards: the money was taken, so the record must be written.

---

## 10. Provider abstraction

Mirrors the payment abstraction (02 §5.8) for the same reason: swapping an SMS provider
should not touch the notification engine.

```
NotificationEngine ──► ChannelAdapter (interface)
                            ├── InAppAdapter
                            ├── PushAdapter      (FCM / APNs)
                            ├── SmsAdapter       ──► provider (Termii, Africa's Talking, …)
                            └── EmailAdapter     ──► provider (SES, SendGrid, SMTP)
```

`MockAdapter` for every channel, as with payments: sends are recorded and asserted in
tests, never transmitted. Tests assert **what was sent and to whom**, not that a provider
returned `200`.

---

## 11. Testing obligations

Carried into 13_TESTING_STRATEGY:

| # | Test |
|---|---|
| N1 | A notification failure does not fail or roll back the triggering operation |
| N2 | Notifications are enqueued after commit, never inside the transaction |
| N3 | Sending a bulk campaign to 10,000 recipients does not block an HTTP request |
| N4 | A paused campaign resumes without re-sending to completed recipients |
| N5 | A user with SMS disabled receives no SMS, including for campaigns already queued |
| N6 | A `CRITICAL` notification cannot be disabled via the preferences API |
| N7 | A stale reminder is dropped when its condition has resolved before send |
| N8 | A client-supplied recipient list is rejected; audiences are resolved server-side |
| N9 | A group admin cannot send to users outside their groups |
| N10 | An undefined template variable fails at send rather than rendering literally |
| N11 | Quiet hours hold non-critical messages and pass critical ones |
| N12 | Every §23 trigger fires exactly once per event under retry |
| N13 | Duplicate webhook delivery produces exactly one notification |
| N14 | Money amounts render correctly for a 0-minor-unit currency as well as a 2-digit one |
