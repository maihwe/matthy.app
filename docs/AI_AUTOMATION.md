# AI automation

IHWEZA now has two independent AI integrations: background assistance for app
users and advisory reviews of pull requests. Both use the OpenAI Responses API;
the default model is `gpt-4.1-mini`. No provider dependency was added.

## In-app automation

Authenticated, phone-verified, active users can request:

- `SUPPORT_ANSWER`: answers grounded in the implemented product's public facts.
- `DRAFT_ANNOUNCEMENT`: a group announcement draft based on supplied details.

English (`en`), French (`fr`) and Swahili (`sw`) are supported by the prompt.
The browser dashboard at `/admin/ai/` uses the existing staff login and MFA flow.
The API supports other authenticated members; member browser sign-in remains future work.
Results are private to the requesting user. The model receives only the supplied
text, language and task instructions. Services do not query group, financial or
personal records for prompts. Generated text is for display/review; render it as
plain text. Draft creation does not publish or send an announcement.

### Enable locally

Supply these values to the web, worker and beat environment through your existing
untracked `.env` or deployment secret store:

```dotenv
AI_AUTOMATION_ENABLED=true
OPENAI_API_KEY=<your API key>
OPENAI_MODEL=gpt-4.1-mini
AI_DAILY_REQUEST_LIMIT=20
AI_TIMEOUT_SECONDS=45
AI_MAX_OUTPUT_TOKENS=1200
```

Apply migrations, then start the existing Compose worker and single beat scheduler:

```sh
docker compose run --rm web python manage.py migrate
docker compose up -d web worker beat
```

The AI endpoints appear in `/api/docs/`. Submit a request with the existing bearer
authentication (or an authenticated, CSRF-protected session):

```http
POST /api/v1/ai/automations/
Authorization: Bearer <access token>
Content-Type: application/json

{
  "kind": "SUPPORT_ANSWER",
  "input_text": "How do group invitations work?",
  "language": "en",
  "idempotency_key": "8dcf28f1-e540-4515-84e4-274726a8e761"
}
```

The response is `202 {"data": {...}}` with a `Location` header. Poll that URL:
`GET /api/v1/ai/automations/{public_id}/`. Status progresses from `QUEUED` to
`RUNNING`, then `SUCCEEDED` (with `output_text`) or `FAILED` (with `error_code`).
Use a fresh UUID for each new request; reuse the same UUID and body to recover a
response after a connection failure. Replays return `200`; changed bodies return
`409 idempotency_conflict`. Another user's result returns `404`.

Input is capped at 4,000 characters. The request limit counts new jobs in the past
24 hours, including failures, and is enforced under a database lock. Limits return
`429`; disabled/unconfigured AI returns `503 ai_unavailable` without creating work.
Existing requests can still be retrieved or replayed when AI is disabled.

### Browser dashboard

Open `/admin/ai/`, or choose **AI workspace** from administration. Sign in with an
existing staff account and complete MFA. A password-only or revoked session cannot
open the workspace. The dashboard provides:

- A support-question or announcement-draft form with English/French/Swahili responses.
- Example questions, an input-length counter and the remaining request allowance.
- Private history for the latest twelve requests and selectable saved results.
- Automatic status/result refresh while the page is visible, plus manual refresh
  and copying of completed results. Polling stops on completion, authentication
  failure, repeated network errors or after five minutes.
- A responsive layout with normal HTML form submission and a manual refresh path
  when JavaScript is unavailable.

Submission uses the same services, ownership checks, usage limits and idempotency
as the API. Browser POSTs require CSRF, and both HTML and JSON result responses use
`Cache-Control: no-store`. AI text is rendered as plain text. If AI is unavailable,
saved results remain readable and the form shows an unavailable state without
exposing provider configuration to users.

The local `.env` can be prepared with fresh development-only Django, database,
JWT and MFA credentials. It is private and git-ignored; a real OpenAI API key still
needs to be configured separately. Do not reuse development credentials in staging
or production.

### Delivery and recovery

PostgreSQL stores job state and idempotency. Publishing happens after commit; a
broker failure leaves a durable queued row. Beat dispatches queued jobs every
minute and recovers running jobs whose five-minute lease expired. A claim token
prevents late attempts from overwriting newer results. Provider timeouts, rate
limits and server errors retry up to three attempts; malformed/refused/incomplete
outputs and permanent provider errors fail. Network calls run outside database
transactions. Creation and terminal states have audit records containing metadata,
without prompt or output text. Provider responses use `store: false`.

A worker crash or an ambiguous provider timeout can cause another generation and
additional provider usage. The job has one accepted result; this does not guarantee
one billed provider request. Keep exactly one beat process and monitor queued/running
age. The current AWS staging template deploys only web: add worker and beat services
and provision their environments before enabling this feature in AWS.

## Developer automation

The existing CI workflow still runs format/lint, types, import contracts, migrations,
tests/coverage, secret scans, dependency checks and OpenAPI validation on PRs.
`.github/workflows/ai-review.yml` adds an AI report on non-draft PRs from this
repository into `main`/`develop`, plus manual review of two full commit SHAs.

To enable after merging the implementation into the target branch:

1. Add repository Actions secret `OPENAI_API_KEY` using a key scoped to developer
   review usage. App secrets and Actions secrets are configured separately.
2. Set repository Actions variable `AI_REVIEW_ENABLED` to `true`.
3. Optionally set `OPENAI_REVIEW_MODEL`; otherwise the default model is used.
4. Open/update a PR, or dispatch **AI review** with full base/head commit SHAs.
5. Download the `ai-review` artifact from the workflow run and assess its findings.

The workflow checks out the trusted base revision and fetches the immutable diff
through GitHub's API. It never checks out or executes changed PR code with the AI
key available. Fork PRs are skipped. The initial implementation PR cannot generate
a report until the trusted base branch contains the review script.
Manual dispatch runs the workflow implementation from its selected branch; select
`main` or `develop`, and use the inputs to identify the code being reviewed.

Enabling review sends eligible code diffs to OpenAI. Environment/key files, binary
changes, the generated OpenAPI contract and production dependency locks are omitted.
Diffs over 180 KB are rejected rather than silently truncated. Reports expire after
seven days. The workflow has only `contents: read`; it saves an artifact and does
not post comments, edit code, approve, merge or deploy. AI review is advisory and
must not replace the existing required checks. No live provider call, GitHub secret
configuration or deployment is performed by adding these files.

Official API references: [quickstart](https://developers.openai.com/api/docs/quickstart)
and [GPT-4.1 mini](https://developers.openai.com/api/docs/models/gpt-4.1-mini).
