# 14 — Regulatory Checklist

**Status:** Phase 0 draft, awaiting review
**Derives from:** 01_PRODUCT_REQUIREMENTS, Master Development Prompt §45, §52
**Depends on:** 01

---

## 0. What this document is, and what it is not

§45 requires a proper Nigerian legal and regulatory review before production launch.

**This document is not that review, and is not a substitute for it.** It is the
*preparation* for one: a structured list of the questions this platform raises, the
decisions that intersect them, and the evidence a lawyer will need. It is written so
that the review is a focused engagement with a prepared brief rather than a discovery
exercise billed by the hour.

**Nothing here is legal advice.** Where a specific instrument is named, it is named as a
point of enquiry for counsel to confirm — not as a settled position. Where the correct
answer is uncertain, it is recorded as uncertain. No part of this document should be
relied on as a statement of law.

§45 states the governing principle directly:

> *"Do not assume that because software can technically perform an operation, the company is legally permitted to perform it."*

That principle is why this document exists as a Phase 0 blocker rather than a launch-week
task.

### 0.1 Why this platform is not obviously any one regulated thing

A conventional fintech maps onto a known regulatory category — a bank, a lender, a
payment processor — and inherits that category's rulebook. This platform does not map
cleanly, and the ambiguity is the central legal question:

- It **records** group savings, but the funds (under the current working assumption,
  PRD Q1) stay in the group's own account. Recording someone's savings is not the same
  as receiving deposits — but where exactly that line falls is a question for counsel,
  not for this document.
- It **facilitates lending** between a group's members from the group's pool, rather
  than lending the platform's own capital. Whether that makes the platform a lender, a
  broker, or a piece of software is genuinely unclear.
- It **coordinates cash collection** by people who physically handle members' money. The
  collector is an old institution that predates every relevant statute; how existing
  law treats a platform that digitises it is unsettled.
- It **stores identity and financial data** on a population, bringing data protection
  squarely into scope regardless of how the above resolve.

The regulatory answer may differ by *feature*. It is entirely possible that the
contribution-recording feature is unregulated while the payment-gateway feature is
licensed activity. The review should be structured to answer feature by feature rather
than seeking one verdict for the whole product.

---

## 1. The gating question: what licence, if any, is required?

Before any other item, counsel should answer: **does operating this platform require a
licence, and if so, which, from whom, and at what point in the product's life?**

The answer depends on facts that are still open:

| Depends on | Ref | Status |
|---|---|---|
| Whether the platform ever holds funds | Q1 | **Open** — designed for both; currently assuming no custody |
| Whether the platform lends its own money or only facilitates | Q2 | **Open** — currently assuming facilitation only |
| Whether collectors are the platform's agents or the group's | Q3 | **Open** — legal weight beyond the settlement question |
| Whether groups are registered entities | Q6 | **Open** |

**The custody question (Q1) dominates this section.** Under `SYSTEM_OF_RECORD` — the
current default — the platform records money it never holds. Under `PLATFORM_CUSTODY`
it would hold funds, and it is difficult to see how that does not engage licensing
requirements that the other model avoids. This is precisely why Q1 was left as an
explicit decision for counsel rather than defaulted by an engineer, and why the schema
was built to accommodate either answer without a migration (03 §2.6).

---

## 2. Checklist by area (§45)

For each: the question to put to counsel, and the platform's current design position.

### 2.1 Central Bank of Nigeria

| # | Question | Platform position |
|---|---|---|
| 2.1.1 | Does the platform's activity fall within BOFIA or CBN licensing regimes — as a deposit-taking institution, a payment service provider, or another category? | Not intended to be any of these. **Requires confirmation.** |
| 2.1.2 | Does recording members' savings — with funds held in the group's own account — constitute "receiving deposits" for CBN purposes? | Assumed no. This is the pivotal question (Q1). |
| 2.1.3 | If the platform later holds funds, which licence or partnership would that require? | Deferred with Q1; `PLATFORM_CUSTODY` mode exists in the schema but is disabled. |
| 2.1.4 | Are there agent-banking or agent-network rules that apply to field collectors handling cash? | Not yet examined. |
| 2.1.5 | Does the platform need SCUML registration as a designated non-financial business? | Not yet examined; likely dependent on 2.1.1. |

### 2.2 Payment service providers

| # | Question | Platform position |
|---|---|---|
| 2.2.1 | Does integrating a licensed provider (Paystack, as the reference implementation) place the platform under any obligation, or is it purely the provider's responsibility? | Assumed purely the provider's, with the platform as the provider's customer. **Requires confirmation.** |
| 2.2.2 | What does the provider's own compliance framework require of integrators — KYC, transaction monitoring, record retention? | Not yet examined. Must be reviewed before Phase 3. |
| 2.2.3 | Are there restrictions on routing group contributions through a provider's collection account? | Not yet examined. |
| 2.2.4 | Is the platform required to disclose the provider relationship to members? | Not yet examined. |
| 2.2.5 | Can a licensed provider serve as the licensed partner for a future `PLATFORM_CUSTODY` model (§45's *"use licensed financial/payment partners where appropriate"*)? | Not yet examined; this is the likely path if custody is ever adopted. |

### 2.3 KYC

| # | Question | Platform position |
|---|---|---|
| 2.3.1 | **What identity verification is required, at what thresholds, for which activities?** | **Open (Q5).** `user_profiles.kyc_level` exists as a column; thresholds are deliberately unset. |
| 2.3.2 | Do the CBN's tiered KYC limits apply to this platform at all, given it may not be a CBN-regulated entity? | Unknown — follows from 2.1.1. |
| 2.3.3 | How do verification obligations differ between contributing members, collectors, and group officers? | Not yet examined. Collectors handling cash are the highest-risk role and may attract the strictest requirements. |
| 2.3.4 | What verification is required for a group itself, as distinct from its members? | Not yet examined; intersects Q6. |
| 2.3.5 | What are the record-keeping requirements for verification evidence, and for how long? | Not yet examined; affects `documents` retention (03 §12.3). |

### 2.4 AML / CFT

| # | Question | Platform position |
|---|---|---|
| 2.4.1 | Is the platform a reporting entity under the Money Laundering (Prevention and Prohibition) Act? | Unknown — follows from 2.1.1 and 2.1.5. |
| 2.4.2 | What transaction monitoring is required, and what must be reported, to whom, on what timeline? | Not yet examined. The platform already records everything necessary (audit logs, ledger entries, immutable history); the *reporting* obligation is what is unknown. |
| 2.4.3 | What customer due diligence is required before a member joins a group? | Not yet examined; intersects 2.3. |
| 2.4.4 | Are there thresholds above which a contribution, loan, or disbursement must be reported? | Not yet examined. |
| 2.4.5 | Does the cash-intensive nature of collector operations attract additional scrutiny? | Not yet examined. **This is distinctive to this platform** and should be raised explicitly. |

### 2.5 Data protection

| # | Question | Platform position |
|---|---|---|
| 2.5.1 | What are the platform's obligations under the Nigeria Data Protection Act and NDPC requirements? | Not yet examined. Applies regardless of how 2.1 resolves — this obligation exists even if none of the financial ones do. |
| 2.5.2 | Is registration with the NDPC required, and are there thresholds? | Not yet examined. |
| 2.5.3 | What is the lawful basis for processing members' data — consent, contract, legitimate interest? | Not yet examined. |
| 2.5.4 | How do data subject rights interact with financial record-retention obligations? | **Partially designed.** 10 §5.3 SEC-PII-003 reconciles erasure with §44's rules by severing the identity link while preserving the ledger entry. **This reconciliation must be reviewed by counsel before it is relied on.** |
| 2.5.5 | What are the cross-border transfer rules, if any infrastructure sits outside Nigeria? | Not yet examined; affects the deployment region choice (12 §8.1). |
| 2.5.6 | What breach notification obligations apply, and on what timeline? | Not yet examined. |
| 2.5.7 | What consent is required for SMS, email, and push communications? | Partly addressed by FR-COM-005 and `notification_preferences` (03 §11.6); the specific consent standard is not. |

### 2.6 Consumer protection

| # | Question | Platform position |
|---|---|---|
| 2.6.1 | What consumer protection obligations apply to members as consumers of a financial service? | Not yet examined. |
| 2.6.2 | What disclosure is required for fees, interest, and penalties? | Not yet examined. **Intersects Q2, Q4.** The platform must display these accurately the moment they are configured; the *disclosure standard* is the open question. |
| 2.6.3 | What complaint-handling and dispute-resolution process is required? | Not yet examined. The audit trail (03 §12.1) supports it; the *process* does not exist. |
| 2.6.4 | What are the obligations when a collector's shortfall affects a member — who is the member's counterparty? | **Depends on Q3**, which is open. This is both a business-rule and a consumer-protection question. |
| 2.6.5 | Is there a right to a receipt, a statement, or account information, and in what form? | The platform produces both (FR-CON-005); formal obligations are unknown. |

### 2.7 Lending regulations

| # | Question | Platform position |
|---|---|---|
| 2.7.1 | Does facilitating member-to-member group lending require a lending licence? | Unknown — the central question of 2.7. |
| 2.7.2 | If groups lend their own pooled funds, is the platform a lender at all, or a system of record? | Assumed the latter. **Requires confirmation.** |
| 2.7.3 | Nigeria's Money Lenders Law is **state-level** — which states' regimes apply, and does operating nationally require registration in each? | Not yet examined. **This is a significant open question for a nationwide platform.** |
| 2.7.4 | Does the FCCPC's regulatory framework for digital lending apply? | Unknown. **Requires confirmation** of current scope and registration requirements. |
| 2.7.5 | **What interest models and rates are lawful, and what are the disclosure requirements?** | **Open (Q2).** `loans.interest_method` supports flat, reducing balance, and none; no default is chosen precisely because this is a legal question. |
| 2.7.6 | What are the rules on penalties, late fees, and default — including whether they are permitted at all? | **Open (Q4).** §17 permits penalties "where permitted"; permitted is what counsel determines. |
| 2.7.7 | What are the requirements for guarantor arrangements, and are they enforceable as modelled? | §16 models the workflow; enforceability is unexamined. |
| 2.7.8 | What debt-collection rules apply, and does the platform's overdue process (FR-LOAN-014) comply? | Not yet examined. |
| 2.7.9 | What are the requirements for loan agreements and documentation? | Not yet examined; `loan_application_documents` exists but the required contents do not. |

### 2.8 Tax

| # | Question | Platform position |
|---|---|---|
| 2.8.1 | What are the platform's tax obligations — VAT on fees, withholding, company income tax? | Not yet examined. |
| 2.8.2 | Is interest or fee income earned by a **group** taxable, and who is responsible for remitting it — the group or the platform? | Not yet examined. **Significant**: the platform may be creating a taxable event for informal groups with no tax identity. |
| 2.8.3 | Does the platform have reporting obligations to tax authorities, and at what thresholds? | Not yet examined. |
| 2.8.4 | Are members' contributions, payouts, or loan proceeds taxable events? | Not yet examined. |
| 2.8.5 | What records must be retained for tax purposes, and for how long? | Not yet examined; affects retention settings (12 §8.1). |

### 2.9 Company structure

| # | Question | Platform position |
|---|---|---|
| 2.9.1 | What corporate structure is required for the licensed activities identified in §1? | Not yet examined. |
| 2.9.2 | Is a separate licensed entity required for any feature? | Not yet examined. |
| 2.9.3 | What are the obligations regarding the platform's Terms of Service with groups and members, and who is the counterparty? | Not yet examined. **The platform's role — agent, custodian, or software provider — must be clearly stated**, and it depends on Q1 and Q3. |
| 2.9.4 | Does the platform need professional indemnity or other insurance? | Not yet examined. |
| 2.9.5 | Are there directors' or officers' obligations attaching to the financial activities? | Not yet examined. |

### 2.10 Financial reporting

| # | Question | Platform position |
|---|---|---|
| 2.10.1 | What financial reporting obligations apply, to whom, and on what schedule? | Not yet examined. |
| 2.10.2 | If groups are registered entities (Q6), do they have their own reporting obligations the platform is expected to support? | Not yet examined. |
| 2.10.3 | What audit obligations apply? | Not yet examined. The platform's audit trail (03 §12.1) and immutable ledger (§2) are the technical prerequisites for any audit obligation, and both are already designed in. |
| 2.10.4 | What records must be retained, in what form, and for how long? | Partly designed (retention windows in 12 §8.1); the *requirement* is unknown and should drive those windows rather than the reverse. |

### 2.11 Electronic transactions

| # | Question | Platform position |
|---|---|---|
| 2.11.1 | **Are electronic records and digital signatures legally valid and admissible as evidence?** | Assumed yes under the Evidence Act's provisions on electronic records and the relevant NITDA instruments. **Requires confirmation.** |
| 2.11.2 | Do electronic receipts, statements, and audit logs satisfy evidentiary requirements? | The platform's immutability controls (append-only ledger, audit triggers, hash-checked documents) are designed to support admissibility, but the *standard* is unexamined. |
| 2.11.3 | What is the legal status of an e-signature or an in-app acceptance (e.g. a guarantor's approval)? | **Not yet examined. This is material for §16** — a guarantor accepts real liability through an in-app action, and whether that is enforceable as modelled is a genuine open question. |
| 2.11.4 | What consent requirements apply to electronic contracting? | Not yet examined. |
| 2.11.5 | Do OTP-authenticated actions carry the same weight as written consent? | Not yet examined. |

---

## 3. Design decisions awaiting a regulatory answer

These are the points where the software is deliberately unfinished *because* the answer
is legal rather than technical. Each is a case of §52's rule that ambiguity is surfaced
rather than resolved by assumption.

| Ref | Decision | Blocked on | Consequence of each answer |
|---|---|---|---|
| **Q1** | Does the platform take custody of funds? | 2.1.2, 2.2.5 | `SYSTEM_OF_RECORD` keeps the platform a system of record. `PLATFORM_CUSTODY` engages licensing and a licensed partner. **Schema supports both without migration.** |
| **Q2** | What interest model is lawful, and what must be disclosed? | 2.7.5, 2.6.2 | Determines the default for `loans.interest_method` and the amortisation in `loan_schedules`. |
| **Q3** | Who bears a collector's settlement shortfall? | 2.6.4, 2.9.3 | Determines whether the shortfall is a collector liability, a group loss, or a platform cost — and shapes the Terms of Service. |
| **Q4** | Where do late fees accrue, and on what? | 2.7.6, 2.8.2 | `4000 Late Fee Income` exists with its owner deliberately unset. |
| **Q5** | What KYC at what thresholds? | 2.3.1–2.3.3 | Determines the `kyc_level` gate points and the platform's verification obligations. |
| **Q6** | Are groups registered entities? | 2.9.3, 2.10.2 | Determines whether registered cooperatives need distinct handling and reporting. |

---

## 4. Pre-launch gate

§45: the review is completed **before production launch.** The gate:

- [ ] Counsel engaged with this document as the brief
- [ ] §1 answered: licence requirements identified per feature
- [ ] §2.1–2.11 each answered, or explicitly recorded as not applicable with reasoning
- [ ] Q1–Q6 resolved, and the corresponding documents revised to match
- [ ] Terms of Service drafted, stating the platform's role clearly and consistently with Q1/Q3
- [ ] KYC thresholds implemented and enforced (Q5)
- [ ] Interest, fee, and penalty rules implemented per Q2/Q4, with required disclosures
- [ ] Data protection registration and lawful-basis determination completed (2.5)
- [ ] Retention schedules set from the **regulatory requirement**, not from storage convenience
- [ ] Reporting obligations implemented, with a tested path to produce what must be produced
- [ ] Any required licences obtained or licensed partners contracted

**No software milestone substitutes for this gate.** §45's instruction and §52's are the
same instruction: a platform that can technically move money is not thereby permitted to,
and the difference is not something engineering can resolve.

---

## 5. Status summary

| Area | Explored | Requires counsel |
|---|:---:|:---:|
| CBN / licensing | — | ● |
| Payment service providers | Partially | ● |
| KYC | — | ● |
| AML / CFT | — | ● |
| Data protection | Partially | ● |
| Consumer protection | Partially | ● |
| Lending regulations | — | ● |
| Tax | — | ● |
| Company structure | — | ● |
| Financial reporting | Partially | ● |
| Electronic transactions | — | ● |

Nothing in this table is marked complete, and it should not become complete by
engineering effort. This document's success criterion is that it makes the legal review
**cheaper and more thorough** than it would otherwise have been — by arriving with the
questions already identified, the design positions already stated, and the software
already built so that whichever way the answers fall, the change is a configuration
rather than a rewrite.
